import io
import json
import unittest
from unittest.mock import patch
import urllib.error

from github_enrich import (
    extract_repo_pr_from_url,
    fetch_pr,
    fetch_pr_commits,
    fetch_pr_timeline_committed_events,
    sync_pr_facts,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _DummyDB:
    def __init__(self, rows):
        self.rows = rows
        self.facts = []
        self.final_commits = {}
        self.timeline_events = {}

    def iter_pr_links(self, limit=None, scope="all_activity"):
        _ = (limit, scope)
        return self.rows

    def upsert_pr_fact(self, fact):
        self.facts.append(fact)

    def replace_pr_commits_final(self, pr_url, rows):
        self.final_commits[pr_url] = list(rows)

    def clear_pr_commit_events_for_pr(self, pr_url):
        self.timeline_events[pr_url] = []

    def upsert_pr_commit_events(self, rows):
        for row in rows:
            self.timeline_events.setdefault(row["pr_url"], []).append(row)

    def commit(self):
        return None


class GitHubEnrichTests(unittest.TestCase):
    def test_extract_repo_pr_from_url(self) -> None:
        self.assertEqual(
            extract_repo_pr_from_url("https://github.com/org/repo/pull/42"),
            ("org", "repo", 42),
        )
        self.assertIsNone(extract_repo_pr_from_url("https://example.com/not-github"))

    def test_sync_skips_without_token(self) -> None:
        db = _DummyDB([])
        stats = sync_pr_facts(db, token=None)
        self.assertTrue(stats["skipped"])
        self.assertEqual(stats["commits_final"], 0)
        self.assertEqual(stats["commit_events"], 0)

    def test_fetch_pr_retries_on_429(self) -> None:
        http_err = urllib.error.HTTPError(
            url="https://api.github.com/repos/org/repo/pulls/7",
            code=429,
            msg="rate limit",
            hdrs=None,
            fp=io.BytesIO(b"{}"),
        )
        payload = {
            "html_url": "https://github.com/org/repo/pull/7",
            "number": 7,
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "created_at": "2026-03-01T00:00:00Z",
            "closed_at": None,
            "merged_at": None,
            "user": {"login": "alice"},
            "base": {"ref": "main", "repo": {"full_name": "org/repo"}},
            "head": {"ref": "feat/x"},
        }
        with patch("github_enrich.urllib.request.urlopen", side_effect=[http_err, _FakeResponse(payload)]):
            fact = fetch_pr("org", "repo", 7, token="tkn")

        self.assertEqual(fact["pr_number"], 7)
        self.assertEqual(fact["repo_full_name"], "org/repo")
        self.assertIsNone(fact["merge_commit_sha"])

    def test_fetch_pr_commits_paginates(self) -> None:
        page1 = [
            {
                "sha": f"sha{i}",
                "author": {"login": "alice"},
                "committer": {"login": "alice"},
                "commit": {
                    "author": {"date": "2026-03-01T00:00:00Z"},
                    "committer": {"date": "2026-03-01T00:00:01Z"},
                    "message": f"commit {i}\n\nbody",
                },
            }
            for i in range(100)
        ]
        page2 = [
            {
                "sha": "sha100",
                "author": {"login": "bob"},
                "committer": {"login": "bob"},
                "commit": {
                    "author": {"date": "2026-03-02T00:00:00Z"},
                    "committer": {"date": "2026-03-02T00:00:01Z"},
                    "message": "last",
                },
            }
        ]
        with patch("github_enrich.urllib.request.urlopen", side_effect=[_FakeResponse(page1), _FakeResponse(page2)]):
            commits = fetch_pr_commits("org", "repo", 7, token="tkn")
        self.assertEqual(len(commits), 101)
        self.assertEqual(commits[0]["message_subject"], "commit 0")
        self.assertEqual(commits[-1]["commit_sha"], "sha100")

    def test_fetch_pr_timeline_keeps_committed_only(self) -> None:
        events = [
            {
                "event": "committed",
                "sha": "abc123",
                "author": {"date": "2026-03-01T01:00:00Z"},
            },
            {
                "id": 11,
                "event": "reviewed",
                "commit_id": "def456",
                "created_at": "2026-03-01T02:00:00Z",
                "actor": {"login": "bob"},
            },
            {
                "id": 12,
                "event": "committed",
                "created_at": "2026-03-01T03:00:00Z",
                "actor": {"login": "charlie"},
            },
        ]
        with patch("github_enrich.urllib.request.urlopen", side_effect=[_FakeResponse(events)]):
            committed = fetch_pr_timeline_committed_events("org", "repo", 9, token="tkn")
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0]["commit_sha"], "abc123")
        self.assertEqual(committed[0]["event_type"], "committed")
        self.assertEqual(committed[0]["event_at"], "2026-03-01T01:00:00Z")

    def test_sync_malformed_url_counts_error(self) -> None:
        db = _DummyDB(rows=[("not-a-url", None, None, "feat/x")])
        stats = sync_pr_facts(db, token="token")
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["commit_errors"], 1)

    def test_sync_populates_facts_commits_and_events(self) -> None:
        db = _DummyDB(rows=[("https://github.com/org/repo/pull/3", "org/repo", 3, "feat/x")])
        with patch(
            "github_enrich.fetch_pr",
            return_value={
                "pr_url": "https://github.com/org/repo/pull/3",
                "repo_full_name": "org/repo",
                "pr_number": 3,
                "state": "closed",
                "is_merged": 1,
                "opened_at": "2026-03-01T00:00:00Z",
                "closed_at": "2026-03-02T00:00:00Z",
                "merged_at": "2026-03-02T00:00:00Z",
                "merge_commit_sha": "merge123",
                "author_login": "alice",
                "base_branch": "main",
                "head_branch": "feat/x",
                "last_synced_at": "2026-03-03T00:00:00Z",
            },
        ), patch(
            "github_enrich.fetch_pr_commits",
            return_value=[
                {
                    "commit_sha": "abc",
                    "authored_at": "2026-03-01T00:00:00Z",
                    "committed_at": "2026-03-01T00:01:00Z",
                    "author_login": "alice",
                    "committer_login": "alice",
                    "message_subject": "first",
                }
            ],
        ), patch(
            "github_enrich.fetch_pr_timeline_committed_events",
            return_value=[
                {
                    "event_id": "org/repo#3:1",
                    "commit_sha": "abc",
                    "event_type": "committed",
                    "event_at": "2026-03-01T00:01:00Z",
                    "actor_login": "alice",
                }
            ],
        ):
            stats = sync_pr_facts(db, token="token")
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(stats["commits_final"], 1)
        self.assertEqual(stats["commit_events"], 1)
        self.assertEqual(len(db.facts), 1)
        self.assertEqual(len(db.final_commits["https://github.com/org/repo/pull/3"]), 1)
        self.assertEqual(len(db.timeline_events["https://github.com/org/repo/pull/3"]), 1)


if __name__ == "__main__":
    unittest.main()
