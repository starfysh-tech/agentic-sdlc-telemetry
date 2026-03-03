import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import manage


def _args(**overrides):
    base = {
        "command": "stats",
        "scope": "all_activity",
        "limit": 10,
        "repos": None,
        "projects": None,
        "full": False,
        "enrich": False,
        "github_token_env": "GITHUB_TOKEN",
        "github_max_prs": 500,
        "yes": False,
        "remove_db": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class AICommandTests(unittest.TestCase):
    def test_stats_command_envelope(self) -> None:
        with patch("manage.get_cli_snapshot", return_value={"sample": 1}), patch(
            "manage.get_cli_tui_parity", return_value={"tui": 1}
        ):
            res = manage.run_ai_command(_args(command="stats", repos=["org/repo"]))
        self.assertTrue(res["ok"])
        self.assertEqual(res["command"], "stats")
        self.assertIn("repo_snapshot", res["data"])
        self.assertIn("tui_parity", res["data"])

    def test_config_set_command(self) -> None:
        with patch(
            "manage.get_project_list",
            return_value=[{"name": "p1", "display": "org/repo", "sessions": 1, "dirs": ["p1"]}],
        ), patch("manage.resolve_dirs", return_value=[Path("/tmp/p1")]), patch("manage.write_config") as write_cfg:
            res = manage.run_ai_command(_args(command="config.set", projects=["org/repo"]))
        self.assertTrue(res["ok"])
        write_cfg.assert_called_once_with(["p1"])
        self.assertEqual(res["data"]["include_dirs"], ["p1"])

    def test_extract_run_command_uses_config_when_no_projects(self) -> None:
        with patch("manage.read_config", return_value=["p1"]), patch(
            "manage.resolve_dirs", return_value=[Path("/tmp/p1")]
        ), patch("manage._perform_extraction", return_value={"ok": True, "stats": {"processed": 1}}):
            res = manage.run_ai_command(_args(command="extract.run"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["stats"]["processed"], 1)

    def test_uninstall_requires_yes(self) -> None:
        res = manage.run_ai_command(_args(command="uninstall", yes=False))
        self.assertFalse(res["ok"])
        self.assertIn("confirmation_required_use_yes", res["errors"])

    def test_uninstall_with_remove_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            db = root / "sdlc_analytics.db"
            wal = root / "sdlc_analytics.db-wal"
            shm = root / "sdlc_analytics.db-shm"
            cfg.write_text("{}")
            db.write_text("db")
            wal.write_text("wal")
            shm.write_text("shm")

            old_cfg = manage.CONFIG_FILE
            old_db = manage.DB_FILE
            try:
                manage.CONFIG_FILE = cfg
                manage.DB_FILE = db
                res = manage.run_ai_command(_args(command="uninstall", yes=True, remove_db=True))
            finally:
                manage.CONFIG_FILE = old_cfg
                manage.DB_FILE = old_db

            self.assertTrue(res["ok"])
            self.assertFalse(cfg.exists())
            self.assertFalse(db.exists())
            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())


if __name__ == "__main__":
    unittest.main()
