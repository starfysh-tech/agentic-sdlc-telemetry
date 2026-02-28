#!/usr/bin/env python3
"""agentic-sdlc-telemetry — management TUI

Run with: python3 manage.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

# ── Bootstrap dependencies ─────────────────────────────────

def _bootstrap():
    missing = [pkg for pkg in ("questionary", "rich") if not _importable(pkg)]
    if not missing:
        return
    print(f"Installing required packages: {', '.join(missing)} ...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *missing]
    )
    print()

def _importable(pkg: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(pkg) is not None

_bootstrap()

import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ── Constants ──────────────────────────────────────────────

INSTALL_DIR   = Path.home() / ".claude" / "usage-data" / "sdlc-analytics"
SCRIPT        = INSTALL_DIR / "sdlc_extract.py"
CONFIG_FILE   = INSTALL_DIR / "config.json"
DB_FILE       = INSTALL_DIR / "sdlc_analytics.db"
SOURCE_SCRIPT = Path(__file__).parent / "sdlc_extract.py"
PROJECTS_BASE = Path.home() / ".claude" / "projects"
REMOTE_URL    = (
    "https://raw.githubusercontent.com/starfysh-tech/"
    "agentic-sdlc-telemetry/main/sdlc_extract.py"
)

console = Console()

# ── Config ─────────────────────────────────────────────────

def read_config() -> list[str]:
    if not CONFIG_FILE.exists():
        return []
    try:
        return json.loads(CONFIG_FILE.read_text()).get("include_bases", [])
    except (json.JSONDecodeError, OSError):
        return []

def write_config(bases: list[str]):
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"include_bases": bases}, indent=2))

# ── Project Discovery ──────────────────────────────────────

def _display_name(dir_name: str) -> str:
    username = os.environ.get("USER", "")
    for prefix in (f"-Users-{username}-Code-", f"-Users-{username}-"):
        if dir_name.startswith(prefix):
            return dir_name[len(prefix):]
    return dir_name

def get_project_groups() -> list[dict]:
    """Identify base project dirs and their plugin variants."""
    if not PROJECTS_BASE.exists():
        return []
    all_names = [d.name for d in sorted(PROJECTS_BASE.iterdir()) if d.is_dir()]
    groups = []
    for name in all_names:
        if any(name != o and name.startswith(o + "-") for o in all_names):
            continue  # it's a plugin variant
        variants = sum(1 for o in all_names if o.startswith(name + "-"))
        groups.append({"name": name, "display": _display_name(name), "variants": variants})
    return groups

def resolve_dirs(bases: list[str]) -> list[Path]:
    """Expand base names to actual dirs including plugin variants."""
    if not PROJECTS_BASE.exists():
        return []
    dirs = []
    for base in bases:
        for d in sorted(PROJECTS_BASE.iterdir()):
            if d.is_dir() and (d.name == base or d.name.startswith(base + "-")):
                dirs.append(d)
    return dirs

# ── Status ─────────────────────────────────────────────────

def get_db_stats() -> dict:
    stats: dict = {}
    if not DB_FILE.exists():
        return stats
    try:
        conn = sqlite3.connect(DB_FILE)
        stats["main"]     = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=0").fetchone()[0]
        stats["sub"]      = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
        stats["git_ops"]  = conn.execute("SELECT COUNT(*) FROM git_operations").fetchone()[0]
        stats["prs"]      = conn.execute("SELECT COUNT(*) FROM pr_links").fetchone()[0]
        row = conn.execute("SELECT value FROM extraction_meta WHERE key='last_run'").fetchone()
        stats["last_run"] = row[0] if row else None
        conn.close()
    except Exception:
        pass
    return stats

def show_header():
    t = Table(box=None, padding=(0, 2, 0, 0), show_header=False, expand=False)
    t.add_column(style="dim", min_width=12)
    t.add_column()

    if SCRIPT.exists():
        t.add_row("Script", "[green]✓ installed[/green]")
    else:
        t.add_row("Script", "[red]✗ not installed[/red]")

    stats = get_db_stats()
    if "main" in stats:
        t.add_row("Sessions", f"[bold]{stats['main']:,}[/bold] main  ·  [bold]{stats['sub']:,}[/bold] subagent")
        t.add_row("Git ops",  f"[bold]{stats['git_ops']:,}[/bold]  ·  PRs [bold]{stats['prs']:,}[/bold]")
        t.add_row("Last run", stats["last_run"] or "[dim]never[/dim]")
    elif DB_FILE.exists():
        t.add_row("Database", "[dim]exists[/dim]")
    else:
        t.add_row("Database", "[dim]not yet created[/dim]")

    cfg = read_config()
    if cfg:
        t.add_row("Projects", f"[green]{len(cfg)} included[/green]")
    else:
        t.add_row("Projects", "[yellow]not configured[/yellow]")

    console.print(Panel(t, title="[bold]agentic-sdlc-telemetry[/bold]",
                        border_style="blue", padding=(1, 2)))

# ── Actions ────────────────────────────────────────────────

def action_configure():
    groups = get_project_groups()
    if not groups:
        console.print("[red]No projects found in ~/.claude/projects/[/red]")
        return

    current = set(read_config())
    choices = [
        questionary.Choice(
            title=(
                g["display"]
                + (f"  [dim]+{g['variants']} plugin dir(s)[/dim]" if g["variants"] else "")
            ),
            value=g["name"],
            checked=g["name"] in current,
        )
        for g in groups
    ]

    selected = questionary.checkbox("Select projects to include:", choices=choices).ask()
    if selected is None:
        return
    if not selected:
        console.print("[yellow]No projects selected — configuration unchanged.[/yellow]")
        return

    write_config(selected)
    console.print(f"[green]✓ Saved: {len(selected)} project(s) included[/green]")

def action_install():
    if sys.version_info < (3, 9):
        console.print(
            f"[red]Python 3.9+ required "
            f"(found {sys.version_info.major}.{sys.version_info.minor})[/red]"
        )
        return
    if not SOURCE_SCRIPT.exists():
        console.print("[red]sdlc_extract.py not found alongside manage.py[/red]")
        return

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_SCRIPT, SCRIPT)
    console.print(f"[green]✓ Script installed to {SCRIPT}[/green]\n")
    action_configure()

def action_update():
    console.print("[dim]Fetching latest from GitHub...[/dim]")
    try:
        with urllib.request.urlopen(REMOTE_URL, timeout=15) as r:
            content = r.read()
    except Exception as e:
        console.print(f"[red]Download failed: {e}[/red]")
        return

    if not content.lstrip().startswith(b"#!"):
        console.print("[red]Downloaded content does not look like a Python script[/red]")
        return

    tmp = INSTALL_DIR / ".sdlc_extract.tmp"
    tmp.write_bytes(content)
    tmp.replace(SCRIPT)
    SCRIPT.chmod(0o755)
    console.print("[green]✓ Updated[/green]")

def action_run():
    bases = read_config()
    if not bases:
        console.print("[yellow]No projects configured — run Configure first.[/yellow]")
        return
    dirs = resolve_dirs(bases)
    if not dirs:
        console.print("[red]No project directories found for configured bases.[/red]")
        return

    console.print()
    subprocess.run(
        [sys.executable, str(SCRIPT), "-v", "--project-dirs", *[str(d) for d in dirs]]
    )

def action_uninstall():
    for f in (SCRIPT, CONFIG_FILE):
        if f.exists():
            f.unlink()
            console.print(f"[green]✓ Removed {f.name}[/green]")

    if DB_FILE.exists():
        if questionary.confirm(f"Remove database ({DB_FILE.name})?", default=False).ask():
            DB_FILE.unlink()
            for suffix in ("-shm", "-wal"):
                sib = DB_FILE.parent / (DB_FILE.name + suffix)
                if sib.exists():
                    sib.unlink()
            console.print("[green]✓ Database removed[/green]")
        else:
            console.print("[dim]Database preserved.[/dim]")

    if INSTALL_DIR.exists() and not any(INSTALL_DIR.iterdir()):
        INSTALL_DIR.rmdir()
        console.print("[green]✓ Removed empty install directory[/green]")

# ── Main Menu ──────────────────────────────────────────────

def main():
    try:
        _main_loop()
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")

def _main_loop():
    while True:
        console.clear()
        show_header()

        installed = SCRIPT.exists()

        if installed:
            choices = [
                questionary.Choice("Run extraction",    value="run"),
                questionary.Choice("Configure projects", value="config"),
                questionary.Choice("Update script",      value="update"),
                questionary.Separator(),
                questionary.Choice("Uninstall",          value="uninstall"),
                questionary.Separator(),
                questionary.Choice("Quit",               value="quit"),
            ]
        else:
            choices = [
                questionary.Choice("Install", value="install"),
                questionary.Separator(),
                questionary.Choice("Quit", value="quit"),
            ]

        choice = questionary.select("What would you like to do?", choices=choices).ask()

        if choice is None or choice == "quit":
            break

        console.print()

        if choice == "install":
            action_install()
        elif choice == "run":
            action_run()
        elif choice == "config":
            action_configure()
        elif choice == "update":
            action_update()
        elif choice == "uninstall":
            if questionary.confirm("Uninstall agentic-sdlc-telemetry?", default=False).ask():
                action_uninstall()
                break

        console.print()
        questionary.press_any_key_to_continue("  Press any key to continue...").ask()

if __name__ == "__main__":
    main()
