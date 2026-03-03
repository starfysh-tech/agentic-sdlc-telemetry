import tempfile
import unittest
from pathlib import Path

import manage
from sdlc_extract import DB


class CliModeTests(unittest.TestCase):
    def test_cli_snapshot_repo_filter_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cli.db"
            db = DB(db_path)

            db.conn.executemany(
                "INSERT INTO sessions (session_id,is_subagent,first_timestamp,git_branch,source_file,file_size_bytes,file_mtime) VALUES (?,?,?,?,?,?,?)",
                [
                    ("main_1", 0, "2026-03-01T00:00:00+00:00", "feat/a", "m1", 1, 1.0),
                    ("sub_1", 1, "2026-03-01T01:00:00+00:00", "feat/b", "s1", 1, 1.0),
                ],
            )
            db.conn.executemany(
                "INSERT INTO pr_links (session_id,pr_number,pr_url,pr_repository,timestamp) VALUES (?,?,?,?,?)",
                [
                    ("main_1", 11, "https://github.com/org/repo-a/pull/11", "org/repo-a", "2026-03-01T02:00:00+00:00"),
                    ("sub_1", 22, "https://github.com/org/repo-b/pull/22", "org/repo-b", "2026-03-01T03:00:00+00:00"),
                ],
            )
            db.conn.executemany(
                "INSERT INTO pr_facts (pr_url,repo_full_name,pr_number,state,is_merged,opened_at,closed_at,merged_at,merge_commit_sha,author_login,base_branch,head_branch,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "https://github.com/org/repo-a/pull/11",
                        "org/repo-a",
                        11,
                        "closed",
                        1,
                        "2026-03-01T10:00:00+00:00",
                        "2026-03-01T12:00:00+00:00",
                        "2026-03-01T12:00:00+00:00",
                        "merge11",
                        "alice",
                        "main",
                        "feat/a",
                        "2026-03-02T00:00:00+00:00",
                    ),
                    (
                        "https://github.com/org/repo-b/pull/22",
                        "org/repo-b",
                        22,
                        "open",
                        0,
                        "2026-03-01T11:00:00+00:00",
                        None,
                        None,
                        None,
                        "bob",
                        "main",
                        "feat/b",
                        "2026-03-02T00:00:00+00:00",
                    ),
                ],
            )
            db.conn.executemany(
                "INSERT INTO pr_commit_events (event_id,pr_url,repo_full_name,pr_number,commit_sha,event_type,event_at,actor_login,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("a-pre", "https://github.com/org/repo-a/pull/11", "org/repo-a", 11, "a1", "committed", "2026-03-01T09:00:00+00:00", "alice", "2026-03-02T00:00:00+00:00"),
                    ("a-post", "https://github.com/org/repo-a/pull/11", "org/repo-a", 11, "a2", "committed", "2026-03-01T11:00:00+00:00", "alice", "2026-03-02T00:00:00+00:00"),
                    ("b-post", "https://github.com/org/repo-b/pull/22", "org/repo-b", 22, "b1", "committed", "2026-03-01T12:00:00+00:00", "bob", "2026-03-02T00:00:00+00:00"),
                ],
            )
            db.conn.commit()
            db.close()

            old_db_file = manage.DB_FILE
            manage.DB_FILE = db_path
            try:
                filtered = manage.get_cli_snapshot(
                    scope="all_activity",
                    repos=["org/repo-a"],
                    limit=10,
                )
                delivery_only = manage.get_cli_snapshot(
                    scope="delivery_only",
                    repos=None,
                    limit=10,
                )
            finally:
                manage.DB_FILE = old_db_file

            self.assertEqual(filtered["filters"]["repos"], ["org/repo-a"])
            self.assertEqual(filtered["overview"]["total_prs"], 1)
            self.assertEqual(filtered["repo_summary"][0]["repo"], "org/repo-a")
            self.assertTrue(all(r["repo"] == "org/repo-a" for r in filtered["post_open_outliers"]))

            self.assertEqual(delivery_only["overview"]["total_prs"], 1)
            self.assertEqual(delivery_only["overview"]["repo_count"], 1)


if __name__ == "__main__":
    unittest.main()
