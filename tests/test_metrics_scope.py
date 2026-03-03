import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import manage
from sdlc_extract import DB


class MetricsScopeTests(unittest.TestCase):
    def test_all_activity_vs_delivery_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metrics.db"
            db = DB(db_path)
            now = datetime.now(timezone.utc)
            ts_main = (now - timedelta(days=2)).isoformat()
            ts_sub = (now - timedelta(days=1)).isoformat()
            ts_pr_open = (now - timedelta(days=1, hours=20)).isoformat()
            ts_pr_merged = (now - timedelta(hours=2)).isoformat()

            db.conn.execute(
                "INSERT INTO sessions (session_id,is_subagent,first_timestamp,git_branch,source_file,file_size_bytes,file_mtime) VALUES (?,?,?,?,?,?,?)",
                ("s_main", 0, ts_main, "feat/test", "m", 1, 1.0),
            )
            db.conn.execute(
                "INSERT INTO sessions (session_id,is_subagent,first_timestamp,git_branch,source_file,file_size_bytes,file_mtime) VALUES (?,?,?,?,?,?,?)",
                ("s_sub", 1, ts_sub, "feat/test", "s", 1, 1.0),
            )
            db.conn.execute(
                "INSERT INTO git_operations (session_id,timestamp,git_op_type) VALUES (?,?,?)",
                ("s_main", ts_main, "commit"),
            )
            db.conn.execute(
                "INSERT INTO git_operations (session_id,timestamp,git_op_type) VALUES (?,?,?)",
                ("s_sub", ts_sub, "commit"),
            )
            db.conn.execute(
                "INSERT INTO git_operations (session_id,timestamp,git_op_type) VALUES (?,?,?)",
                ("s_main", ts_main, "push"),
            )
            db.conn.execute(
                "INSERT INTO pr_links (session_id,pr_number,pr_url,pr_repository,timestamp) VALUES (?,?,?,?,?)",
                ("s_main", 12, "https://github.com/org/repo/pull/12", "org/repo", ts_main),
            )
            db.conn.execute(
                "INSERT INTO pr_facts (pr_url,repo_full_name,pr_number,state,is_merged,opened_at,closed_at,merged_at,author_login,base_branch,head_branch,last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "https://github.com/org/repo/pull/12",
                    "org/repo",
                    12,
                    "closed",
                    1,
                    ts_pr_open,
                    ts_pr_merged,
                    ts_pr_merged,
                    "alice",
                    "main",
                    "feat/test",
                    now.isoformat(),
                ),
            )
            db.conn.commit()
            db.close()

            old_db_file = manage.DB_FILE
            manage.DB_FILE = db_path
            try:
                all_metrics = manage.get_velocity_banner("all_activity")
                delivery_metrics = manage.get_velocity_banner("delivery_only")
            finally:
                manage.DB_FILE = old_db_file

            self.assertGreater(all_metrics["commits_day"], delivery_metrics["commits_day"])
            self.assertEqual(delivery_metrics["coverage_num"], 1)
            self.assertEqual(delivery_metrics["coverage_den"], 1)


if __name__ == "__main__":
    unittest.main()
