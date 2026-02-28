#!/usr/bin/env python3
"""agentic-sdlc-telemetry — management TUI

Install:  pipx install git+https://github.com/starfysh-tech/agentic-sdlc-telemetry
Run:      sdlc-telemetry
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen

from textual import work
from textual.app import App, ComposeResult
from textual.color import Color  # noqa: F401
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive  # noqa: F401
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Label, OptionList, RichLog, Static  # noqa: F401
from textual.widgets import SelectionList
from textual.widgets.selection_list import Selection

# ── Constants ──────────────────────────────────────────────

try:
    __version__ = _pkg_version("agentic-sdlc-telemetry")
except PackageNotFoundError:
    __version__ = "dev"

DATA_DIR       = Path.home() / ".claude" / "usage-data" / "sdlc-analytics"
CONFIG_FILE    = DATA_DIR / "config.json"
DB_FILE        = DATA_DIR / "sdlc_analytics.db"
EXTRACT_SCRIPT = Path(__file__).parent / "sdlc_extract.py"
PROJECTS_BASE  = Path.home() / ".claude" / "projects"
PACKAGE_URL    = "git+https://github.com/starfysh-tech/agentic-sdlc-telemetry.git"

# ── Config ─────────────────────────────────────────────────

def read_config() -> list[str]:
    if not CONFIG_FILE.exists():
        return []
    try:
        data = json.loads(CONFIG_FILE.read_text())
        # support old key name
        return data.get("include_dirs") or data.get("include_bases", [])
    except (json.JSONDecodeError, OSError):
        return []

def write_config(dirs: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"include_dirs": dirs}, indent=2))

# ── Project Discovery ──────────────────────────────────────

def _peek_cwd(project_dir: Path) -> str | None:
    """Return the cwd value from the first JSONL file in project_dir, or None."""
    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    try:
        with jsonl_files[0].open() as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                    cwd = obj.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return None

def _display_from_cwd(cwd: str) -> str:
    home = str(Path.home())
    code_prefix = home + "/Code/"
    if cwd.startswith(code_prefix):
        return cwd[len(code_prefix):]
    if cwd == home + "/Code":
        return "~/Code"
    if cwd.startswith(home + "/"):
        return "~/" + cwd[len(home) + 1:]
    if cwd == home:
        return "~"
    return cwd

def get_project_list() -> list[dict]:
    """Return dirs under ~/.claude/projects/ grouped by display-name prefix.

    Dirs whose display name starts with another's display name + '-' are treated
    as plugin variants of the same project and merged into a single entry.
    """
    if not PROJECTS_BASE.exists():
        return []
    try:
        entries = list(PROJECTS_BASE.iterdir())
    except OSError:
        return []
    raw = []
    for d in entries:
        if not d.is_dir():
            continue
        jsonl_count = sum(1 for _ in d.glob("*.jsonl"))
        if jsonl_count == 0:
            continue
        cwd = _peek_cwd(d)
        display = _display_from_cwd(cwd) if cwd else d.name
        raw.append({"name": d.name, "display": display, "sessions": jsonl_count})

    raw.sort(key=lambda p: p["display"].lower())

    groups: list[dict] = []
    absorbed: set[str] = set()
    for p in raw:
        if p["name"] in absorbed:
            continue
        variant_dirs = [p["name"]]
        total_sessions = p["sessions"]
        for other in raw:
            if other["name"] == p["name"] or other["name"] in absorbed:
                continue
            if other["display"].startswith(p["display"] + "-"):
                variant_dirs.append(other["name"])
                total_sessions += other["sessions"]
                absorbed.add(other["name"])
        groups.append({
            "name": p["name"],
            "display": p["display"],
            "sessions": total_sessions,
            "dirs": variant_dirs,
        })
    return sorted(groups, key=lambda g: g["display"].lower())

def resolve_dirs(names: list[str]) -> list[Path]:
    """Resolve config dir names to actual paths (exact match only)."""
    if not PROJECTS_BASE.exists():
        return []
    try:
        all_dirs = {d.name: d for d in PROJECTS_BASE.iterdir() if d.is_dir()}
    except OSError:
        return []
    return [all_dirs[n] for n in names if n in all_dirs]

# ── Status ─────────────────────────────────────────────────

def get_db_stats() -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            stats: dict = {}
            stats["main"]    = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=0").fetchone()[0]
            stats["sub"]     = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
            stats["git_ops"] = conn.execute("SELECT COUNT(*) FROM git_operations").fetchone()[0]
            stats["prs"]     = conn.execute("SELECT COUNT(*) FROM pr_links").fetchone()[0]
            row = conn.execute("SELECT value FROM extraction_meta WHERE key='last_run'").fetchone()
            stats["last_run"] = row[0] if row else None
            return stats
    except sqlite3.Error:
        return {}

# ── TUI Widgets ────────────────────────────────────────────

class AnimatedBanner(Static):
    DEFAULT_CSS = "AnimatedBanner { height: auto; padding: 1 2; text-align: center; }"

    BANNER_LINES = [
        "╔═╗ ╔╦╗ ╦  ╔═╗   ╔╦╗╔═╗╦  ╔═╗╔╦╗╔═╗╔╦╗╦═╗╦ ╦",
        "╚═╗  ║║ ║  ║      ║ ║╣ ║  ║╣ ║║║║╣  ║ ╠╦╝╚╦╝",
        "╚═╝ ═╩╝ ╩═╝╚═╝   ╩ ╚═╝╩═╝╚═╝╩ ╩╚═╝ ╩ ╩╚═ ╩ ",
    ]
    PALETTE = ["cyan", "dodgerblue", "mediumpurple", "magenta", "mediumpurple", "dodgerblue"]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._phase = 0

    def on_mount(self) -> None:
        self._render_banner()
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % len(self.PALETTE)
        self._render_banner()

    def _render_banner(self) -> None:
        n = len(self.PALETTE)
        lines = []
        for line in self.BANNER_LINES:
            colored = ""
            for i, ch in enumerate(line):
                idx = (self._phase + i) % n
                color = self.PALETTE[idx]
                colored += f"[{color}]{ch}[/{color}]"
            lines.append(colored)
        self.update("\n".join(lines))


class StatusSidebar(Static):
    def refresh_stats(self) -> None:
        stats = get_db_stats()
        cfg = read_config()
        projects = get_project_list()

        if DB_FILE.exists():
            size = DB_FILE.stat().st_size
            db_size = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.1f} KB"
        else:
            db_size = "not yet created"

        if stats:
            status_lines = (
                f" Sessions\n"
                f"   [bold]{stats.get('main', 0):,}[/bold] main\n"
                f"   [bold]{stats.get('sub', 0):,}[/bold] subagent\n"
                f" Git Activity\n"
                f"   [bold]{stats.get('git_ops', 0):,}[/bold] ops\n"
                f"   [bold]{stats.get('prs', 0):,}[/bold] PRs\n"
                f" Last Run\n"
                f"   {stats.get('last_run') or '[dim]never[/dim]'}\n"
                f" Projects\n"
                f"   {len(cfg)} configured\n"
                f"   {len(projects)} available\n"
                f" Database\n"
                f"   {db_size}"
            )
        else:
            status_lines = (
                f" Database\n"
                f"   [dim]{db_size}[/dim]\n"
                f" Projects\n"
                f"   {len(cfg)} configured\n"
                f"   {len(projects)} available"
            )

        system_lines = (
            f" v{__version__}\n"
            f" Python {sys.version.split()[0]}\n"
            f" {DATA_DIR}"
        )

        self.update(
            f"[bold]─ Status ─[/bold]\n{status_lines}\n\n"
            f"[bold]─ System ─[/bold]\n{system_lines}"
        )


class ConfirmDialog(ModalScreen[bool]):
    def __init__(self, message: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class SubprocessScreen(Screen):
    def __init__(self, cmd: list[str], title: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cmd = cmd
        self._title = title
        self._done = False
        self._proc: Popen | None = None
        self._start_time: float = 0.0

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        with Horizontal():
            with Vertical(id="main-content"):
                yield RichLog(id="log", auto_scroll=True)
            with Vertical(id="sidebar"):
                yield StatusSidebar(id="status-sidebar")
        yield Footer()

    def on_mount(self) -> None:
        self._start_time = time.monotonic()
        self._run_subprocess()

    @work(thread=True)
    def _run_subprocess(self) -> None:
        log = self.query_one(RichLog)
        sidebar = self.query_one(StatusSidebar)

        try:
            self.call_from_thread(sidebar.update, "Running...")
        except Exception:
            return

        try:
            proc = Popen(
                self._cmd,
                stdout=PIPE,
                stderr=STDOUT,
                text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._proc = proc

            if proc.stdout is not None:
                for line in proc.stdout:
                    try:
                        self.call_from_thread(log.write, line.rstrip())
                    except Exception:
                        break

            proc.wait()
            elapsed = time.monotonic() - self._start_time

            if proc.returncode != 0:
                try:
                    self.call_from_thread(
                        log.write,
                        f"\n[red]Process exited with code {proc.returncode}[/red]",
                    )
                except Exception:
                    pass

            status = f"{'Done' if proc.returncode == 0 else 'Failed'} ({elapsed:.1f}s)"
            try:
                self.call_from_thread(sidebar.update, status)
                self.call_from_thread(log.write, "\nPress any key to return")
            except Exception:
                pass

        except Exception as exc:
            try:
                self.call_from_thread(log.write, f"[red]Error: {exc}[/red]")
                self.call_from_thread(log.write, "\nPress any key to return")
            except Exception:
                pass

        self._done = True

    def on_unmount(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()

    def on_key(self) -> None:
        if self._done:
            self.app.pop_screen()


class ConfigureScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("c", "copy_name", "Copy name"),
    ]

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        with Horizontal():
            with Vertical(id="main-content"):
                yield SelectionList(id="project-list")
                with Horizontal(id="button-bar"):
                    yield Button("Save", id="save", variant="primary")
                    yield Button("Cancel", id="cancel")
            with Vertical(id="sidebar"):
                yield StatusSidebar(id="status-sidebar")
        yield Footer()

    def on_mount(self) -> None:
        projects = get_project_list()
        if not projects:
            self.notify("No projects found in ~/.claude/projects/")
            self.app.pop_screen()
            return
        current = set(read_config())
        self._project_dirs: dict[str, list[str]] = {
            p["name"]: p.get("dirs", [p["name"]]) for p in projects
        }
        self._display_map: dict[str, str] = {p["name"]: p["display"] for p in projects}
        sl = self.query_one(SelectionList)
        for p in projects:
            dirs = p.get("dirs", [p["name"]])
            n_dirs = len(dirs)
            if n_dirs > 1:
                label = f"{p['display']} ({p['sessions']} sessions, {n_dirs} dirs)"
            else:
                label = f"{p['display']} ({p['sessions']})"
            checked = any(d in current for d in dirs)
            sl.add_option(Selection(label, p["name"], checked))
        self.query_one(StatusSidebar).refresh_stats()

    def action_copy_name(self) -> None:
        sl = self.query_one(SelectionList)
        if sl.highlighted is None:
            return
        try:
            option = sl.get_option_at_index(sl.highlighted)
        except Exception:
            return
        display = self._display_map.get(str(option.value), str(option.prompt).rsplit(" (", 1)[0])
        self.app.copy_to_clipboard(display)
        self.notify(f"Copied: {display}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            selected_bases = list(self.query_one(SelectionList).selected)
            if not selected_bases:
                self.notify("No projects selected — configuration unchanged.")
                return
            all_dirs: list[str] = []
            seen: set[str] = set()
            for base in selected_bases:
                for d in self._project_dirs.get(base, [base]):
                    if d not in seen:
                        all_dirs.append(d)
                        seen.add(d)
            write_config(all_dirs)
            self.notify(f"Saved: {len(selected_bases)} project(s) included")
            self.app.pop_screen()
        elif event.button.id == "cancel":
            self.app.pop_screen()


class DashboardScreen(Screen):
    BINDINGS = [
        ("r", "run", "Run"),
        ("c", "configure", "Configure"),
        ("u", "update", "Update"),
        ("x", "uninstall", "Uninstall"),
        ("q", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        with Horizontal():
            with Vertical(id="main-content"):
                yield OptionList(
                    "Run extraction",
                    "Configure projects",
                    "Update",
                    "Uninstall",
                    id="menu",
                )
            with Vertical(id="sidebar"):
                yield StatusSidebar(id="status-sidebar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(StatusSidebar).refresh_stats()

    def on_screen_resume(self) -> None:
        self.query_one(StatusSidebar).refresh_stats()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        actions = [
            self.action_run,
            self.action_configure,
            self.action_update,
            self.action_uninstall,
        ]
        if 0 <= event.option_index < len(actions):
            actions[event.option_index]()

    def action_run(self) -> None:
        bases = read_config()
        if not bases:
            self.notify("No projects configured — run Configure first.")
            return
        dirs = resolve_dirs(bases)
        if not dirs:
            self.notify("No project directories found for configured bases.", severity="error")
            return
        if not EXTRACT_SCRIPT.exists():
            self.notify(f"Cannot find {EXTRACT_SCRIPT}", severity="error")
            return
        cmd = [
            sys.executable, "-u", str(EXTRACT_SCRIPT), "-v",
            "--project-dirs", *[str(d) for d in dirs],
        ]
        self.app.push_screen(SubprocessScreen(cmd, "Extraction"))

    def action_configure(self) -> None:
        self.app.push_screen(ConfigureScreen())

    def action_update(self) -> None:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_URL]
        self.app.push_screen(SubprocessScreen(cmd, "Update"))

    def action_uninstall(self) -> None:
        self.do_uninstall()

    def action_quit_app(self) -> None:
        self.app.exit()

    @work
    async def do_uninstall(self) -> None:
        if not await self.app.push_screen_wait(ConfirmDialog("Remove config and data?")):
            return
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        if DB_FILE.exists():
            if await self.app.push_screen_wait(ConfirmDialog(f"Remove database ({DB_FILE.name})?")):
                DB_FILE.unlink()
                for suffix in ("-shm", "-wal"):
                    sib = DB_FILE.parent / (DB_FILE.name + suffix)
                    if sib.exists():
                        sib.unlink()
        self.notify("Uninstall complete. Run: pipx uninstall agentic-sdlc-telemetry")
        self.app.exit()


class SdlcApp(App):
    CSS = """
    AnimatedBanner {
        height: auto;
        padding: 1 2;
        text-align: center;
    }
    #main-content {
        width: 3fr;
    }
    #sidebar {
        width: 1fr;
        border-left: solid $accent;
        padding: 0 1;
    }
    ConfirmDialog {
        align: center middle;
    }
    ConfirmDialog > Vertical {
        width: 40;
        height: auto;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }
    #dialog-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: center;
    }
    Button {
        width: 10;
        margin: 0 1;
    }
    OptionList {
        height: 1fr;
    }
    SelectionList {
        height: 1fr;
    }
    #button-bar {
        height: auto;
        margin-top: 1;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen())

# ── Entry Point ────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        prog="sdlc-telemetry",
        description="SDLC session analytics for Claude Code",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.parse_args()
    SdlcApp().run()

if __name__ == "__main__":
    main()
