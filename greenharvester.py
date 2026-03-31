#!/usr/bin/env python3
"""
greenharvester.py
=================
Exports ALL Greenhouse data (via Harvest API v1) to:
  - JSON files             ./output/json/<resource>.json
  - CSV files              ./output/csv/<resource>.csv
  - Lever-ready JSON+CSV   ./output/lever/
  - Attachments on disk    ./output/attachments/<candidate_id>/<type>__<filename>

Usage:
    python greenharvester.py --api-key YOUR_HARVEST_API_KEY

Flags:
    --output-dir    Output root (default: ./greenhouse_export_YYYYMMDD_HHMMSS)
    --no-resumes    Skip attachment/resume download
    --workers       Parallel download threads (default: 6)
    --skip          Comma-separated resource stems to skip (e.g. email_templates)
    --after         Only export records created at or after this date (ISO-8601, e.g. 2024-01-01)
    --before        Only export records created before this date (ISO-8601, e.g. 2025-01-01)
    --check         Probe each endpoint and report estimated record counts. No data is written.

Requirements:
    pip install requests tqdm

WARNING: Greenhouse Harvest API v1/v2 will be deprecated on August 31, 2026.
See https://harvestdocs.greenhouse.io for the v3 migration path.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://harvest.greenhouse.io/v1"
PER_PAGE = 500
MAX_RETRIES = 6
RATE_LIMIT_HEADROOM = 5     # pause proactively when remaining drops this low
RATE_LIMIT_WINDOW = 10      # seconds per window (Greenhouse: 50 req / 10s)

# (endpoint_path, output_stem, skip_count_supported)
# skip_count=True removes the expensive total-count SQL query — big speedup.
RESOURCES: list[tuple[str, str, bool]] = [
    ("jobs",              "jobs",              True),
    ("candidates",        "candidates",        True),
    ("applications",      "applications",      True),
    ("offers",            "offers",            True),
    ("interviews",        "interviews",        True),
    ("scorecards",        "scorecards",        True),
    ("users",             "users",             True),
    ("departments",       "departments",       False),
    ("offices",           "offices",           False),
    ("job_stages",        "job_stages",        False),
    ("job_posts",         "job_posts",         True),
    ("rejection_reasons", "rejection_reasons", False),
    ("sources",           "sources",           False),
    ("tags/candidate",    "candidate_tags",    False),
    ("email_templates",   "email_templates",   False),
    ("custom_fields",     "custom_fields",     False),
    ("prospect_pools",    "prospect_pools",    False),
]

# Endpoints that support created_after / created_before date filtering.
# Not all Harvest endpoints accept these params — applying them to endpoints
# that don't support them results in a 422, so we track this explicitly.
DATE_FILTERABLE = {
    "candidates",
    "applications",
    "jobs",
    "offers",
    "scorecards",
    "interviews",
    "users",
    "job_posts",
}

# ---------------------------------------------------------------------------
# HTTP / rate-limit helpers
# ---------------------------------------------------------------------------

def make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.auth = (api_key, "")
    s.headers.update({"User-Agent": "greenharvester/1.0"})
    return s


def _throttle_from_headers(resp: requests.Response) -> None:
    """
    Proactively throttle using X-RateLimit-Remaining.
    Greenhouse window: 50 requests per 10 seconds.
    When remaining drops to RATE_LIMIT_HEADROOM or below, sleep until reset.
    """
    try:
        remaining = int(resp.headers.get("X-RateLimit-Remaining", 999))
        if remaining <= RATE_LIMIT_HEADROOM:
            reset_ts = resp.headers.get("X-RateLimit-Reset")
            if reset_ts:
                wait = max(0.2, float(reset_ts) - time.time()) + 0.5
            else:
                wait = float(RATE_LIMIT_WINDOW) + 0.5
            log.debug("Rate headroom low (%d left) — sleeping %.1fs", remaining, wait)
            time.sleep(wait)
    except (ValueError, TypeError):
        pass


def get_with_retry(
    session: requests.Session,
    url: str,
    params: dict | None = None,
    stream: bool = False,
) -> requests.Response:
    """GET with exponential backoff, honouring Retry-After on 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params or {}, timeout=60, stream=stream)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", RATE_LIMIT_WINDOW))
                log.warning("429 — sleeping %.0fs (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            _throttle_from_headers(resp)
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = min(2 ** attempt, 60)
            log.warning("Request error (attempt %d/%d): %s — retry in %.0fs", attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Exhausted retries for {url}")


def _parse_next_url(link_header: str) -> str | None:
    """Extract rel="next" URL from an RFC-5988 Link header."""
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def _parse_last_page(link_header: str) -> int | None:
    """Extract the page number from rel="last" in an RFC-5988 Link header."""
    from urllib.parse import parse_qs
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="last"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                qs = parse_qs(urlparse(m.group(1)).query)
                pages = qs.get("page", [])
                if pages:
                    try:
                        return int(pages[0])
                    except ValueError:
                        pass
    return None


# ---------------------------------------------------------------------------
# Pagination — always follow the full Link header URL
# ---------------------------------------------------------------------------

def paginate(
    session: requests.Session,
    endpoint: str,
    skip_count: bool = True,
    created_after: str | None = None,
    created_before: str | None = None,
) -> list:
    """
    Fetch all pages for a Harvest v1 list endpoint.
    Follows Link header `next` URLs directly — never reconstructs them —
    because some endpoints use cursor-based next URLs, not plain page numbers.

    created_after / created_before are ISO-8601 date strings (e.g. "2024-01-01").
    They are only applied to endpoints that support them (see DATE_FILTERABLE).
    """
    first_url = f"{BASE_URL}/{endpoint}"
    first_params: dict = {"per_page": PER_PAGE}
    if skip_count:
        first_params["skip_count"] = "true"
    if created_after:
        first_params["created_after"] = created_after
    if created_before:
        first_params["created_before"] = created_before

    all_records: list = []
    next_url: str | None = first_url
    is_first = True

    with tqdm(desc=f"  {endpoint}", unit=" rec", leave=False, dynamic_ncols=True) as pbar:
        while next_url:
            resp = get_with_retry(
                session,
                next_url,
                params=first_params if is_first else None,
            )
            is_first = False

            data = resp.json()
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                records = next((v for v in data.values() if isinstance(v, list)), [])
            else:
                records = []

            if not records:
                break

            all_records.extend(records)
            pbar.update(len(records))
            next_url = _parse_next_url(resp.headers.get("Link", ""))

    return all_records


# ---------------------------------------------------------------------------
# Attachment collection
# Collect URLs from freshly-fetched data immediately — S3 URLs are ephemeral.
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\.\-]", "_", name or "attachment")[:120]


def _url_filename(url: str) -> str:
    try:
        return urlparse(url).path.split("/")[-1].split("?")[0] or "attachment"
    except Exception:
        return "attachment"


def collect_attachment_tasks(
    candidates: list,
    applications: list,
    att_dir: Path,
) -> list[tuple[str, Path, str]]:
    """
    Build a flat list of (url, dest_path, attachment_type) from both
    candidate-level and application-level attachments (they are distinct sets
    as of the Greenhouse API changelog, July 2019).
    """
    apps_by_cid: dict[int, list] = defaultdict(list)
    for app in applications:
        cid = app.get("candidate_id")
        if cid:
            apps_by_cid[cid].append(app)

    tasks: list[tuple[str, Path, str]] = []
    seen_urls: set[str] = set()

    def _enqueue(url: str, cid, att_type: str, filename: str):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        dest_dir = att_dir / str(cid)
        dest = dest_dir / f"{att_type}__{_safe_filename(filename)}"
        # Skip eagerly if already complete — no need to create the directory
        if (dest.with_suffix(dest.suffix + ".complete")).exists():
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        tasks.append((url, dest, att_type))

    for cand in candidates:
        cid = cand.get("id", "unknown")
        for att in cand.get("attachments", []):
            url = att.get("url", "")
            fn = att.get("filename") or _url_filename(url)
            _enqueue(url, cid, att.get("type", "other"), fn)
        for app in apps_by_cid.get(cid, []):
            for att in app.get("attachments", []):
                url = att.get("url", "")
                fn = att.get("filename") or _url_filename(url)
                _enqueue(url, cid, att.get("type", "other"), fn)

    return tasks


def _download_one(
    session: requests.Session,
    url: str,
    dest: Path,
) -> tuple[bool, str]:
    """
    Download one file to dest. Returns (ok, error_message). Never raises.

    A zero-byte sibling file named '<filename>.complete' is written only after
    the full download succeeds. On re-runs, if the marker exists the file is
    skipped entirely — this is safer than checking dest.exists() alone, which
    can't distinguish a complete file from one that was interrupted mid-write.
    """
    marker = dest.with_suffix(dest.suffix + ".complete")
    if marker.exists():
        return True, ""
    try:
        resp = get_with_retry(session, url, stream=True)
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
        marker.touch()          # only written after a successful full write
        return True, ""
    except Exception as exc:
        # Clean up a potentially partial file so the next run retries cleanly
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False, str(exc)


def download_all_attachments(
    session: requests.Session,
    tasks: list[tuple[str, Path, str]],
    workers: int,
) -> list[tuple[str, str]]:
    """
    Download all attachments in parallel.
    Returns list of (url, error) for any failures.
    """
    if not tasks:
        log.info("  No attachments to download.")
        return []

    log.info("  Downloading %d file(s) with %d threads …", len(tasks), workers)
    failures: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_download_one, session, url, dest): url
            for url, dest, _ in tasks
        }
        with tqdm(total=len(tasks), desc="  attachments", unit=" files", dynamic_ncols=True) as pbar:
            for future in as_completed(future_map):
                url = future_map[future]
                try:
                    ok, err = future.result()
                    if not ok:
                        failures.append((url, err))
                except Exception as exc:
                    failures.append((url, str(exc)))
                pbar.update(1)

    return failures


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _flatten(obj, parent_key: str = "", sep: str = ".") -> dict:
    items: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            nk = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(_flatten(v, nk, sep))
            elif isinstance(v, list):
                items[nk] = json.dumps(v, default=str)
            else:
                items[nk] = v
    else:
        items[parent_key] = obj
    return items


def write_json(data, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def write_csv(records: list, path: Path):
    if not records:
        return
    rows = [_flatten(r) for r in records]
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Lever mapping
# ---------------------------------------------------------------------------

def _str(v) -> str:
    return str(v) if v is not None else ""


def _map_to_lever(candidate: dict, apps_by_cid: dict) -> dict:
    """
    Map a Greenhouse candidate + applications to Lever's candidate import schema.
    https://help.lever.co/hc/en-us/articles/206407135
    """
    cid = candidate.get("id")
    cand_apps = sorted(
        apps_by_cid.get(cid, []),
        key=lambda a: a.get("applied_at") or "",
        reverse=True,
    )
    latest = cand_apps[0] if cand_apps else {}

    # Emails — work type first
    emails_sorted = sorted(
        candidate.get("email_addresses", []),
        key=lambda e: 0 if e.get("type") == "work" else 1,
    )
    emails = [e["value"] for e in emails_sorted if e.get("value")]

    # Phones
    phones = [p["value"] for p in candidate.get("phone_numbers", []) if p.get("value")]

    # Links: website_addresses (note: NOT "websites") + social_media_addresses
    links: list[str] = []
    for w in candidate.get("website_addresses", []):
        val = w.get("value", "")
        if val:
            links.append(val if val.startswith("http") else "https://" + val)
    for s in candidate.get("social_media_addresses", []):
        val = s.get("value", "")
        if val:
            links.append(val)

    # All unique jobs across all applications
    job_names = list({
        j.get("name")
        for a in cand_apps
        for j in a.get("jobs", [])
        if j.get("name")
    })

    # Tags
    tags = [t["name"] for t in candidate.get("tags", []) if t.get("name")]

    # Custom fields — prefer keyed version (includes type metadata)
    custom_fields = candidate.get("keyed_custom_fields") or candidate.get("custom_fields") or {}

    # Location — Greenhouse stores it on the application, not the candidate profile
    location = (
        (latest.get("location") or {}).get("address")
        or next((a.get("value") for a in (candidate.get("addresses") or [])), "")
    )

    return {
        # Core identity
        "name":              f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip(),
        "headline":          _str(candidate.get("title")),
        "company":           _str(candidate.get("company")),
        "emails":            emails,
        "phones":            phones,
        "links":             links,

        # Pipeline
        "source":            (latest.get("source") or {}).get("public_name", ""),
        "origin":            "sourced" if candidate.get("is_private") else "applied",
        "currentStage":      (latest.get("current_stage") or {}).get("name", ""),
        "applicationStatus": _str(latest.get("status")),
        "jobsAppliedTo":     job_names,

        # Dates
        "createdAt":         _str(candidate.get("created_at")),
        "updatedAt":         _str(candidate.get("updated_at")),
        "lastActivity":      _str(candidate.get("last_activity")),

        # Location
        "location":          location,

        # Tags & custom data
        "tags":              tags,
        "customFields":      custom_fields,

        # Rejection
        "rejectionReason":   (latest.get("rejection_reason") or {}).get("name", ""),
        "rejectedAt":        _str(latest.get("rejected_at")),

        # Team
        "recruiter":         (candidate.get("recruiter") or {}).get("name", ""),
        "coordinator":       (candidate.get("coordinator") or {}).get("name", ""),

        # Greenhouse provenance — keep for reconciliation
        "_greenhouse": {
            "candidate_id":      cid,
            "is_private":        candidate.get("is_private"),
            "application_ids":   [a.get("id") for a in cand_apps],
            "application_count": len(cand_apps),
        },
    }


def build_lever_export(candidates: list, applications: list) -> list:
    apps_by_cid: dict = defaultdict(list)
    for app in applications:
        cid = app.get("candidate_id")
        if cid:
            apps_by_cid[cid].append(app)

    return [
        _map_to_lever(c, apps_by_cid)
        for c in tqdm(candidates, desc="  mapping → Lever", unit=" cands", dynamic_ncols=True)
    ]


# ---------------------------------------------------------------------------
# Check mode
# ---------------------------------------------------------------------------

def run_check(
    session: requests.Session,
    skip_stems: set,
    after: str | None,
    before: str | None,
) -> None:
    """
    Probe each endpoint with a single per_page=1 request and estimate record
    counts from the rel="last" Link header. No files are written.
    """
    print()
    print("=" * 64)
    print("CHECK MODE — probing Greenhouse API (no data written)")
    print("=" * 64)
    if after or before:
        print(f"  Date filter: after={after or 'none'}  before={before or 'none'}")
    print()

    COL = 30
    print(f"  {'Resource':<{COL}}  {'Est. records':>12}   Status")
    print("  " + "─" * 60)

    total = 0
    n_errors = 0

    for endpoint, stem, _ in RESOURCES:
        if stem in skip_stems:
            print(f"  {stem:<{COL}}  {'':>12}   [skipped]")
            continue

        params: dict = {"per_page": 1}
        date_note = ""
        if stem in DATE_FILTERABLE:
            if after:
                params["created_after"] = after
            if before:
                params["created_before"] = before
        else:
            if after or before:
                date_note = "  (no date filter)"

        try:
            resp = get_with_retry(session, f"{BASE_URL}/{endpoint}", params=params)
            link = resp.headers.get("Link", "")
            last_page = _parse_last_page(link)

            if last_page is not None:
                # per_page=1 means last page number == total record count
                count = last_page
            else:
                # No "last" link — fits on one page; count what came back
                data = resp.json()
                if isinstance(data, list):
                    count = len(data)
                elif isinstance(data, dict):
                    lst = next((v for v in data.values() if isinstance(v, list)), [])
                    count = len(lst)
                else:
                    count = 0

            total += count
            print(f"  {stem:<{COL}}  {count:>12,}   OK{date_note}")

        except requests.exceptions.HTTPError as exc:
            n_errors += 1
            status = exc.response.status_code if exc.response is not None else "?"
            if status == 403:
                label = "FORBIDDEN — add permission in Dev Center or use --skip"
            elif status == 404:
                label = "NOT FOUND"
            elif status == 422:
                label = "UNPROCESSABLE — date params rejected by this endpoint"
            else:
                label = f"HTTP {status}"
            print(f"  {stem:<{COL}}  {'':>12}   [{label}]")

        except Exception as exc:
            n_errors += 1
            print(f"  {stem:<{COL}}  {'':>12}   [ERROR: {exc}]")

    print("  " + "─" * 60)
    print(f"  {'Total (estimated)':<{COL}}  {total:>12,}")
    print()
    if n_errors:
        print(f"  {n_errors} endpoint(s) had errors.")
        print("  Add those stems to --skip, fix permissions, then re-run without --check.")
    else:
        print("  All endpoints accessible. Run without --check to start the export.")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Greenhouse Harvest API → Lever / flat-file migration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Greenhouse Harvest API key. Can also be set via the "
             "GREENHOUSE_API_KEY environment variable.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--no-resumes", action="store_true")
    parser.add_argument("--workers",    type=int, default=6)
    parser.add_argument("--skip",       default="", help="Comma-separated stems to skip")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Probe each endpoint and print estimated record counts. No data is written.",
    )
    parser.add_argument(
        "--after",
        default=None,
        metavar="DATE",
        help="Only export records created at or after this date (ISO-8601, e.g. 2024-01-01). "
             "Applied to: candidates, applications, jobs, offers, scorecards, interviews, users, job_posts.",
    )
    parser.add_argument(
        "--before",
        default=None,
        metavar="DATE",
        help="Only export records created before this date (ISO-8601, e.g. 2025-01-01). "
             "Applied to the same endpoints as --after.",
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GREENHOUSE_API_KEY")
    if not api_key:
        parser.error("Provide --api-key or set the GREENHOUSE_API_KEY environment variable.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or f"./greenhouse_export_{ts}")
    json_dir   = output_dir / "json"
    csv_dir    = output_dir / "csv"
    lever_dir  = output_dir / "lever"
    att_dir    = output_dir / "attachments"
    log_path   = output_dir / "export.log"

    for d in [json_dir, csv_dir, lever_dir, att_dir]:
        d.mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)

    skip_stems = {s.strip() for s in args.skip.split(",") if s.strip()}
    session    = make_session(api_key)

    # Validate date args early so we fail fast before making any API calls
    for label, val in [("--after", args.after), ("--before", args.before)]:
        if val:
            try:
                datetime.fromisoformat(val)
            except ValueError:
                log.error("%s value %r is not a valid ISO-8601 date (e.g. 2024-01-01)", label, val)
                sys.exit(1)

    # -- Verify --
    log.info("Verifying API key …")
    try:
        r = session.get(f"{BASE_URL}/users", params={"per_page": 1}, timeout=15)
        if r.status_code == 401:
            log.error("Invalid API key. Exiting.")
            sys.exit(1)
        if r.status_code == 403:
            log.error("Forbidden — check permissions and that you are using HTTPS.")
            sys.exit(1)
        r.raise_for_status()
        log.info("Connected  |  rate limit: %s req / %ds", r.headers.get("X-RateLimit-Limit", "?"), RATE_LIMIT_WINDOW)
    except requests.exceptions.RequestException as e:
        log.error("Connection failed: %s", e)
        sys.exit(1)

    log.warning(
        "Greenhouse Harvest API v1/v2 deprecates August 31, 2026. "
        "Plan v3 migration: https://harvestdocs.greenhouse.io"
    )

    if args.check:
        run_check(session, skip_stems, args.after, args.before)
        return

    if args.after or args.before:
        log.info(
            "Date filter active — after: %s  before: %s  (applied to date-filterable endpoints only)",
            args.after or "none",
            args.before or "none",
        )

    exported: dict[str, list] = {}
    errors: dict[str, str]    = {}

    # -----------------------------------------------------------------------
    # Phase 1 — Resources
    # -----------------------------------------------------------------------
    print()
    print("=" * 64)
    print("PHASE 1 — Exporting resources")
    print("=" * 64)

    for endpoint, stem, skip_count in RESOURCES:
        if stem in skip_stems:
            log.info("[%s] skipped", stem)
            exported[stem] = []
            continue
        log.info("[%s]", stem)
        try:
            # Only pass date filters to endpoints that support them
            date_kwargs = {}
            if stem in DATE_FILTERABLE:
                if args.after:
                    date_kwargs["created_after"] = args.after
                if args.before:
                    date_kwargs["created_before"] = args.before

            records = paginate(session, endpoint, skip_count=skip_count, **date_kwargs)
            exported[stem] = records
            write_json(records, json_dir / f"{stem}.json")
            write_csv(records,  csv_dir  / f"{stem}.csv")
            log.info("  %d record(s)", len(records))
        except Exception as exc:
            log.error("  Failed: %s", exc)
            errors[stem] = str(exc)
            exported[stem] = []

    # -----------------------------------------------------------------------
    # Phase 2 — Attachments
    # Collect from freshly-fetched data — signed S3 URLs expire quickly.
    # -----------------------------------------------------------------------
    att_failures: list[tuple[str, str]] = []
    if not args.no_resumes:
        print()
        print("=" * 64)
        print("PHASE 2 — Downloading resumes & attachments")
        print("=" * 64)
        candidates   = exported.get("candidates", [])
        applications = exported.get("applications", [])
        tasks = collect_attachment_tasks(candidates, applications, att_dir)
        att_failures = download_all_attachments(session, tasks, workers=args.workers)
        if att_failures:
            log.warning("%d attachment(s) failed — see export.log", len(att_failures))
            for url, err in att_failures:
                log.error("  FAIL [%s] %s", err, url)

    # -----------------------------------------------------------------------
    # Phase 3 — Lever
    # -----------------------------------------------------------------------
    print()
    print("=" * 64)
    print("PHASE 3 — Building Lever import files")
    print("=" * 64)
    lever_records = build_lever_export(
        exported.get("candidates", []),
        exported.get("applications", []),
    )
    write_json(lever_records, lever_dir / "lever_candidates.json")
    write_csv(lever_records,  lever_dir / "lever_candidates.csv")
    log.info("%d Lever candidate record(s) written", len(lever_records))

    # -- Manifest --
    att_file_count = (
        sum(1 for f in att_dir.rglob("*") if f.is_file() and not f.suffix == ".complete")
        if not args.no_resumes else None
    )
    manifest = {
        "exported_at":            datetime.now(timezone.utc).isoformat(),
        "api":                    "Greenhouse Harvest v1",
        "deprecation_note":       "v1/v2 retires August 31, 2026 — migrate to v3",
        "date_filter_after":      args.after,
        "date_filter_before":     args.before,
        "record_counts":          {s: len(r) for s, r in exported.items()},
        "lever_candidates":       len(lever_records),
        "attachments_downloaded": not args.no_resumes,
        "attachment_files":       att_file_count,
        "resource_errors":        errors,
        "attachment_failures":    len(att_failures),
    }
    write_json(manifest, output_dir / "manifest.json")

    # -- Summary --
    print()
    print("=" * 64)
    print("EXPORT COMPLETE")
    print("=" * 64)
    print(f"Output    : {output_dir.resolve()}")
    print(f"Log file  : {log_path.resolve()}")
    print()
    print("Record counts:")
    for stem, count in manifest["record_counts"].items():
        flag = "  [ERROR]" if stem in errors else ""
        print(f"  {stem:<28} {count:>8,}{flag}")
    print(f"  {'lever_candidates':<28} {len(lever_records):>8,}")

    if not args.no_resumes:
        print(f"\n  Attachment files   : {att_file_count}")
        if att_failures:
            print(f"  Attachment failures: {len(att_failures)}  (URLs in export.log)")

    if errors:
        print(f"\n  {len(errors)} resource(s) had errors — see export.log")

    print()
    print("Lever import steps:")
    print("  1. Settings → Data Import → upload lever/lever_candidates.json")
    print("  2. Bulk-upload attachments/ folder via Lever resume import")
    print("  3. Map customFields keys in Lever after import")
    print()
    print("Done. v")


if __name__ == "__main__":
    main()