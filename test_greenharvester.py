"""
Unit tests for greenharvester.py

Run with:
    pytest test_greenharvester.py -v

Requirements:
    pip install pytest requests
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import greenharvester as gh


# ---------------------------------------------------------------------------
# _parse_next_url
# ---------------------------------------------------------------------------

class TestParseNextUrl:
    def test_extracts_next(self):
        header = '<https://example.com/v1/candidates?page=2>; rel="next", <https://example.com/v1/candidates?page=10>; rel="last"'
        assert gh._parse_next_url(header) == "https://example.com/v1/candidates?page=2"

    def test_returns_none_when_no_next(self):
        header = '<https://example.com/v1/candidates?page=10>; rel="last"'
        assert gh._parse_next_url(header) is None

    def test_empty_header(self):
        assert gh._parse_next_url("") is None

    def test_next_only(self):
        header = '<https://example.com/v1/jobs?page=3>; rel="next"'
        assert gh._parse_next_url(header) == "https://example.com/v1/jobs?page=3"


# ---------------------------------------------------------------------------
# _parse_last_page
# ---------------------------------------------------------------------------

class TestParseLastPage:
    def test_extracts_last_page(self):
        header = (
            '<https://example.com/v1/candidates?page=2&per_page=1>; rel="next", '
            '<https://example.com/v1/candidates?page=4523&per_page=1>; rel="last"'
        )
        assert gh._parse_last_page(header) == 4523

    def test_returns_none_when_no_last(self):
        header = '<https://example.com/v1/candidates?page=2>; rel="next"'
        assert gh._parse_last_page(header) is None

    def test_empty_header(self):
        assert gh._parse_last_page("") is None

    def test_page_one(self):
        header = '<https://example.com/v1/users?page=1&per_page=1>; rel="last"'
        assert gh._parse_last_page(header) == 1

    def test_large_count(self):
        header = '<https://example.com/v1/applications?page=99999&per_page=1>; rel="last"'
        assert gh._parse_last_page(header) == 99999


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------

class TestSafeFilename:
    def test_replaces_special_chars(self):
        assert gh._safe_filename("John Smith / CV.pdf") == "John_Smith___CV.pdf"

    def test_allows_dots_hyphens_underscores(self):
        assert gh._safe_filename("my-file_v2.docx") == "my-file_v2.docx"

    def test_truncates_to_120(self):
        assert len(gh._safe_filename("a" * 200)) == 120

    def test_empty_string(self):
        assert gh._safe_filename("") == "attachment"

    def test_none(self):
        assert gh._safe_filename(None) == "attachment"


# ---------------------------------------------------------------------------
# _url_filename
# ---------------------------------------------------------------------------

class TestUrlFilename:
    def test_extracts_filename_from_s3_url(self):
        url = "https://s3.amazonaws.com/bucket/path/resume.pdf?X-Amz-Signature=abc123"
        assert gh._url_filename(url) == "resume.pdf"

    def test_no_query_string(self):
        assert gh._url_filename("https://s3.amazonaws.com/bucket/cv.docx") == "cv.docx"

    def test_trailing_slash_returns_fallback(self):
        assert gh._url_filename("https://example.com/") == "attachment"

    def test_empty_string(self):
        assert gh._url_filename("") == "attachment"


# ---------------------------------------------------------------------------
# _flatten
# ---------------------------------------------------------------------------

class TestFlatten:
    def test_flat_dict(self):
        assert gh._flatten({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}

    def test_nested_dict(self):
        assert gh._flatten({"a": {"b": {"c": 3}}}) == {"a.b.c": 3}

    def test_list_values_json_serialised(self):
        result = gh._flatten({"tags": ["a", "b"]})
        assert result["tags"] == json.dumps(["a", "b"])

    def test_mixed_nested(self):
        result = gh._flatten({"name": "Alice", "address": {"city": "London"}, "tags": [1, 2]})
        assert result["name"] == "Alice"
        assert result["address.city"] == "London"
        assert "tags" in result

    def test_non_dict_root_with_parent_key(self):
        assert gh._flatten("hello", parent_key="key") == {"key": "hello"}

    def test_none_value_preserved(self):
        assert gh._flatten({"x": None})["x"] is None

    def test_empty_dict(self):
        assert gh._flatten({}) == {}


# ---------------------------------------------------------------------------
# _map_to_lever
# ---------------------------------------------------------------------------

class TestMapToLever:
    def _cand(self, **kwargs):
        base = {
            "id": 1,
            "first_name": "Jane",
            "last_name": "Doe",
            "title": "Engineer",
            "company": "Acme",
            "email_addresses": [],
            "phone_numbers": [],
            "website_addresses": [],
            "social_media_addresses": [],
            "tags": [],
            "addresses": [],
            "is_private": False,
            "created_at": "2023-01-01T00:00:00Z",
            "updated_at": "2023-06-01T00:00:00Z",
            "last_activity": "2023-06-01T00:00:00Z",
            "recruiter": None,
            "coordinator": None,
        }
        base.update(kwargs)
        return base

    def _app(self, cid=1, status="active", applied_at="2023-06-01T00:00:00Z", **kwargs):
        base = {
            "id": 10,
            "candidate_id": cid,
            "status": status,
            "applied_at": applied_at,
            "jobs": [],
            "source": None,
            "current_stage": None,
            "location": None,
            "rejection_reason": None,
            "rejected_at": None,
            "attachments": [],
        }
        base.update(kwargs)
        return base

    def test_basic_name(self):
        result = gh._map_to_lever(self._cand(), {})
        assert result["name"] == "Jane Doe"

    def test_name_strips_extra_whitespace(self):
        result = gh._map_to_lever(self._cand(first_name="", last_name="Doe"), {})
        assert result["name"] == "Doe"

    def test_no_applications(self):
        result = gh._map_to_lever(self._cand(), {})
        assert result["applicationStatus"] == ""
        assert result["currentStage"] == ""
        assert result["jobsAppliedTo"] == []

    def test_picks_most_recent_application(self):
        apps = {1: [
            self._app(applied_at="2022-01-01T00:00:00Z", status="rejected"),
            self._app(applied_at="2023-06-01T00:00:00Z", status="active"),
        ]}
        result = gh._map_to_lever(self._cand(), apps)
        assert result["applicationStatus"] == "active"

    def test_email_work_sorted_first(self):
        cand = self._cand(email_addresses=[
            {"value": "personal@example.com", "type": "personal"},
            {"value": "work@example.com", "type": "work"},
        ])
        result = gh._map_to_lever(cand, {})
        assert result["emails"][0] == "work@example.com"
        assert result["emails"][1] == "personal@example.com"

    def test_emails_excludes_empty_values(self):
        cand = self._cand(email_addresses=[
            {"value": "", "type": "work"},
            {"value": "good@example.com", "type": "personal"},
        ])
        result = gh._map_to_lever(cand, {})
        assert result["emails"] == ["good@example.com"]

    def test_origin_sourced_when_private(self):
        result = gh._map_to_lever(self._cand(is_private=True), {})
        assert result["origin"] == "sourced"

    def test_origin_applied_when_not_private(self):
        result = gh._map_to_lever(self._cand(is_private=False), {})
        assert result["origin"] == "applied"

    def test_location_from_application(self):
        apps = {1: [self._app(location={"address": "New York, NY"})]}
        result = gh._map_to_lever(self._cand(), apps)
        assert result["location"] == "New York, NY"

    def test_location_falls_back_to_candidate_address(self):
        cand = self._cand(addresses=[{"value": "London, UK"}])
        result = gh._map_to_lever(cand, {})
        assert result["location"] == "London, UK"

    def test_location_empty_when_none(self):
        result = gh._map_to_lever(self._cand(), {})
        assert result["location"] == ""

    def test_keyed_custom_fields_preferred_over_flat(self):
        cand = self._cand(
            keyed_custom_fields={"salary": {"value": 100000, "type": "number"}},
            custom_fields={"salary": 100000},
        )
        result = gh._map_to_lever(cand, {})
        assert result["customFields"] == {"salary": {"value": 100000, "type": "number"}}

    def test_falls_back_to_flat_custom_fields(self):
        cand = self._cand(custom_fields={"department": "Engineering"})
        result = gh._map_to_lever(cand, {})
        assert result["customFields"] == {"department": "Engineering"}

    def test_links_combine_websites_and_social(self):
        cand = self._cand(
            website_addresses=[{"value": "https://janedoe.com"}],
            social_media_addresses=[{"value": "https://linkedin.com/in/janedoe"}],
        )
        result = gh._map_to_lever(cand, {})
        assert "https://janedoe.com" in result["links"]
        assert "https://linkedin.com/in/janedoe" in result["links"]

    def test_website_without_scheme_gets_https_prefix(self):
        cand = self._cand(website_addresses=[{"value": "janedoe.com"}])
        result = gh._map_to_lever(cand, {})
        assert result["links"] == ["https://janedoe.com"]

    def test_jobs_deduplicated_across_applications(self):
        apps = {1: [
            self._app(jobs=[{"name": "Engineer"}, {"name": "Senior Engineer"}]),
            self._app(applied_at="2022-01-01T00:00:00Z", jobs=[{"name": "Engineer"}]),
        ]}
        result = gh._map_to_lever(self._cand(), apps)
        assert sorted(result["jobsAppliedTo"]) == ["Engineer", "Senior Engineer"]

    def test_rejection_reason_from_latest_app(self):
        apps = {1: [self._app(rejection_reason={"name": "Underqualified"}, status="rejected")]}
        result = gh._map_to_lever(self._cand(), apps)
        assert result["rejectionReason"] == "Underqualified"

    def test_recruiter_name(self):
        cand = self._cand(recruiter={"name": "Alice Recruiter"})
        result = gh._map_to_lever(cand, {})
        assert result["recruiter"] == "Alice Recruiter"

    def test_greenhouse_provenance_block(self):
        apps = {1: [self._app(id=99)]}
        result = gh._map_to_lever(self._cand(), apps)
        assert result["_greenhouse"]["candidate_id"] == 1
        assert result["_greenhouse"]["application_ids"] == [99]
        assert result["_greenhouse"]["application_count"] == 1

    def test_greenhouse_provenance_no_apps(self):
        result = gh._map_to_lever(self._cand(), {})
        assert result["_greenhouse"]["application_ids"] == []
        assert result["_greenhouse"]["application_count"] == 0


# ---------------------------------------------------------------------------
# collect_attachment_tasks
# ---------------------------------------------------------------------------

class TestCollectAttachmentTasks:
    def test_candidate_level_attachment(self, tmp_path):
        candidates = [{
            "id": 1,
            "attachments": [{"url": "https://s3.example.com/resume.pdf", "filename": "resume.pdf", "type": "resume"}],
        }]
        tasks = gh.collect_attachment_tasks(candidates, [], tmp_path)
        assert len(tasks) == 1
        url, dest, att_type = tasks[0]
        assert url == "https://s3.example.com/resume.pdf"
        assert att_type == "resume"
        assert dest.name.startswith("resume__")

    def test_application_level_attachment(self, tmp_path):
        candidates = [{"id": 1, "attachments": []}]
        applications = [{
            "candidate_id": 1,
            "attachments": [{"url": "https://s3.example.com/offer.pdf", "filename": "offer.pdf", "type": "offer_letter"}],
        }]
        tasks = gh.collect_attachment_tasks(candidates, applications, tmp_path)
        assert len(tasks) == 1
        assert tasks[0][2] == "offer_letter"

    def test_deduplicates_same_url_across_candidate_and_app(self, tmp_path):
        url = "https://s3.example.com/file.pdf"
        candidates = [{"id": 1, "attachments": [{"url": url, "filename": "f.pdf", "type": "resume"}]}]
        applications = [{"candidate_id": 1, "attachments": [{"url": url, "filename": "f.pdf", "type": "resume"}]}]
        tasks = gh.collect_attachment_tasks(candidates, applications, tmp_path)
        assert len(tasks) == 1

    def test_skips_already_completed(self, tmp_path):
        candidates = [{"id": 1, "attachments": [{"url": "https://s3.example.com/r.pdf", "filename": "r.pdf", "type": "resume"}]}]
        dest_dir = tmp_path / "1"
        dest_dir.mkdir()
        (dest_dir / "resume__r.pdf.complete").touch()
        tasks = gh.collect_attachment_tasks(candidates, [], tmp_path)
        assert len(tasks) == 0

    def test_skips_empty_url(self, tmp_path):
        candidates = [{"id": 1, "attachments": [{"url": "", "filename": "f.pdf", "type": "resume"}]}]
        tasks = gh.collect_attachment_tasks(candidates, [], tmp_path)
        assert len(tasks) == 0

    def test_dest_dir_created_for_pending_task(self, tmp_path):
        candidates = [{"id": 42, "attachments": [{"url": "https://s3.example.com/x.pdf", "filename": "x.pdf", "type": "other"}]}]
        gh.collect_attachment_tasks(candidates, [], tmp_path)
        assert (tmp_path / "42").is_dir()

    def test_no_dir_created_when_already_complete(self, tmp_path):
        candidates = [{"id": 99, "attachments": [{"url": "https://s3.example.com/x.pdf", "filename": "x.pdf", "type": "other"}]}]
        dest_dir = tmp_path / "99"
        dest_dir.mkdir()
        (dest_dir / "other__x.pdf.complete").touch()
        gh.collect_attachment_tasks(candidates, [], tmp_path)
        # Directory existed before; we just confirm no error and no new task
        assert not (tmp_path / "99" / "other__x.pdf").exists()

    def test_multiple_candidates(self, tmp_path):
        candidates = [
            {"id": 1, "attachments": [{"url": "https://s3.example.com/a.pdf", "filename": "a.pdf", "type": "resume"}]},
            {"id": 2, "attachments": [{"url": "https://s3.example.com/b.pdf", "filename": "b.pdf", "type": "resume"}]},
        ]
        tasks = gh.collect_attachment_tasks(candidates, [], tmp_path)
        assert len(tasks) == 2
        cids = {str(t[1].parent.name) for t in tasks}
        assert cids == {"1", "2"}


# ---------------------------------------------------------------------------
# get_with_retry
# ---------------------------------------------------------------------------

class TestGetWithRetry:
    def _make_response(self, status: int, headers: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.headers = headers or {}
        if status >= 400:
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        else:
            resp.raise_for_status = MagicMock()
        return resp

    @patch("greenharvester.time.sleep")
    def test_returns_immediately_on_200(self, mock_sleep):
        session = MagicMock()
        session.get.return_value = self._make_response(200)
        resp = gh.get_with_retry(session, "https://example.com")
        assert resp.status_code == 200
        mock_sleep.assert_not_called()

    @patch("greenharvester.time.sleep")
    def test_retries_once_on_429_then_succeeds(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [
            self._make_response(429, {"Retry-After": "2"}),
            self._make_response(200),
        ]
        resp = gh.get_with_retry(session, "https://example.com")
        assert resp.status_code == 200
        mock_sleep.assert_called_once_with(2.0)

    @patch("greenharvester.time.sleep")
    def test_retries_on_connection_error_then_succeeds(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [
            requests.exceptions.ConnectionError("timeout"),
            self._make_response(200),
        ]
        resp = gh.get_with_retry(session, "https://example.com")
        assert resp.status_code == 200
        assert mock_sleep.called

    @patch("greenharvester.time.sleep")
    def test_raises_after_exhausting_all_retries(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("down")
        with pytest.raises(requests.exceptions.ConnectionError):
            gh.get_with_retry(session, "https://example.com")
        assert session.get.call_count == gh.MAX_RETRIES

    @patch("greenharvester.time.sleep")
    def test_raises_runtime_error_after_all_429s(self, mock_sleep):
        session = MagicMock()
        session.get.return_value = self._make_response(429, {"Retry-After": "0"})
        with pytest.raises(RuntimeError, match="Exhausted retries"):
            gh.get_with_retry(session, "https://example.com")
        assert session.get.call_count == gh.MAX_RETRIES

    @patch("greenharvester.time.sleep")
    def test_429_uses_retry_after_header(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [
            self._make_response(429, {"Retry-After": "15"}),
            self._make_response(200),
        ]
        gh.get_with_retry(session, "https://example.com")
        mock_sleep.assert_called_once_with(15.0)

    @patch("greenharvester.time.sleep")
    def test_429_falls_back_to_rate_limit_window_when_no_header(self, mock_sleep):
        session = MagicMock()
        session.get.side_effect = [
            self._make_response(429, {}),
            self._make_response(200),
        ]
        gh.get_with_retry(session, "https://example.com")
        mock_sleep.assert_called_once_with(float(gh.RATE_LIMIT_WINDOW))
