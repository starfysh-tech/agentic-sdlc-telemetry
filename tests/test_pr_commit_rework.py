import tempfile
import unittest
from pathlib import Path

import manage
from sdlc_extract import DB


class PRCommitReworkTests(unittest.TestCase):
    def test_summary_outliers_and_scope_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rework.db"
            db = DB(db_path)

            db.conn.execute(
                "INSERT INTO sessions (session_id,is_subagent,first_timestamp,git_branch,source_file,file_size_bytes,file_mtime) VALUES (?,?,?,?,?,?,?)",
                ("s_main", 0, "2026-03-01T00:00:00+00:00", "feat/main", "m", 1, 1.0),
            )
            db.conn.execute(
                "INSERT INTO sessions (session_id,is_subagent,first_timestamp,git_branch,source_file,file_size_bytes,file_mtime) VALUES (?,?,?,?,?,?,?)",
                ("s_sub", 1, "2026-03-01T01:00:00+00:00", "feat/sub", "s", 1, 1.0),
            )

            db.conn.executemany(
                "INSERT INTO pr_links (session_id,pr_number,pr_url,pr_repository,timestamp) VALUES (?,?,?,?,?)",
                [
                    ("s_main", 11, "https://github.com/org/repo/pull/11", "org/repo", "2026-03-01T01:00:00+00:00"),
                    ("s_main", 12, "https://github.com/org/repo/pull/12", "org/repo", "2026-03-01T02:00:00+00:00"),
                    ("s_main", 13, "https://github.com/org/repo/pull/13", "org/repo", "2026-03-01T03:00:00+00:00"),
                    ("s_sub", 14, "https://github.com/org/repo/pull/14", "org/repo", "2026-03-01T04:00:00+00:00"),
                ],
            )

            db.conn.executemany(
                "INSERT INTO pr_facts (pr_url,repo_full_name,pr_number,state,is_merged,opened_at,closed_at,merged_at,merge_commit_sha,author_login,base_branch,head_branch,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "https://github.com/org/repo/pull/11",
                        "org/repo",
                        11,
                        "closed",
                        1,
                        "2026-03-01T10:00:00+00:00",
                        "2026-03-01T12:00:00+00:00",
                        "2026-03-01T12:00:00+00:00",
                        "merge11",
                        "alice",
                        "main",
                        "feat/main",
                        "2026-03-02T00:00:00+00:00",
                    ),
                    (
                        "https://github.com/org/repo/pull/12",
                        "org/repo",
                        12,
                        "closed",
                        1,
                        "2026-03-01T10:00:00+00:00",
                        "2026-03-01T13:00:00+00:00",
                        "2026-03-01T13:00:00+00:00",
                        "merge12",
                        "bob",
                        "main",
                        "feat/main",
                        "2026-03-02T00:00:00+00:00",
                    ),
                    (
                        "https://github.com/org/repo/pull/13",
                        "org/repo",
                        13,
                        "open",
                        0,
                        None,
                        None,
                        None,
                        None,
                        "carol",
                        "main",
                        "feat/main",
                        "2026-03-02T00:00:00+00:00",
                    ),
                    (
                        "https://github.com/org/repo/pull/14",
                        "org/repo",
                        14,
                        "closed",
                        1,
                        "2026-03-01T11:00:00+00:00",
                        "2026-03-01T14:00:00+00:00",
                        "2026-03-01T14:00:00+00:00",
                        "merge14",
                        "dora",
                        "main",
                        "feat/sub",
                        "2026-03-02T00:00:00+00:00",
                    ),
                ],
            )

            db.conn.executemany(
                "INSERT INTO pr_commit_events (event_id,pr_url,repo_full_name,pr_number,commit_sha,event_type,event_at,actor_login,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("11:1", "https://github.com/org/repo/pull/11", "org/repo", 11, "a1", "committed", "2026-03-01T09:00:00+00:00", "alice", "2026-03-02T00:00:00+00:00"),
                    ("11:2", "https://github.com/org/repo/pull/11", "org/repo", 11, "a2", "committed", "2026-03-01T11:00:00+00:00", "alice", "2026-03-02T00:00:00+00:00"),
                    ("14:1", "https://github.com/org/repo/pull/14", "org/repo", 14, "z1", "committed", "2026-03-01T12:00:00+00:00", "dora", "2026-03-02T00:00:00+00:00"),
                ],
            )

            db.conn.executemany(
                "INSERT INTO pr_commits_final (pr_url,repo_full_name,pr_number,commit_sha,authored_at,committed_at,author_login,committer_login,message_subject,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("https://github.com/org/repo/pull/12", "org/repo", 12, "b1", "2026-03-01T09:30:00+00:00", "2026-03-01T09:30:00+00:00", "bob", "bob", "pre", "2026-03-02T00:00:00+00:00"),
                    ("https://github.com/org/repo/pull/12", "org/repo", 12, "b2", "2026-03-01T11:30:00+00:00", "2026-03-01T11:30:00+00:00", "bob", "bob", "post", "2026-03-02T00:00:00+00:00"),
                ],
            )

            db.conn.commit()
            db.close()

            old_db_file = manage.DB_FILE
            manage.DB_FILE = db_path
            try:
                all_summary = manage.get_pr_commit_timing_summary("all_activity")
                delivery_summary = manage.get_pr_commit_timing_summary("delivery_only")
                all_outliers = manage.get_pr_post_open_commit_outliers(10, "all_activity")
                delivery_outliers = manage.get_pr_post_open_commit_outliers(10, "delivery_only")
                details = manage.get_pr_commit_timing_details(20, "all_activity")
            finally:
                manage.DB_FILE = old_db_file

            self.assertEqual(all_summary["coverage_num"], 3)
            self.assertEqual(all_summary["coverage_den"], 4)
            self.assertAlmostEqual(all_summary["post_ratio"], 0.6, places=4)

            self.assertEqual(delivery_summary["coverage_num"], 2)
            self.assertEqual(delivery_summary["coverage_den"], 3)
            self.assertAlmostEqual(delivery_summary["post_ratio"], 0.5, places=4)

            all_outlier_prs = {row[0] for row in all_outliers}
            delivery_outlier_prs = {row[0] for row in delivery_outliers}
            self.assertIn(14, all_outlier_prs)
            self.assertNotIn(14, delivery_outlier_prs)

            conf_by_pr = {row[0]: row[7] for row in details}
            self.assertEqual(conf_by_pr[11], "HIGH")
            self.assertEqual(conf_by_pr[12], "MED")
            self.assertEqual(conf_by_pr[13], "LOW")


if __name__ == "__main__":
    unittest.main()
