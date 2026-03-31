# GreenHarvester

A Python tool that exports everything from your Greenhouse account and converts it into formats ready for import into Lever, with a complete backup of all your data along the way.

---

## What this does

When you run this script, it connects to your Greenhouse account using the official Harvest API and downloads every piece of data your account contains: all candidates, every application ever submitted, job postings, offers, interview feedback, scorecards, team users, org structure, and more. It also downloads the actual resume and attachment files for every candidate.

It then organises all of that into four things:

- **A JSON backup** of every resource type, one file per type, in Greenhouse's native format
- **A CSV backup** of every resource type, flattened into spreadsheet-friendly rows
- **Lever-ready files** with your candidates and applications mapped to Lever's import schema
- **Your attachments on disk**, organised by candidate ID, with each file named by type

The JSON and CSV exports are your own permanent backup of your Greenhouse data. They exist independently of the Lever migration and are useful regardless of where you end up. If you ever need to audit a hiring decision, recover data, or move platforms again in the future, those files are your source of truth. The Lever folder is specifically formatted for Lever's data import tool and is a separate, transformed version of the same data.

---

## Before you start

You will need Python 3.10 or later and two libraries:

```bash
pip install requests tqdm
```

You will also need a Greenhouse Harvest API key. This is different from the regular Greenhouse login — it is a special key that gives read access to your data programmatically.

The script accepts the key either as a flag (`--api-key`) or as the environment variable `GREENHOUSE_API_KEY`. The environment variable is recommended because command-line flags are visible in process listings (`ps aux`) on shared systems:

```bash
export GREENHOUSE_API_KEY=your_key_here
python greenharvester.py
```

### Getting your Harvest API key

1. Log into Greenhouse as a **Site Admin**
2. Click the **Configure** icon in the top navigation bar
3. Select **Dev Center** from the left menu
4. Click **API Credential Management**
5. Click **Create New API Key**, choose **Harvest** as the type
6. Give it a name like "Migration Export"
7. Click **Manage Permissions** and enable read access for all the endpoints you want to export. For a full export, enable everything.
8. Copy the key somewhere safe — you won't be able to see it again

> **Important:** Greenhouse Harvest API keys give binary access to data — either everything or nothing per endpoint. Only share this key with people you trust, and delete or disable it once the migration is complete.

---

## Running the script

### Check first (recommended before a full run)

Before committing to a full export, run `--check` to verify your API key permissions and see how many records exist in each resource:

```bash
python greenharvester.py --api-key YOUR_KEY --check
```

This makes one lightweight API call per endpoint (no data is written) and prints an estimated record count table. Any endpoint your key cannot access is flagged with the reason, so you can fix permissions or add it to `--skip` before starting the real export. `--check` also respects `--after`, `--before`, and `--skip`, so the counts reflect exactly what a real run would export:

```bash
python greenharvester.py --api-key YOUR_KEY --check --after 2024-01-01
```

### Full export (recommended)

```bash
python greenharvester.py --api-key YOUR_HARVEST_API_KEY
```

This exports everything and downloads all attachments. Output goes to a timestamped folder like `./greenhouse_export_20260330_142500/`.

### Skip resume and attachment downloads

```bash
python greenharvester.py --api-key YOUR_KEY --no-resumes
```

Useful if you want to do a fast test run first, or if your org has a very large volume of files and you want to do data first, attachments separately.

### Specify an output directory

```bash
python greenharvester.py --api-key YOUR_KEY --output-dir ~/Desktop/my_export
```

### Export only recent records (incremental / date-filtered)

```bash
# Only candidates and applications created in 2024 or later
python greenharvester.py --api-key YOUR_KEY --after 2024-01-01

# Only records created within a specific window
python greenharvester.py --api-key YOUR_KEY --after 2023-06-01 --before 2024-06-01
```

Date filters are applied to: candidates, applications, jobs, offers, scorecards, interviews, users, and job posts. Endpoints that don't support date filtering (departments, offices, sources, etc.) are always exported in full regardless of these flags.

### Increase parallel download speed for attachments

```bash
python greenharvester.py --api-key YOUR_KEY --workers 10
```

The default is 6 parallel threads. If you have a large number of attachments and a fast connection, increasing this will speed up the download phase noticeably.

### Skip specific resource types

If your API key doesn't have permission for certain endpoints, or you simply don't need them:

```bash
python greenharvester.py --api-key YOUR_KEY --skip email_templates,approval_flows
```

---

## Output structure

Every run creates a timestamped folder so you never accidentally overwrite a previous export:

```
greenhouse_export_20260330_142500/
│
├── manifest.json                  # Summary: counts, errors, export time
├── export.log                     # Full run log with timestamps
│
├── json/                          # Raw Greenhouse data — your permanent backup
│   ├── candidates.json
│   ├── applications.json
│   ├── jobs.json
│   ├── offers.json
│   ├── interviews.json
│   ├── scorecards.json
│   ├── users.json
│   ├── departments.json
│   ├── offices.json
│   ├── job_stages.json
│   ├── job_posts.json
│   ├── rejection_reasons.json
│   ├── sources.json
│   ├── candidate_tags.json
│   ├── email_templates.json
│   └── custom_fields.json
│
├── csv/                           # Same data, flattened for spreadsheet use
│   ├── candidates.csv
│   ├── applications.csv
│   └── … (one per resource)
│
├── lever/                         # Transformed files ready for Lever import
│   ├── lever_candidates.json      # Primary import file — use this in Lever
│   └── lever_candidates.csv       # Human-readable version of the same data
│
└── attachments/                   # Actual files: resumes, cover letters, offers
    ├── 12345/                     # Candidate ID as folder name
    │   ├── resume__John_Smith_CV.pdf
    │   ├── cover_letter__Cover_Letter.pdf
    │   └── offer_letter__Offer_Document.pdf
    └── 67890/
        └── resume__Jane_Doe_Resume.docx
```

---

## Understanding the output folders

### `json/` and `csv/` — your backup

These two folders are your company's independent backup of all Greenhouse data. They are in Greenhouse's own native format, completely unmodified. They are not specific to Lever and are not affected by what happens during the migration.

Keep these files. They are the closest thing to a full database dump that Greenhouse makes available. If you ever need to look up a historical scorecard, verify what stage a candidate was at, check the exact wording of an offer, or prove a hiring timeline for compliance purposes, these files are your reference. The JSON files are the most complete — they include nested objects and all field metadata. The CSVs are the same data flattened into rows, which makes them easier to open in Excel or Google Sheets for ad hoc queries.

Storing both formats gives you flexibility: JSON is better for programmatic access and accuracy, CSV is better for quick human review.

### `lever/` — the Lever import files

These are a transformed version of your candidate data, mapped from Greenhouse's schema to Lever's import schema. This folder exists specifically to support the Lever migration and would not make sense on its own outside of that context.

`lever_candidates.json` is the file you will actually upload to Lever. `lever_candidates.csv` is a human-readable version of the same data, useful for checking the output before you import.

Note that the Lever export only contains candidates and their application data. Jobs, scorecards, and other resource types are not currently importable via Lever's standard import tool — they remain in the JSON/CSV backup for reference.

### `attachments/` — your files

Every resume, cover letter, offer letter, take-home test, and other file attachment is downloaded here, organised into per-candidate subfolders. Each filename is prefixed with its document type (e.g. `resume__`, `cover_letter__`) so you can tell at a glance what each file is without opening it.

These are the actual binary files — PDFs, Word documents, whatever was uploaded to Greenhouse. They are preserved exactly as-is.

Alongside each downloaded file, a zero-byte sibling file with a `.complete` extension is written (e.g. `resume__John_Smith.pdf.complete`). This marker is only created after the file has been fully written to disk. On a re-run, GreenHarvester checks for the marker rather than the file itself — this means an interrupted download (where the file exists but is incomplete) will be retried correctly, rather than being silently skipped. You can safely delete the `.complete` files after the export is done; they serve no purpose outside of the tool.

---

## What gets exported

| Resource | JSON | CSV | Description |
|----------|:----:|:---:|-------------|
| Candidates | ✓ | ✓ | Full profiles including custom fields, tags, all contact info |
| Applications | ✓ | ✓ | Every application, including status, stage, answers to application questions |
| Jobs | ✓ | ✓ | All job records |
| Job Posts | ✓ | ✓ | Published job post content, including HTML descriptions and application questions |
| Offers | ✓ | ✓ | Offer details, status, and compensation data |
| Interviews | ✓ | ✓ | Scheduled interviews with interviewer assignments |
| Scorecards | ✓ | ✓ | Interview feedback and ratings |
| Users | ✓ | ✓ | All Greenhouse team members and their roles |
| Departments | ✓ | ✓ | Organisational structure |
| Offices | ✓ | ✓ | Office locations |
| Job Stages | ✓ | ✓ | Pipeline stage definitions |
| Rejection Reasons | ✓ | ✓ | All configured rejection reason labels |
| Sources | ✓ | ✓ | Candidate source definitions |
| Candidate Tags | ✓ | ✓ | All tag definitions |
| Email Templates | ✓ | ✓ | Saved email templates |
| Custom Fields | ✓ | ✓ | Custom field definitions and configuration |
| Prospect Pools | ✓ | ✓ | Prospect pool definitions and stages |
| Resumes & Attachments | — | — | Downloaded as original files to `attachments/` |

---

## Importing into Lever

### Step 1: Import candidate data

1. In Lever, go to **Settings → Data Import**
2. Upload `lever/lever_candidates.json`
3. Follow Lever's field mapping UI to confirm the column assignments

### Step 2: Upload resumes and attachments

Lever has a bulk resume upload feature that can ingest a folder of files. Point it at the `attachments/` directory. Files are named with their type prefix so Lever can categorise them correctly.

### Step 3: Map custom fields

After the import, check **Settings → Custom Fields** in Lever. The `customFields` values from Greenhouse will be present in the imported records, but you may need to map them to Lever custom fields if the field names differ between the two systems.

---

## Lever field mapping

The following table shows how each Greenhouse field is mapped to its Lever equivalent in the export file. Understanding this is useful if you need to troubleshoot a mismatch or if Lever's import UI asks you to confirm field assignments.

| Lever Field | Greenhouse Source | Notes |
|-------------|-------------------|-------|
| `name` | `first_name` + `last_name` | Combined with a space |
| `headline` | `title` | Candidate's job title |
| `company` | `company` | Current employer |
| `emails` | `email_addresses[].value` | Work-type emails sorted first |
| `phones` | `phone_numbers[].value` | All phone numbers |
| `links` | `website_addresses[]` + `social_media_addresses[]` | All URLs combined |
| `source` | `application.source.public_name` | From the most recent application |
| `origin` | `is_private` flag | `sourced` or `applied` |
| `currentStage` | `application.current_stage.name` | Most recent application |
| `applicationStatus` | `application.status` | `active`, `rejected`, `hired` |
| `jobsAppliedTo` | `application.jobs[].name` | All jobs across all applications |
| `location` | `application.location.address` | Falls back to `candidate.addresses` |
| `tags` | `tags[].name` | All candidate tags |
| `customFields` | `keyed_custom_fields` | Includes type metadata; falls back to `custom_fields` |
| `rejectionReason` | `application.rejection_reason.name` | If applicable |
| `recruiter` | `recruiter.name` | Assigned recruiter |
| `coordinator` | `coordinator.name` | Assigned coordinator |
| `createdAt` | `created_at` | ISO-8601 UTC |
| `updatedAt` | `updated_at` | ISO-8601 UTC |

Each exported Lever record also contains a `_greenhouse` block with the original `candidate_id`, all `application_ids`, and other provenance data. This block is not imported into Lever but is preserved in the file so you can cross-reference records between the Greenhouse backup and Lever after the migration.

---

## Rate limits and performance

The Greenhouse Harvest API allows 50 requests per 10-second window. Exceeding this returns an HTTP 429 response.

The script handles this in two ways. First, it reads the `X-RateLimit-Remaining` header on every response and proactively pauses when the remaining budget drops low — rather than waiting until a 429 is received. Second, if a 429 does occur, it reads the `Retry-After` header and waits exactly as long as the server instructs before retrying.

For failed requests due to network errors or transient server issues, the script uses exponential backoff, doubling the wait time on each consecutive failure up to a maximum of 60 seconds, for up to 6 total attempts per request.

Attachment downloads run in parallel (6 threads by default) and are completely independent of the API rate limit, since they are direct S3 downloads, not Harvest API calls.

For a typical mid-size organisation (2,000 to 10,000 candidates), expect the full export including attachments to take 20 to 60 minutes. Larger organisations with significant attachment volumes can take longer — attachment download time dominates at scale.

---

## Why certain design decisions were made

This section explains the research and reasoning behind some non-obvious choices in the script. It is intended for anyone who wants to audit or extend it.

### Pagination follows Link headers, not page numbers

The Greenhouse API documentation notes that it is transitioning some endpoints to a newer pagination model that returns only a `next` link — no `page` parameter in the response, and no `last` link. Manually incrementing a page counter and reconstructing the URL would silently fail on those endpoints once they complete the transition. The script always extracts and follows the full `next` URL from the `Link` response header, which works correctly for both the legacy numbered-page model and the newer cursor-based model.

### Attachment URLs must be downloaded immediately

Greenhouse hosts all file attachments on Amazon S3 using signed, temporary URLs. These URLs are generated fresh each time you call the API and expire shortly after. The original approach of collecting all URLs during the data export phase and then downloading them later (potentially hours into a long run) would result in expired links and failed downloads for any large export. The script now collects attachment tasks directly from the freshly-fetched API responses and hands them to the download pool immediately, before the S3 signatures have time to expire.

### Application-level attachments are a separate set

In July 2019, Greenhouse added the ability to attach files directly to applications (not just candidate profiles). These are a distinct set of records from candidate-level attachments and are returned by different API endpoints. A script that only reads `candidate.attachments` misses all files attached at the application level — offer packets, signed offer letters, take-home test submissions, and so on. This script reads from both sets and deduplicates by URL.

### `skip_count=true` for performance

On large candidate and application datasets, Greenhouse's API performs a total-count SQL query to populate the `last` pagination link. On tables with hundreds of thousands of rows, this can be slow enough to noticeably extend the export time. The `skip_count=true` parameter disables this count query, which means the `last` link is not returned, but since the script follows `next` links rather than jumping to `last`, nothing is lost. All endpoints that support this parameter have it enabled.

### `keyed_custom_fields` over `custom_fields`

Greenhouse returns custom field data in two formats simultaneously: `custom_fields` (a flat key-value map) and `keyed_custom_fields` (a richer map where each value also includes the field's display name and data type). The `keyed_custom_fields` format is strictly more useful for migration purposes because it lets the receiving system (Lever) understand what type of data each field contains, rather than having to infer it. The script prefers the keyed format and falls back to the flat format only if it is absent.

### Location comes from the application, not the candidate

Greenhouse stores a candidate's location on their application record (as `application.location.address`), not directly on the candidate profile. The candidate object has an `addresses` array but it is typically empty or contains a home address, not the work location used for job matching. The Lever mapping reads `application.location.address` from the most recent application first, which gives the more accurate and relevant location value.

### Email ordering

Lever treats the first email in the `emails` array as the candidate's primary contact address. Greenhouse stores multiple email addresses per candidate with type labels (`work`, `personal`, `other`). The Lever mapping sorts email addresses so that `work`-typed emails appear first, which generally aligns with what a recruiter would want as the primary contact method.

### Social media addresses

Greenhouse has a dedicated `social_media_addresses` field on the candidate object that stores LinkedIn profiles, Twitter handles, and other social links. This is separate from `website_addresses`. The Lever mapping combines both into Lever's single `links` field so no contact links are dropped during migration.

### Completion marker files for attachments

When GreenHarvester downloads an attachment, it writes the file first, then creates a zero-byte sibling marker file named `<filename>.complete`. The skip-on-rerun check looks for the marker, not the file itself. This distinction matters because if a download is interrupted mid-stream — power cut, network drop, process kill — the file will exist on disk but be incomplete. The old approach of checking `dest.exists()` would skip it on the next run, silently leaving a corrupt file in place. With the marker pattern, an incomplete file has no marker, so it will be cleaned up and retried on the next run.

### Date filtering (`--after` / `--before`)

The Greenhouse Harvest API supports `created_after` and `created_before` query parameters on most major list endpoints. GreenHarvester exposes these as `--after` and `--before` CLI flags. They are useful in two scenarios: incremental exports (re-running the tool to pick up only new records since the last run) and scoped exports (pulling only the last two years of candidates for a faster migration). The flags are only applied to endpoints that actually support them — passing date filters to endpoints like departments or offices would return a 422 error, so the script maintains an explicit allowlist (`DATE_FILTERABLE`) and skips the params for everything else.

### `prospect_pools` endpoint

Greenhouse's `prospect_pools` resource defines the pools and stages used to manage prospects before they become formal applicants. It was missing from the original resource list and has been added. Like departments and offices, it does not support date filtering and is always exported in full.

Every record in `lever_candidates.json` includes a `_greenhouse` block containing the original Greenhouse candidate ID and all associated application IDs. This block is not part of the Lever import schema and will not appear in Lever after import. It exists in the file specifically to allow post-migration reconciliation — if you need to verify that a particular Lever candidate maps to a specific Greenhouse record, or if the import partially fails and you need to identify which records were affected, the provenance block gives you the cross-reference.

### Attachment downloads stream to disk in chunks

Attachment files are written to disk in 1 MB chunks via `iter_content` rather than being loaded into memory all at once. Buffering the full response in RAM before writing (the simpler `resp.content` approach) would cause memory usage to scale with file size multiplied by the number of parallel download threads — a problem at scale when large PDFs or offer documents are being downloaded 6 at a time. Chunked writing keeps peak memory bounded to approximately 1 MB per worker regardless of file size.

### `--check` uses `rel="last"` for record counts

The check mode makes one `per_page=1` request per endpoint (without `skip_count=true`) and reads the page number from the `rel="last"` Link header. Because `per_page=1`, the last page number equals the total record count — an exact figure from a single lightweight API call. Endpoints that fit on one page return no `last` link, in which case the script counts the records in the response directly.

### API key via environment variable

Passing the API key as a `--api-key` flag makes it visible in process listings (`ps aux`) on multi-user systems. The script also accepts the key via the `GREENHOUSE_API_KEY` environment variable, which keeps it out of the process table and shell history. The flag still works for convenience, but the environment variable is the safer option on shared machines.

---

## Testing

The repository includes a test suite covering the script's core logic. To run it:

```bash
pip install pytest requests tqdm
pytest test_greenharvester.py -v
```

The tests do not make any network calls — all HTTP behaviour is mocked. They cover:

- Link header parsing (`_parse_next_url`, `_parse_last_page`)
- Filename sanitisation (`_safe_filename`, `_url_filename`)
- CSV flattening (`_flatten`)
- Greenhouse-to-Lever field mapping (`_map_to_lever`) — including application sorting, email ordering, location fallback, custom field preference, origin flag, job deduplication, and the provenance block
- Attachment task collection (`collect_attachment_tasks`) — including deduplication, `.complete` marker skipping, and multi-candidate handling
- HTTP retry logic (`get_with_retry`) — including 429 handling, `Retry-After` header, exponential backoff, and exhaustion behaviour

---

## Deprecation notice

Greenhouse Harvest API v1 and v2 will be retired on **August 31, 2026**. This script uses v1. If you are running this export after that date, you will need to update the `BASE_URL` and potentially some endpoint paths to use the v3 API. The v3 documentation is at [harvestdocs.greenhouse.io](https://harvestdocs.greenhouse.io).

For most migration use cases, running this export before August 2026 is strongly recommended.

---

## Troubleshooting

**Authentication error (401 or 403)**
Your API key is invalid, has expired, or the request was made over HTTP instead of HTTPS. Greenhouse requires HTTPS. Check that the key is correct and that the endpoint permissions are configured in Greenhouse's Dev Center.

**Not sure which endpoints your key has access to**
Run `--check` before a full export. It probes every endpoint and reports any that return a permission error, so you can fix them in Greenhouse's Dev Center or exclude them with `--skip` before starting the real run.

**Some records are missing**
The most common cause is API key permissions. Each endpoint must be explicitly granted in Greenhouse's Dev Center. Check the `export.log` file in the output folder — any endpoint that failed will be logged with the error message.

**Attachment downloads failed**
S3 signed URLs expire quickly. If a large export runs for a very long time before reaching the download phase, some URLs may have expired. The export log records the URL and error for every failed download. In this case, run the script again with `--skip` for all resource types except candidates and applications, then rely on the fresh attachment URLs in the new export.

**A specific resource type is unavailable**
Use `--skip` to exclude that endpoint. For example, if your API key does not have access to `approval_flows`:

```bash
python greenharvester.py --api-key YOUR_KEY --skip approval_flows
```

**The export is taking a very long time**
For organisations with tens of thousands of candidates and many years of attachment files, full exports can take over an hour. The attachment download phase is the most time-consuming. You can increase `--workers` to speed up parallel downloads, or do a first run with `--no-resumes` to get the data quickly and then run again for attachments only.