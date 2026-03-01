#!/usr/bin/env python3
"""agentic-sdlc-telemetry — management TUI

Install:  pipx install git+https://github.com/starfysh-tech/agentic-sdlc-telemetry
Run:      sdlc-t
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen

import pyfiglet
from rich.text import Text
from sdlc_extract import DB as ExtractDB, SessionExtractor
from textual import work
from textual.app import App, ComposeResult
from textual.color import Color  # noqa: F401
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive  # noqa: F401
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Label, OptionList, RichLog, Select, Static, TabbedContent, TabPane  # noqa: F401
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
GITHUB_API_URL = "https://api.github.com/repos/starfysh-tech/agentic-sdlc-telemetry/commits/main"

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

def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    h = m // 60
    if h < 24:
        return f"{h}h {m % 60:02d}m"
    d = h // 24
    return f"{d}d {h % 24}h"

def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

# ── Metric Thresholds (good, warn) ────────────────────────
_THRESH = {
    "prs_week":       (5, 2),      # higher-is-better
    "commits_day":    (3, 1),      # higher-is-better
    "lead_time_h":    (4, 24),     # lower-is-better
    "push_rate_pct":  (60, 40),    # higher-is-better (percentage scale)
    "productivity":   (40, 20),    # higher-is-better
    "unproductive_h": (0.17, 1),   # lower-is-better (<10m green, >1h red)
}

def _threshold_color(val, good, warn, *, lower_is_better=False) -> str:
    """Return hex color for metric value. None → empty string (no color)."""
    if val is None:
        return ""
    if lower_is_better:
        if val <= good: return "#00ff7f"
        if val <= warn: return "#ffa500"
        return "#ff5555"
    else:
        if val >= good: return "#00ff7f"
        if val >= warn: return "#ffa500"
        return "#ff5555"

def _colored(val_str: str, color: str) -> str:
    """Wrap val_str in bold+color markup, or plain bold if color is empty."""
    if color:
        return f"[bold {color}]{val_str}[/]"
    return f"[bold]{val_str}[/bold]"

def _lead_time_clamp_color(lead_h):
    """Return (clamped_hours, color) for a lead time value in hours."""
    clamped = max(lead_h, 0) if lead_h is not None else None
    color = _threshold_color(clamped, *_THRESH["lead_time_h"], lower_is_better=True)
    return clamped, color

def _phase_cell(phase: str) -> Text:
    """Return a Text cell with phase name styled using _PHASE_COLORS."""
    color = _PHASE_COLORS.get(phase, "")
    return Text(phase, style=f"bold {color}" if color else "")

def _fmt_relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "[dim]never[/dim]"
    from datetime import datetime, timezone
    try:
        ts = str(iso_str)  # guard against non-string input
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:    return "just now"
        if s < 3600:  return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except (ValueError, TypeError, AttributeError):
        return str(iso_str)[:19]

def get_velocity_banner() -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            prs_week = conn.execute("""
                SELECT COUNT(*) FROM git_operations
                WHERE git_op_type = 'pr_create'
                AND datetime(timestamp) >= datetime('now', '-7 days')
            """).fetchone()[0]
            commits_7d = conn.execute("""
                SELECT COUNT(*) FROM git_operations
                WHERE git_op_type = 'commit'
                AND datetime(timestamp) >= datetime('now', '-7 days')
            """).fetchone()[0]
            avg_lead = conn.execute("""
                WITH branch_first AS (
                    SELECT git_branch, MIN(first_timestamp) as first_session
                    FROM sessions
                    WHERE is_subagent = 0 AND git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_times AS (
                    SELECT s.git_branch, MIN(g.timestamp) as pr_created
                    FROM git_operations g
                    JOIN sessions s ON g.session_id = s.session_id
                    WHERE g.git_op_type = 'pr_create'
                    GROUP BY s.git_branch
                )
                SELECT AVG((julianday(pt.pr_created) - julianday(bf.first_session)) * 24)
                FROM pr_times pt
                JOIN branch_first bf ON pt.git_branch = bf.git_branch
            """).fetchone()[0]
            push_row = conn.execute("""
                SELECT
                    SUM(CASE WHEN git_op_type = 'push'   THEN 1 ELSE 0 END) * 1.0,
                    SUM(CASE WHEN git_op_type = 'commit' THEN 1 ELSE 0 END)
                FROM git_operations
            """).fetchone()
            push_rate = (push_row[0] or 0) / max(push_row[1] or 0, 1)
            return {
                "prs_week": prs_week,
                "commits_day": commits_7d / 7.0,
                "avg_lead_time_h": avg_lead or 0,
                "push_rate": push_rate,
            }
    except sqlite3.Error:
        return {}

def get_weekly_throughput(weeks: int = 8) -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                WITH weekly_ops AS (
                    SELECT
                        strftime('%Y-W%W', timestamp) as week,
                        SUM(CASE WHEN git_op_type = 'pr_create' THEN 1 ELSE 0 END) as prs_created,
                        SUM(CASE WHEN git_op_type = 'pr_merge'  THEN 1 ELSE 0 END) as prs_merged,
                        SUM(CASE WHEN git_op_type = 'commit'    THEN 1 ELSE 0 END) as commits
                    FROM git_operations
                    WHERE timestamp IS NOT NULL
                    GROUP BY week
                    ORDER BY week DESC
                    LIMIT ?
                ),
                branch_first AS (
                    SELECT git_branch, MIN(first_timestamp) as first_session
                    FROM sessions
                    WHERE is_subagent = 0 AND git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_times AS (
                    SELECT
                        s.git_branch,
                        strftime('%Y-W%W', g.timestamp) as pr_week,
                        MIN(g.timestamp) as pr_created
                    FROM git_operations g
                    JOIN sessions s ON g.session_id = s.session_id
                    WHERE g.git_op_type = 'pr_create'
                    GROUP BY s.git_branch
                ),
                lead_by_week AS (
                    SELECT
                        pt.pr_week as week,
                        AVG((julianday(pt.pr_created) - julianday(bf.first_session)) * 24) as avg_lead_h
                    FROM pr_times pt
                    JOIN branch_first bf ON pt.git_branch = bf.git_branch
                    GROUP BY pt.pr_week
                )
                SELECT w.week, w.prs_created, w.prs_merged, w.commits, COALESCE(l.avg_lead_h, 0)
                FROM weekly_ops w
                LEFT JOIN lead_by_week l ON w.week = l.week
                ORDER BY w.week DESC
            """, (weeks,)).fetchall()
    except sqlite3.Error:
        return []

def get_recent_prs(limit: int = 10) -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                SELECT
                    substr(pl.timestamp, 1, 10) as date,
                    pl.pr_number,
                    pl.pr_repository,
                    s.git_branch
                FROM pr_links pl
                LEFT JOIN sessions s ON pl.session_id = s.session_id
                ORDER BY pl.timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except sqlite3.Error:
        return []

def get_pr_lifecycle(limit: int = 15) -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                WITH branch_sessions AS (
                    SELECT
                        git_branch,
                        MIN(first_timestamp) as first_session,
                        COUNT(DISTINCT session_id) as sessions
                    FROM sessions
                    WHERE is_subagent = 0 AND git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_times AS (
                    SELECT s.git_branch, MIN(g.timestamp) as pr_created
                    FROM git_operations g
                    JOIN sessions s ON g.session_id = s.session_id
                    WHERE g.git_op_type = 'pr_create'
                    GROUP BY s.git_branch
                ),
                branch_commits AS (
                    SELECT s.git_branch, COUNT(*) as commits
                    FROM git_operations g
                    JOIN sessions s ON g.session_id = s.session_id
                    WHERE g.git_op_type = 'commit'
                    GROUP BY s.git_branch
                )
                SELECT
                    bs.git_branch,
                    (julianday(pt.pr_created) - julianday(bs.first_session)) * 24 as lead_time_h,
                    bs.sessions,
                    COALESCE(bc.commits, 0) as commits,
                    substr(bs.first_session, 1, 10) as first_session_date,
                    substr(pt.pr_created, 1, 10) as pr_created_date
                FROM branch_sessions bs
                JOIN pr_times pt ON bs.git_branch = pt.git_branch
                LEFT JOIN branch_commits bc ON bs.git_branch = bc.git_branch
                ORDER BY lead_time_h DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except sqlite3.Error:
        return []

def get_rework_hotspots(limit: int = 10) -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                WITH branch_sessions AS (
                    SELECT
                        git_branch,
                        COUNT(DISTINCT session_id) as sessions,
                        SUM(COALESCE(total_duration_ms, 0)) / 1000.0 as total_duration_s
                    FROM sessions
                    WHERE is_subagent = 0 AND git_branch IS NOT NULL
                    GROUP BY git_branch
                    HAVING sessions > 2
                ),
                branch_commits AS (
                    SELECT s.git_branch, COUNT(*) as commits
                    FROM git_operations g
                    JOIN sessions s ON g.session_id = s.session_id
                    WHERE g.git_op_type = 'commit' AND s.is_subagent = 0
                    GROUP BY s.git_branch
                )
                SELECT
                    bs.git_branch,
                    bs.sessions,
                    COALESCE(bc.commits, 0) as commits,
                    bs.total_duration_s,
                    CAST(COALESCE(bc.commits, 0) AS REAL) / bs.sessions as commits_per_session
                FROM branch_sessions bs
                LEFT JOIN branch_commits bc ON bs.git_branch = bc.git_branch
                ORDER BY bs.sessions DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except sqlite3.Error:
        return []

def get_efficiency_metrics() -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            total_main = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE is_subagent=0"
            ).fetchone()[0] or 1
            productive = conn.execute("""
                SELECT COUNT(DISTINCT s.session_id)
                FROM sessions s
                JOIN git_operations g ON g.session_id = s.session_id
                WHERE s.is_subagent = 0 AND g.git_op_type = 'commit'
            """).fetchone()[0]
            avg_dur = conn.execute(
                "SELECT AVG(total_duration_ms) FROM sessions WHERE is_subagent=0 AND total_duration_ms IS NOT NULL"
            ).fetchone()[0]
            sub_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
            main_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=0").fetchone()[0]
            total_commits = conn.execute("""
                SELECT COUNT(*) FROM git_operations g
                JOIN sessions s ON g.session_id = s.session_id
                WHERE s.is_subagent = 0 AND g.git_op_type = 'commit'
            """).fetchone()[0] or 1
            rows = conn.execute(
                "SELECT usage_by_model FROM sessions WHERE is_subagent=0 AND usage_by_model IS NOT NULL"
            ).fetchall()
            total_tokens = 0
            for (ubm,) in rows:
                try:
                    for md in json.loads(ubm).values():
                        total_tokens += md.get("input", 0) + md.get("output", 0)
                except (json.JSONDecodeError, AttributeError):
                    pass
            return {
                "productivity_rate": productive / total_main * 100,
                "tokens_per_commit": total_tokens / total_commits,
                "avg_duration_s": (avg_dur or 0) / 1000.0,
                "subagent_ratio": sub_count / max(main_count, 1),
            }
    except sqlite3.Error:
        return {}

def get_model_efficiency() -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            model_stats: dict[str, dict] = {}
            for model, cnt, avg_dur in conn.execute("""
                SELECT model, COUNT(*) as sessions, AVG(total_duration_ms)
                FROM sessions
                WHERE is_subagent = 0 AND model IS NOT NULL
                GROUP BY model ORDER BY sessions DESC
            """).fetchall():
                model_stats[model] = {
                    "sessions": cnt,
                    "avg_dur_s": (avg_dur or 0) / 1000.0,
                    "tokens": 0,
                    "commits": 0,
                }
            for model, commits in conn.execute("""
                SELECT s.model, COUNT(*) as commits
                FROM sessions s
                JOIN git_operations g ON g.session_id = s.session_id
                WHERE s.is_subagent = 0 AND g.git_op_type = 'commit' AND s.model IS NOT NULL
                GROUP BY s.model
            """).fetchall():
                if model in model_stats:
                    model_stats[model]["commits"] = commits
            for model, ubm in conn.execute("""
                SELECT model, usage_by_model FROM sessions
                WHERE is_subagent = 0 AND model IS NOT NULL AND usage_by_model IS NOT NULL
            """).fetchall():
                if model not in model_stats:
                    continue
                try:
                    for md in json.loads(ubm).values():
                        model_stats[model]["tokens"] += md.get("input", 0) + md.get("output", 0)
                except (json.JSONDecodeError, AttributeError):
                    pass
            result = []
            for model, s in sorted(model_stats.items(), key=lambda x: x[1]["sessions"], reverse=True):
                tpc = s["tokens"] / s["commits"] if s["commits"] > 0 else 0
                result.append((model, s["sessions"], s["avg_dur_s"], s["commits"], tpc))
            return result
    except sqlite3.Error:
        return []

def get_unproductive_sessions(limit: int = 10) -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                SELECT
                    substr(s.first_timestamp, 1, 10),
                    s.total_duration_ms / 1000.0,
                    s.model,
                    s.first_user_message
                FROM sessions s
                LEFT JOIN (
                    SELECT session_id FROM git_operations GROUP BY session_id
                ) gop ON gop.session_id = s.session_id
                WHERE s.is_subagent = 0
                    AND s.total_duration_ms > 60000
                    AND gop.session_id IS NULL
                ORDER BY s.total_duration_ms DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except sqlite3.Error:
        return []

def get_tool_usage_enhanced() -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute("""
                SELECT
                    tool_name,
                    SUM(call_count) as total_calls,
                    COUNT(DISTINCT session_id) as session_count,
                    AVG(call_count) as avg_per_session
                FROM session_tool_summary
                GROUP BY tool_name
                ORDER BY total_calls DESC
                LIMIT 30
            """).fetchall()
    except sqlite3.Error:
        return []

def get_aggregate_pipeline() -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            phase_rows = conn.execute("""
                SELECT e.phase, COUNT(*) AS total_calls,
                       COUNT(DISTINCT e.session_id) AS session_count,
                       SUM(e.duration_ms) AS total_ms
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE s.is_subagent = 0
                GROUP BY e.phase
            """).fetchall()
            tool_rows = conn.execute("""
                SELECT e.phase, e.tool_name, COUNT(*) AS cnt
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE s.is_subagent = 0
                GROUP BY e.phase, e.tool_name
                ORDER BY e.phase, cnt DESC
            """).fetchall()
    except sqlite3.OperationalError:
        return []
    top_tools: dict[str, list] = {}
    for r in tool_rows:
        phase = r["phase"]
        if phase not in top_tools:
            top_tools[phase] = []
        if len(top_tools[phase]) < 2:
            top_tools[phase].append((r["tool_name"], r["cnt"]))
    grand_total_ms = sum(r["total_ms"] or 0 for r in phase_rows) or 1
    return [{
        "phase": r["phase"],
        "total_calls": r["total_calls"],
        "session_count": r["session_count"],
        "time_pct": round((r["total_ms"] or 0) / grand_total_ms * 100),
        "top_tools": top_tools.get(r["phase"], []),
    } for r in phase_rows]


def get_session_pipeline(session_id: str) -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            phase_rows = conn.execute("""
                SELECT phase, COUNT(*) AS total_calls, SUM(duration_ms) AS total_ms
                FROM session_events
                WHERE session_id = ?
                GROUP BY phase
            """, (session_id,)).fetchall()
            tool_rows = conn.execute("""
                SELECT phase, tool_name, COUNT(*) AS cnt
                FROM session_events
                WHERE session_id = ?
                GROUP BY phase, tool_name
                ORDER BY phase, cnt DESC
            """, (session_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    top_tools: dict[str, list] = {}
    for r in tool_rows:
        phase = r["phase"]
        if phase not in top_tools:
            top_tools[phase] = []
        if len(top_tools[phase]) < 2:
            top_tools[phase].append((r["tool_name"], r["cnt"]))
    grand_total_ms = sum(r["total_ms"] or 0 for r in phase_rows) or 1
    return [{
        "phase": r["phase"],
        "total_calls": r["total_calls"],
        "session_count": 1,
        "time_pct": round((r["total_ms"] or 0) / grand_total_ms * 100),
        "top_tools": top_tools.get(r["phase"], []),
    } for r in phase_rows]


def get_recent_sessions_for_select(limit: int = 30) -> list[tuple[str, str]]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT DISTINCT e.session_id, s.slug, s.first_timestamp
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE s.is_subagent = 0
                ORDER BY s.first_timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []
    result = []
    for r in rows:
        date = (r["first_timestamp"] or "")[:10]
        slug = r["slug"] or r["session_id"][:8]
        result.append((f"{date} — {slug}", r["session_id"]))
    return result


_PHASE_COLORS = {
    "Discover": "#00bfff",
    "Plan": "#9370db",
    "Code": "#00ff7f",
    "Test": "#ffa500",
    "Review": "#ff69b4",
    "Deliver": "#00ffff",
}
_PHASE_ORDER = ["Discover", "Plan", "Code", "Test", "Review", "Deliver"]


def render_phase_bar(phases: list[dict], bar_width: int = 80) -> Text:
    by_phase = {p["phase"]: p for p in phases}
    text = Text()

    # Only active phases (with data and non-zero time) get bar segments
    active = [(p, by_phase[p]["time_pct"]) for p in _PHASE_ORDER if p in by_phase and by_phase[p]["time_pct"] > 0]
    if not active:
        text.append("[dim]No timing data available[/dim]\n")
        return text

    total_pct = sum(pct for _, pct in active) or 1
    remaining = bar_width
    allocs = []
    for i, (phase, pct) in enumerate(active):
        if i == len(active) - 1:
            chars = remaining  # last phase gets remainder — no overflow
        else:
            chars = max(1, int(pct / total_pct * bar_width))
            remaining -= chars
        allocs.append((phase, pct, chars))

    for phase, pct, chars in allocs:
        color = _PHASE_COLORS[phase]
        label = f" {phase} {pct}% "
        if len(label) <= chars:
            segment = label + "█" * (chars - len(label))
        else:
            segment = "█" * chars
        text.append(segment, style=f"bold {color}")
    text.append("\n")
    return text


# ── TUI Widgets ────────────────────────────────────────────

class AnimatedBanner(Static):
    DEFAULT_CSS = "AnimatedBanner { height: auto; padding: 1 2; text-align: center; }"

    PALETTE = ["#00ffff", "#1e90ff", "#9370db", "#ff00ff", "#9370db", "#1e90ff"]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._phase = 0
        self._banner_lines = pyfiglet.figlet_format("SDLC TELEMETRY", font="small").splitlines()

    def on_mount(self) -> None:
        self._render_banner()
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % len(self.PALETTE)
        self._render_banner()

    def _render_banner(self) -> None:
        n = len(self.PALETTE)
        text = Text()
        for line_idx, line in enumerate(self._banner_lines):
            if line_idx > 0:
                text.append("\n")
            for i, ch in enumerate(line):
                idx = (self._phase + i) % n
                text.append(ch, style=self.PALETTE[idx])
        self.update(text)


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
                f"   {_fmt_relative_time(stats.get('last_run'))}\n"
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
    def __init__(self, cmd: list[str], title: str, auto_pop: bool = False, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cmd = cmd
        self._title = title
        self._auto_pop = auto_pop
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

        cft = self.app.call_from_thread
        cft(sidebar.update, "Running...")

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
                        cft(log.write, line.rstrip())
                    except Exception:
                        break

            proc.wait()
            elapsed = time.monotonic() - self._start_time

            if proc.returncode != 0:
                cft(log.write, f"\n[red]Process exited with code {proc.returncode}[/red]")

            status = f"{'Done' if proc.returncode == 0 else 'Failed'} ({elapsed:.1f}s)"
            cft(sidebar.update, status)
            if self._auto_pop and proc.returncode == 0:
                cft(self.app.pop_screen)
                return
            cft(log.write, "\nPress any key to return")

        except Exception as exc:
            cft(log.write, f"[red]Error: {exc}[/red]")
            cft(log.write, "\nPress any key to return")

        self._done = True

    def on_unmount(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()

    def on_key(self) -> None:
        if self._done:
            self.app.pop_screen()


class StatsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        with TabbedContent(initial="throughput"):
            with TabPane("Throughput", id="throughput"):
                with Vertical():
                    yield Static(id="velocity-banner")
                    yield Label("[bold] Weekly Throughput[/bold]")
                    yield DataTable(id="weekly-throughput-table", cursor_type="row", zebra_stripes=True)
                    yield Label("[bold] Recent PRs[/bold]")
                    yield DataTable(id="recent-prs-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Lead Time", id="lead-time"):
                with Vertical():
                    yield Label("[bold] PR Lifecycle[/bold]")
                    yield DataTable(id="pr-lifecycle-table", cursor_type="row", zebra_stripes=True)
                    yield Label("[bold] Rework Hotspots[/bold]")
                    yield DataTable(id="rework-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Efficiency", id="efficiency"):
                with Vertical():
                    yield Static(id="efficiency-banner")
                    yield Label("[bold] Model Comparison[/bold]")
                    yield DataTable(id="model-efficiency-table", cursor_type="row")
                    yield Label("[bold] Unproductive Sessions[/bold]")
                    yield DataTable(id="unproductive-table", cursor_type="row", zebra_stripes=True)
                    yield Label("[bold] Tool Usage[/bold]")
                    yield DataTable(id="tool-usage-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Value Stream", id="value-stream"):
                with Vertical():
                    yield Static(id="vs-info-banner")
                    yield Static(id="phase-bar")
                    yield Label("[bold] Phase Breakdown[/bold]")
                    yield DataTable(id="phase-table", cursor_type="row")
                    yield Label("[bold] Session Drill-down[/bold]")
                    yield Select([], id="session-select", prompt="Select a session…")
                    yield Static(id="session-phase-bar")
                    yield DataTable(id="session-phase-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._populate_throughput()
        self._populate_lead_time()
        self._populate_efficiency()
        self._populate_value_stream()

    def _populate_throughput(self) -> None:
        v = get_velocity_banner()
        prs = v.get("prs_week", 0)
        cpd = v.get("commits_day", 0)
        lt = v.get("avg_lead_time_h")
        pr_pct = v.get("push_rate", 0) * 100

        c_prs = _threshold_color(prs, *_THRESH["prs_week"])
        c_cpd = _threshold_color(cpd, *_THRESH["commits_day"])
        c_lt  = _threshold_color(lt, *_THRESH["lead_time_h"], lower_is_better=True)
        c_pr  = _threshold_color(pr_pct, *_THRESH["push_rate_pct"])

        self.query_one("#velocity-banner", Static).update(
            f" PRs/week {_colored(str(prs), c_prs)}"
            f"  ·  Commits/day {_colored(f'{cpd:.1f}', c_cpd)}"
            f"  ·  Avg lead time {_colored(f'{lt or 0:.1f}h', c_lt)}"
            f"  ·  Push rate {_colored(f'{pr_pct:.1f}%', c_pr)}"
        )
        wt = self.query_one("#weekly-throughput-table", DataTable)
        wt.add_columns("Week", "PRs Created", "PRs Merged", "Commits", "Lead Time (avg)")
        for week, prs_c, prs_m, commits, lead_h in get_weekly_throughput():
            lead_h_clamped, c = _lead_time_clamp_color(lead_h)
            lead_cell = Text(f"{lead_h_clamped:.1f}h" if lead_h_clamped is not None else "—", style=f"bold {c}" if c else "")
            wt.add_row(week, str(prs_c), str(prs_m), str(commits), lead_cell)
        rt = self.query_one("#recent-prs-table", DataTable)
        rt.add_columns("Date", "PR#", "Repository", "Branch")
        for date, pr_num, repo, branch in get_recent_prs():
            rt.add_row(date or "", f"#{pr_num}" if pr_num else "—",
                       (repo or "")[:30], (branch or "")[:40])

    def _populate_lead_time(self) -> None:
        lc = self.query_one("#pr-lifecycle-table", DataTable)
        lc.add_columns("Branch", "Lead Time", "Sessions", "Commits", "First Session", "PR Created")
        for branch, lead_h, sessions, commits, first_s, pr_c in get_pr_lifecycle():
            lead_h_clamped, c = _lead_time_clamp_color(lead_h)
            lead_cell = Text(_fmt_duration(lead_h_clamped * 3600) if lead_h_clamped is not None else "—", style=f"bold {c}" if c else "")
            lc.add_row((branch or "")[:40], lead_cell,
                       str(sessions), str(commits), first_s or "", pr_c or "")
        rw = self.query_one("#rework-table", DataTable)
        rw.add_columns("Branch", "Sessions", "Commits", "Total Duration", "Commits/Session")
        for branch, sessions, commits, dur_s, cps in get_rework_hotspots():
            rw.add_row((branch or "")[:40], str(sessions), str(commits),
                       _fmt_duration(dur_s), f"{cps:.1f}")

    def _populate_efficiency(self) -> None:
        e = get_efficiency_metrics()
        prod = e.get('productivity_rate', 0)
        c_prod = _threshold_color(prod, *_THRESH["productivity"])
        self.query_one("#efficiency-banner", Static).update(
            f" Productivity {_colored(f'{prod:.0f}%', c_prod)}"
            f"  ·  Tokens/commit [bold]{_fmt_tokens(int(e.get('tokens_per_commit', 0)))}[/bold]"
            f"  ·  Avg session [bold]{_fmt_duration(e.get('avg_duration_s', 0))}[/bold]"
            f"  ·  Subagent ratio [bold]{e.get('subagent_ratio', 0):.1f}x[/bold]"
        )
        me = self.query_one("#model-efficiency-table", DataTable)
        me.add_columns("Model", "Sessions", "Avg Duration", "Commits", "Tokens/Commit")
        for model, sessions, avg_dur_s, commits, tpc in get_model_efficiency():
            me.add_row(model or "unknown", f"{sessions:,}", _fmt_duration(avg_dur_s),
                       f"{commits:,}", _fmt_tokens(int(tpc)) if tpc else "—")
        ut = self.query_one("#unproductive-table", DataTable)
        ut.add_columns("Date", "Duration", "Model", "First Prompt")
        for date, dur_s, model, prompt in get_unproductive_sessions():
            dur_h = (dur_s / 3600) if dur_s is not None else None
            c = _threshold_color(dur_h, *_THRESH["unproductive_h"], lower_is_better=True)
            dur_cell = Text(_fmt_duration(dur_s), style=f"bold {c}" if c else "")
            ut.add_row(date or "", dur_cell, model or "unknown",
                       (prompt or "")[:80])
        tt = self.query_one("#tool-usage-table", DataTable)
        tt.add_columns("Tool", "Total Calls", "Sessions", "Avg/Session")
        for i, (tool, total, session_cnt, avg) in enumerate(get_tool_usage_enhanced()):
            if i < 3:
                style = "bold"
            elif i < 10:
                style = ""
            else:
                style = "dim"
            tt.add_row(Text(tool, style=style), f"{total:,}", f"{session_cnt:,}", f"{avg:.1f}")


    def _populate_value_stream(self) -> None:
        pt = self.query_one("#phase-table", DataTable)
        pt.add_columns("Phase", "Calls", "Time %", "Sessions", "Top Tools")
        spt = self.query_one("#session-phase-table", DataTable)
        spt.add_columns("Phase", "Calls", "Time %", "Top Tools")

        phases = get_aggregate_pipeline()
        banner = self.query_one("#vs-info-banner", Static)
        if not phases:
            banner.update("[yellow]No event data — run extraction to populate value stream.[/yellow]")
            return
        banner.update("")
        self.query_one("#phase-bar", Static).update(render_phase_bar(phases))

        for p in sorted(phases, key=lambda x: _PHASE_ORDER.index(x["phase"]) if x["phase"] in _PHASE_ORDER else 99):
            tools_str = ", ".join(t[0] for t in p["top_tools"][:3])
            pt.add_row(_phase_cell(p["phase"]), f"{p['total_calls']:,}", f"{p['time_pct']}%",
                       f"{p['session_count']:,}", tools_str)

        sessions = get_recent_sessions_for_select()
        if sessions:
            sel = self.query_one("#session-select", Select)
            sel.set_options((label, sid) for label, sid in sessions)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        phases = get_session_pipeline(str(event.value))
        bar = self.query_one("#session-phase-bar", Static)
        table = self.query_one("#session-phase-table", DataTable)
        table.clear()

        if not phases:
            bar.update("[dim]No events for this session.[/dim]")
            return

        bar.update(render_phase_bar(phases))
        for p in sorted(phases, key=lambda x: _PHASE_ORDER.index(x["phase"]) if x["phase"] in _PHASE_ORDER else 99):
            tools_str = ", ".join(t[0] for t in p["top_tools"][:3])
            table.add_row(_phase_cell(p["phase"]), f"{p['total_calls']:,}", f"{p['time_pct']}%", tools_str)


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
    _extraction_running: bool = False

    BINDINGS = [
        ("r", "run", "Run"),
        ("c", "configure", "Configure"),
        ("u", "update", "Update"),
        ("s", "stats", "Stats"),
        ("x", "uninstall", "Uninstall"),
        ("q", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        yield Static(id="extraction-progress")
        with Horizontal():
            with Vertical(id="main-content"):
                yield OptionList(
                    "Run extraction",
                    "Configure projects",
                    "Update",
                    "View stats",
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
            self.action_stats,
            self.action_uninstall,
        ]
        if 0 <= event.option_index < len(actions):
            actions[event.option_index]()

    def action_run(self) -> None:
        if self._extraction_running:
            self.notify("Extraction already running.")
            return
        bases = read_config()
        if not bases:
            self.notify("No projects configured — run Configure first.")
            return
        dirs = resolve_dirs(bases)
        if not dirs:
            self.notify("No project directories found.", severity="error")
            return
        self._run_extraction(dirs)

    @work(thread=True)
    def _run_extraction(self, dirs: list[Path]) -> None:
        self._extraction_running = True
        cft = self.app.call_from_thread
        progress = self.query_one("#extraction-progress", Static)
        cft(progress.add_class, "visible")
        db = None

        def update_progress(current, total):
            if total == 0:
                return
            pct = current / total
            bar_w = 30
            filled = int(pct * bar_w)
            bar = "█" * filled + "░" * (bar_w - filled)
            cft(progress.update, f" [{bar}] {current:,}/{total:,}")

        try:
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            db = ExtractDB(DB_FILE)
            extractor = SessionExtractor(db, verbose=False)
            stats = extractor.run([str(d) for d in dirs], on_progress=update_progress)
            extractor.load_facets()
            from datetime import datetime, timezone
            db.set_meta("last_run", datetime.now(timezone.utc).isoformat())
            db.set_meta("sessions_processed", str(stats["processed"]))
            db.set_meta("sessions_skipped", str(stats["skipped"]))
            db.commit()

            if stats["total"] == 0:
                cft(self.notify, "No JSONL files found in configured projects.", severity="warning")
            else:
                cft(self.notify, f"Extracted {stats['processed']} sessions ({stats['skipped']} unchanged)")
                cft(lambda: self.app.push_screen(StatsScreen()))
        except Exception as exc:
            cft(self.notify, f"Extraction failed: {exc}", severity="error")
        finally:
            if db is not None:
                db.close()
            cft(progress.remove_class, "visible")
            cft(self.query_one(StatusSidebar).refresh_stats)
            self._extraction_running = False

    def action_configure(self) -> None:
        self.app.push_screen(ConfigureScreen())

    def action_update(self) -> None:
        self.notify("Checking for updates...")
        self._check_and_update()

    @work(thread=True)
    def _check_and_update(self) -> None:
        cft = self.app.call_from_thread
        if __version__ == "dev" or ".dev" in __version__:
            cft(self.notify, f"Dev install ({__version__}) — update from source instead", severity="warning")
            return
        local_sha: str | None = None
        if "+g" in __version__:
            local_sha = __version__.split("+g")[1][:7]
        try:
            req = urllib.request.Request(
                GITHUB_API_URL,
                headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "sdlc-t"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            remote_sha = data["sha"][:7]
            if local_sha and local_sha == remote_sha:
                cft(self.notify, f"Already on latest ({__version__})")
                return
            cft(self.notify, f"Update available ({remote_sha}), installing...")
            proc = Popen(
                [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_URL],
                stdout=PIPE, stderr=STDOUT, text=True,
            )
            proc.wait()
            if proc.returncode == 0:
                cft(self.notify, "Updated — restart sdlc-t to apply")
            else:
                cft(self.notify, "Update failed", severity="error")
        except urllib.error.URLError as exc:
            cft(self.notify, f"Network error: {exc}", severity="warning")
        except Exception as exc:
            cft(self.notify, f"Update check failed: {exc}", severity="error")

    def action_stats(self) -> None:
        if not DB_FILE.exists():
            self.notify("No database yet — run extraction first.")
            return
        self.app.push_screen(StatsScreen())

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
    StatsScreen TabbedContent {
        height: 1fr;
    }
    StatsScreen DataTable {
        margin-bottom: 1;
    }
    #velocity-banner, #efficiency-banner {
        height: auto;
        padding: 1 2;
        background: $surface;
        border-bottom: solid $accent;
    }
    #weekly-throughput-table {
        height: 12;
    }
    #recent-prs-table {
        height: 8;
    }
    #pr-lifecycle-table, #rework-table {
        height: 1fr;
    }
    #model-efficiency-table {
        height: 10;
    }
    #unproductive-table {
        height: 1fr;
    }
    #tool-usage-table {
        height: 1fr;
    }
    #extraction-progress { height: auto; padding: 0 1; display: none; }
    #extraction-progress.visible { display: block; }
    #phase-bar          { height: auto; padding: 1 0; }
    #phase-table        { height: 10; }
    #session-phase-bar  { height: auto; padding: 1 0; }
    #session-phase-table { height: 10; }
    #vs-info-banner     { height: auto; padding: 0 0 1 0; }
    #session-select     { margin-bottom: 1; }
    """

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen())

# ── Entry Point ────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        prog="sdlc-t",
        description="SDLC session analytics for Claude Code",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.parse_args()
    SdlcApp().run()

if __name__ == "__main__":
    main()
