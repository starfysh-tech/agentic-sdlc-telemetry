#!/usr/bin/env python3
"""agentic-sdlc-telemetry — management TUI

Install:  pipx install git+https://github.com/starfysh-tech/agentic-sdlc-telemetry
Run:      sdlc-telemetry
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ── Constants ──────────────────────────────────────────────

try:
    __version__ = _pkg_version("agentic-sdlc-telemetry")
except PackageNotFoundError:
    __version__ = "dev"

DATA_DIR      = Path.home() / ".claude" / "usage-data" / "sdlc-analytics"
CONFIG_FILE   = DATA_DIR / "config.json"
DB_FILE       = DATA_DIR / "sdlc_analytics.db"
EXTRACT_SCRIPT = Path(__file__).parent / "sdlc_extract.py"
PROJECTS_BASE = Path.home() / ".claude" / "projects"
PACKAGE_URL   = "git+https://github.com/starfysh-tech/agentic-sdlc-telemetry.git"

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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    # Compare display names so that a home-dir encoding (-Users-username) can't
    # absorb unrelated projects as "plugin variants".
    display_of = {name: _display_name(name) for name in all_names}
    all_displays = list(display_of.values())
    groups = []
    for name in all_names:
        display = display_of[name]
        if any(display != d and display.startswith(d + "-") for d in all_displays):
            continue  # plugin variant — shown under its base
        variants = sum(1 for d in all_displays if d.startswith(display + "-"))
        groups.append({"name": name, "display": display, "variants": variants})
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
        stats["main"]    = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=0").fetchone()[0]
        stats["sub"]     = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
        stats["git_ops"] = conn.execute("SELECT COUNT(*) FROM git_operations").fetchone()[0]
        stats["prs"]     = conn.execute("SELECT COUNT(*) FROM pr_links").fetchone()[0]
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

    stats = get_db_stats()
    if "main" in stats:
        t.add_row("Sessions", f"[bold]{stats['main']:,}[/bold] main  ·  [bold]{stats['sub']:,}[/bold] subagent")
        t.add_row("Git ops",  f"[bold]{stats['git_ops']:,}[/bold]  ·  PRs [bold]{stats['prs']:,}[/bold]")
        t.add_row("Last run", stats["last_run"] or "[dim]never[/dim]")
    else:
        t.add_row("Database", "[dim]not yet created — run extraction first[/dim]")

    cfg = read_config()
    if cfg:
        t.add_row("Projects", f"[green]{len(cfg)} included[/green]")
    else:
        t.add_row("Projects", "[yellow]not configured[/yellow]")

    console.print(Panel(
        t,
        title=f"[bold]agentic-sdlc-telemetry[/bold] [dim]v{__version__}[/dim]",
        border_style="blue",
        padding=(1, 2),
    ))

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
                + (f"  (+{g['variants']} plugin dir(s))" if g["variants"] else "")
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

def action_run():
    bases = read_config()
    if not bases:
        console.print("[yellow]No projects configured — run Configure first.[/yellow]")
        return
    dirs = resolve_dirs(bases)
    if not dirs:
        console.print("[red]No project directories found for configured bases.[/red]")
        return
    if not EXTRACT_SCRIPT.exists():
        console.print(f"[red]Cannot find {EXTRACT_SCRIPT}[/red]")
        return

    console.print()
    subprocess.run(
        [sys.executable, str(EXTRACT_SCRIPT), "-v", "--project-dirs", *[str(d) for d in dirs]]
    )

def action_update():
    console.print(f"[dim]Upgrading from {PACKAGE_URL} ...[/dim]\n")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_URL]
    )
    if result.returncode == 0:
        console.print("\n[green]✓ Updated — restart sdlc-telemetry to use the new version[/green]")
    else:
        console.print("\n[red]Update failed[/red]")
        console.print("[dim]pipx users: pipx upgrade agentic-sdlc-telemetry[/dim]")
        console.print("[dim]brew users: brew upgrade agentic-sdlc-telemetry[/dim]")

def action_uninstall():
    for f in (CONFIG_FILE,):
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

    if DATA_DIR.exists() and not any(DATA_DIR.iterdir()):
        DATA_DIR.rmdir()

    console.print("\nTo remove the command:")
    console.print("  [bold]pipx uninstall agentic-sdlc-telemetry[/bold]")
    console.print("  [dim]or: pip uninstall agentic-sdlc-telemetry[/dim]")
    console.print("  [dim]or: brew uninstall agentic-sdlc-telemetry[/dim]")

# ── Main Menu ──────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(
        prog="sdlc-telemetry",
        description="SDLC session analytics for Claude Code",
        add_help=True,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.parse_args()  # exits for --version / --help; no other args accepted

    try:
        _main_loop()
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")

def _main_loop():
    while True:
        console.clear()
        show_header()

        choices = [
            questionary.Choice("[r] Run extraction",     value="run",       shortcut_key="r"),
            questionary.Choice("[c] Configure projects", value="config",    shortcut_key="c"),
            questionary.Choice("[u] Update",             value="update",    shortcut_key="u"),
            questionary.Separator(),
            questionary.Choice("[x] Uninstall",          value="uninstall", shortcut_key="x"),
            questionary.Separator(),
            questionary.Choice("[q] Quit",               value="quit",      shortcut_key="q"),
        ]

        choice = questionary.select(
            "What would you like to do?  (↑↓ arrows, or press key)",
            choices=choices,
        ).ask()

        if choice is None or choice == "quit":
            break

        console.print()

        if choice == "run":
            action_run()
        elif choice == "config":
            action_configure()
        elif choice == "update":
            action_update()
        elif choice == "uninstall":
            if questionary.confirm("Remove config and data?", default=False).ask():
                action_uninstall()
                break

        console.print()
        questionary.press_any_key_to_continue("  Press any key to continue...").ask()

if __name__ == "__main__":
    main()
