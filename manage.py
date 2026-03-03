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
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from typing import Literal

import pyfiglet
from rich.text import Text
from sdlc_extract import DB as ExtractDB, SCHEMA_VERSION, SessionExtractor
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
Scope = Literal["all_activity", "delivery_only"]

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


def _scope_clause(scope: Scope, alias: str = "s") -> str:
    if scope == "delivery_only":
        return f"{alias}.is_subagent = 0"
    return "1=1"


def _confidence_label(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "LOW"
    pct = numerator / denominator
    if pct >= 0.90:
        return "HIGH"
    if pct >= 0.70:
        return "MED"
    return "LOW"


def _confidence_style(label: str) -> str:
    if label == "HIGH":
        return "bold #00ff7f"
    if label == "MED":
        return "bold #ffa500"
    return "bold #ff5555"


def _scope_label(scope: Scope) -> str:
    return "All Activity" if scope == "all_activity" else "Delivery Only"


def _scoped_pr_urls(scope: Scope) -> str:
    return (
        "SELECT DISTINCT pl.pr_url "
        "FROM pr_links pl "
        "JOIN sessions s ON s.session_id = pl.session_id "
        "WHERE pl.pr_url IS NOT NULL AND pl.pr_url != '' "
        f"AND {_scope_clause(scope, 's')}"
    )


def _pr_commit_rollup_sql(scope: Scope) -> str:
    scoped = _scoped_pr_urls(scope)
    return f"""
        WITH scoped_pr_urls AS (
            {scoped}
        ),
        pr_base AS (
            SELECT
                spu.pr_url,
                pf.repo_full_name,
                pf.pr_number,
                pf.opened_at,
                pf.merged_at
            FROM scoped_pr_urls spu
            LEFT JOIN pr_facts pf ON pf.pr_url = spu.pr_url
        ),
        timeline_first AS (
            SELECT
                pr_url,
                commit_sha,
                MIN(event_at) AS first_event_at
            FROM pr_commit_events
            WHERE event_type = 'committed' AND commit_sha IS NOT NULL
            GROUP BY pr_url, commit_sha
        ),
        timeline_counts AS (
            SELECT
                pb.pr_url,
                COUNT(DISTINCT CASE WHEN tf.first_event_at < pb.opened_at THEN tf.commit_sha END) AS pre_count,
                COUNT(DISTINCT CASE WHEN tf.first_event_at >= pb.opened_at THEN tf.commit_sha END) AS post_count,
                COUNT(DISTINCT tf.commit_sha) AS total_count
            FROM pr_base pb
            LEFT JOIN timeline_first tf ON tf.pr_url = pb.pr_url
            WHERE pb.opened_at IS NOT NULL
            GROUP BY pb.pr_url
        ),
        final_counts AS (
            SELECT
                pb.pr_url,
                COUNT(DISTINCT CASE WHEN COALESCE(pcf.authored_at, pcf.committed_at) < pb.opened_at THEN pcf.commit_sha END) AS pre_count,
                COUNT(DISTINCT CASE WHEN COALESCE(pcf.authored_at, pcf.committed_at) >= pb.opened_at THEN pcf.commit_sha END) AS post_count,
                COUNT(DISTINCT pcf.commit_sha) AS total_count
            FROM pr_base pb
            LEFT JOIN pr_commits_final pcf ON pcf.pr_url = pb.pr_url
            WHERE pb.opened_at IS NOT NULL
            GROUP BY pb.pr_url
        ),
        rollup AS (
            SELECT
                pb.pr_url,
                COALESCE(pb.repo_full_name, '') AS repo_full_name,
                pb.pr_number,
                pb.opened_at,
                pb.merged_at,
                CASE
                    WHEN pb.opened_at IS NULL THEN 0
                    WHEN COALESCE(tc.total_count, 0) > 0 THEN COALESCE(tc.pre_count, 0)
                    WHEN COALESCE(fc.total_count, 0) > 0 THEN COALESCE(fc.pre_count, 0)
                    ELSE 0
                END AS pre_commits,
                CASE
                    WHEN pb.opened_at IS NULL THEN 0
                    WHEN COALESCE(tc.total_count, 0) > 0 THEN COALESCE(tc.post_count, 0)
                    WHEN COALESCE(fc.total_count, 0) > 0 THEN COALESCE(fc.post_count, 0)
                    ELSE 0
                END AS post_commits,
                CASE
                    WHEN pb.opened_at IS NULL THEN 'LOW'
                    WHEN COALESCE(tc.total_count, 0) > 0 THEN 'HIGH'
                    WHEN COALESCE(fc.total_count, 0) > 0 THEN 'MED'
                    ELSE 'LOW'
                END AS confidence
            FROM pr_base pb
            LEFT JOIN timeline_counts tc ON tc.pr_url = pb.pr_url
            LEFT JOIN final_counts fc ON fc.pr_url = pb.pr_url
        )
    """


def _get_meta(key: str) -> str | None:
    if not DB_FILE.exists():
        return None
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT value FROM extraction_meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _pr_coverage(conn: sqlite3.Connection, scope: Scope) -> tuple[int, int]:
    row = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT pl.pr_url) AS total_links,
            COUNT(DISTINCT pf.pr_url) AS matched
        FROM pr_links pl
        JOIN sessions s ON s.session_id = pl.session_id
        LEFT JOIN pr_facts pf ON pf.pr_url = pl.pr_url
        WHERE pl.pr_url IS NOT NULL
          AND pl.pr_url != ''
          AND {_scope_clause(scope, 's')}
        """
    ).fetchone()
    total = int((row[0] if row else 0) or 0)
    matched = int((row[1] if row else 0) or 0)
    return matched, total


def _schema_outdated() -> bool:
    v = _get_meta("schema_version")
    return bool(DB_FILE.exists() and v != SCHEMA_VERSION)


def _normalize_repo_filters(repos: list[str] | None) -> list[str]:
    if not repos:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        v = str(repo or "").strip().lower()
        if not v or v in seen:
            continue
        seen.add(v)
        cleaned.append(v)
    return cleaned


def get_cli_snapshot(
    scope: Scope = "all_activity",
    repos: list[str] | None = None,
    limit: int = 10,
) -> dict:
    repos_norm = _normalize_repo_filters(repos)
    row_limit = max(int(limit), 1)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "source_db": str(DB_FILE),
        "filters": {"repos": repos_norm},
        "overview": {
            "total_prs": 0,
            "prs_with_facts": 0,
            "prs_merged": 0,
            "repo_count": 0,
        },
        "commit_timing_summary": {
            "avg_pre": 0.0,
            "avg_post": 0.0,
            "post_ratio": 0.0,
            "coverage_num": 0,
            "coverage_den": 0,
            "confidence": "LOW",
        },
        "repo_summary": [],
        "post_open_outliers": [],
        "recent_prs": [],
    }
    if not DB_FILE.exists():
        snapshot["error"] = "database_not_found"
        return snapshot

    placeholders = ",".join("?" for _ in repos_norm)
    repo_filter_links = ""
    repo_filter_rollup = ""
    repo_params: list[object] = []
    if repos_norm:
        repo_filter_links = (
            " AND lower(COALESCE(pf.repo_full_name, pl.pr_repository)) "
            f"IN ({placeholders})"
        )
        repo_filter_rollup = f" AND lower(repo_full_name) IN ({placeholders})"
        repo_params = list(repos_norm)

    try:
        with sqlite3.connect(DB_FILE) as conn:
            overview = conn.execute(
                f"""
                WITH scoped_prs AS (
                    SELECT DISTINCT
                        pl.pr_url,
                        COALESCE(pf.repo_full_name, pl.pr_repository) AS repo_name,
                        pf.pr_url AS fact_url,
                        pf.is_merged AS is_merged
                    FROM pr_links pl
                    JOIN sessions s ON s.session_id = pl.session_id
                    LEFT JOIN pr_facts pf ON pf.pr_url = pl.pr_url
                    WHERE pl.pr_url IS NOT NULL
                      AND pl.pr_url != ''
                      AND {_scope_clause(scope, 's')}
                      {repo_filter_links}
                )
                SELECT
                    COUNT(*) AS total_prs,
                    SUM(CASE WHEN fact_url IS NOT NULL THEN 1 ELSE 0 END) AS prs_with_facts,
                    SUM(CASE WHEN is_merged = 1 THEN 1 ELSE 0 END) AS prs_merged,
                    COUNT(DISTINCT repo_name) AS repo_count
                FROM scoped_prs
                """,
                repo_params,
            ).fetchone()
            total_prs = int((overview[0] if overview else 0) or 0)
            prs_with_facts = int((overview[1] if overview else 0) or 0)
            prs_merged = int((overview[2] if overview else 0) or 0)
            repo_count = int((overview[3] if overview else 0) or 0)
            snapshot["overview"] = {
                "total_prs": total_prs,
                "prs_with_facts": prs_with_facts,
                "prs_merged": prs_merged,
                "repo_count": repo_count,
            }

            summary = conn.execute(
                _pr_commit_rollup_sql(scope)
                + f"""
                SELECT
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN pre_commits END), 0),
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN post_commits END), 0),
                    COALESCE(
                        SUM(CASE WHEN confidence != 'LOW' THEN post_commits ELSE 0 END) * 1.0
                        / NULLIF(
                            SUM(CASE WHEN confidence != 'LOW' THEN (pre_commits + post_commits) ELSE 0 END),
                            0
                        ),
                        0.0
                    ),
                    SUM(CASE WHEN confidence != 'LOW' THEN 1 ELSE 0 END),
                    COUNT(*)
                FROM rollup
                WHERE 1=1
                {repo_filter_rollup}
                """,
                repo_params,
            ).fetchone()
            cov_num = int((summary[3] if summary else 0) or 0)
            cov_den = int((summary[4] if summary else 0) or 0)
            snapshot["commit_timing_summary"] = {
                "avg_pre": float((summary[0] if summary else 0) or 0),
                "avg_post": float((summary[1] if summary else 0) or 0),
                "post_ratio": float((summary[2] if summary else 0) or 0),
                "coverage_num": cov_num,
                "coverage_den": cov_den,
                "confidence": _confidence_label(cov_num, cov_den),
            }

            repo_rows = conn.execute(
                _pr_commit_rollup_sql(scope)
                + f"""
                SELECT
                    COALESCE(NULLIF(repo_full_name, ''), '<unknown>') AS repo_name,
                    COUNT(*) AS prs,
                    SUM(CASE WHEN confidence != 'LOW' THEN 1 ELSE 0 END) AS covered,
                    SUM(CASE WHEN confidence = 'HIGH' THEN 1 ELSE 0 END) AS high_count,
                    SUM(CASE WHEN confidence = 'MED' THEN 1 ELSE 0 END) AS med_count,
                    SUM(CASE WHEN confidence = 'LOW' THEN 1 ELSE 0 END) AS low_count,
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN pre_commits END), 0),
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN post_commits END), 0),
                    COALESCE(
                        SUM(CASE WHEN confidence != 'LOW' THEN post_commits ELSE 0 END) * 1.0
                        / NULLIF(
                            SUM(CASE WHEN confidence != 'LOW' THEN (pre_commits + post_commits) ELSE 0 END),
                            0
                        ),
                        0.0
                    ) AS post_ratio
                FROM rollup
                WHERE 1=1
                {repo_filter_rollup}
                GROUP BY COALESCE(NULLIF(repo_full_name, ''), '<unknown>')
                ORDER BY prs DESC, post_ratio DESC, repo_name ASC
                LIMIT ?
                """,
                [*repo_params, row_limit],
            ).fetchall()
            snapshot["repo_summary"] = [
                {
                    "repo": str(r[0]),
                    "prs": int(r[1] or 0),
                    "covered_prs": int(r[2] or 0),
                    "confidence": _confidence_label(int(r[2] or 0), int(r[1] or 0)),
                    "high_count": int(r[3] or 0),
                    "med_count": int(r[4] or 0),
                    "low_count": int(r[5] or 0),
                    "avg_pre": float(r[6] or 0),
                    "avg_post": float(r[7] or 0),
                    "post_ratio": float(r[8] or 0),
                }
                for r in repo_rows
            ]

            outlier_rows = conn.execute(
                _pr_commit_rollup_sql(scope)
                + f"""
                SELECT
                    pr_number,
                    COALESCE(NULLIF(repo_full_name, ''), '<unknown>') AS repo_name,
                    opened_at,
                    merged_at,
                    pre_commits,
                    post_commits,
                    CASE
                        WHEN (pre_commits + post_commits) > 0
                        THEN (post_commits * 100.0) / (pre_commits + post_commits)
                        ELSE 0
                    END AS post_pct,
                    confidence
                FROM rollup
                WHERE confidence != 'LOW'
                {repo_filter_rollup}
                ORDER BY post_commits DESC, post_pct DESC, opened_at DESC
                LIMIT ?
                """,
                [*repo_params, row_limit],
            ).fetchall()
            snapshot["post_open_outliers"] = [
                {
                    "pr_number": int(r[0] or 0),
                    "repo": str(r[1] or ""),
                    "opened_at": r[2],
                    "merged_at": r[3],
                    "pre_commits": int(r[4] or 0),
                    "post_commits": int(r[5] or 0),
                    "post_pct": float(r[6] or 0),
                    "confidence": str(r[7] or "LOW"),
                }
                for r in outlier_rows
            ]

            recent_rows = conn.execute(
                f"""
                SELECT
                    substr(COALESCE(pf.opened_at, pl.timestamp), 1, 19),
                    COALESCE(pf.pr_number, pl.pr_number),
                    COALESCE(pf.repo_full_name, pl.pr_repository),
                    s.git_branch
                FROM pr_links pl
                JOIN sessions s ON s.session_id = pl.session_id
                LEFT JOIN pr_facts pf ON pf.pr_url = pl.pr_url
                WHERE {_scope_clause(scope, 's')}
                {repo_filter_links}
                ORDER BY COALESCE(pf.opened_at, pl.timestamp) DESC
                LIMIT ?
                """,
                [*repo_params, row_limit],
            ).fetchall()
            snapshot["recent_prs"] = [
                {
                    "timestamp": r[0],
                    "pr_number": int(r[1] or 0),
                    "repo": str(r[2] or ""),
                    "branch": str(r[3] or ""),
                }
                for r in recent_rows
            ]
    except sqlite3.Error as exc:
        snapshot["error"] = f"sqlite_error: {exc}"
    return snapshot


def _rows_to_dicts(rows: list[tuple], keys: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        out.append({k: row[i] if i < len(row) else None for i, k in enumerate(keys)})
    return out


def get_cli_tui_parity(scope: Scope = "all_activity", limit: int = 10) -> dict:
    lim = max(int(limit), 1)
    sessions = get_recent_sessions_for_select(limit=min(lim, 10), scope=scope)
    session_breakdowns = []
    for label, session_id in sessions[: min(len(sessions), 5)]:
        session_breakdowns.append(
            {
                "label": label,
                "session_id": session_id,
                "pipeline": get_session_pipeline(session_id),
            }
        )
    return {
        "scope": scope,
        "throughput": {
            "velocity_banner": get_velocity_banner(scope),
            "weekly_throughput": _rows_to_dicts(
                get_weekly_throughput(weeks=lim, scope=scope),
                ("week", "prs_created", "prs_merged", "commits", "avg_lead_hours"),
            ),
            "recent_prs": _rows_to_dicts(
                get_recent_prs(limit=lim, scope=scope),
                ("date", "pr_number", "repo_name", "branch"),
            ),
        },
        "lead_time": {
            "pr_lifecycle": _rows_to_dicts(
                get_pr_lifecycle(limit=lim, scope=scope),
                ("branch", "lead_time_hours", "sessions", "commits", "first_session_date", "pr_created_date", "confidence"),
            ),
            "rework_hotspots": _rows_to_dicts(
                get_rework_hotspots(limit=lim, scope=scope),
                ("branch", "sessions", "commits", "total_duration_s", "commits_per_session"),
            ),
        },
        "pr_commit_timing": {
            "summary": get_pr_commit_timing_summary(scope),
            "outliers": _rows_to_dicts(
                get_pr_post_open_commit_outliers(limit=lim, scope=scope),
                ("pr_number", "repo", "opened_at", "merged_at", "pre_commits", "post_commits", "post_pct", "confidence"),
            ),
            "details": _rows_to_dicts(
                get_pr_commit_timing_details(limit=lim, scope=scope),
                ("pr_number", "repo", "opened_at", "merged_at", "pre_commits", "post_commits", "post_pct", "confidence"),
            ),
        },
        "efficiency": {
            "banner": get_efficiency_metrics(scope),
            "model_efficiency": _rows_to_dicts(
                get_model_efficiency(scope),
                ("model", "sessions", "avg_duration_s", "commits", "tokens_per_commit"),
            ),
            "unproductive_sessions": _rows_to_dicts(
                get_unproductive_sessions(limit=lim, scope=scope),
                ("date", "duration_s", "model", "first_prompt"),
            ),
            "tool_usage": _rows_to_dicts(
                get_tool_usage_enhanced(scope),
                ("tool", "total_calls", "sessions", "avg_per_session"),
            ),
        },
        "value_stream": {
            "aggregate_pipeline": get_aggregate_pipeline(scope),
            "default_code_rate": get_default_code_rate(scope),
            "sample_session_breakdowns": session_breakdowns,
        },
    }


def _extract_paths_from_project_names(project_values: list[str] | None) -> tuple[list[Path], list[str]]:
    projects = get_project_list()
    by_name = {str(p["name"]).lower(): p for p in projects}
    by_display = {str(p["display"]).lower(): p for p in projects}
    resolved_dirs: list[str] = []
    unknown: list[str] = []
    requested = project_values or []
    for raw in requested:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        p = by_name.get(key) or by_display.get(key)
        if not p:
            unknown.append(raw)
            continue
        for d in p.get("dirs", [p["name"]]):
            if d not in resolved_dirs:
                resolved_dirs.append(d)
    return resolve_dirs(resolved_dirs), unknown


def _perform_extraction(
    dirs: list[Path],
    *,
    full: bool = False,
    enrich: bool = False,
    scope: Scope = "all_activity",
    github_token_env: str = "GITHUB_TOKEN",
    github_max_prs: int = 500,
) -> dict:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = ExtractDB(DB_FILE)
    try:
        extractor = SessionExtractor(db, verbose=False)
        stats = extractor.run([str(d) for d in dirs], full=full)
        facets = extractor.load_facets()
        db.set_meta("last_run", datetime.now(timezone.utc).isoformat())
        db.set_meta("sessions_processed", str(stats["processed"]))
        db.set_meta("sessions_skipped", str(stats["skipped"]))
        row = db.conn.execute(
            "SELECT SUM(CASE WHEN phase='Code' THEN 1 ELSE 0 END), COUNT(*) FROM session_events"
        ).fetchone()
        code_events = int((row[0] if row else 0) or 0)
        all_events = int((row[1] if row else 0) or 0)
        default_code_rate = (code_events / all_events) if all_events else 0.0
        db.set_meta("schema_version", SCHEMA_VERSION)
        db.set_meta("default_code_rate", f"{default_code_rate:.4f}")

        enrich_stats = {
            "attempted": 0,
            "updated": 0,
            "errors": 0,
            "commit_errors": 0,
            "commits_final": 0,
            "commit_events": 0,
            "skipped": True,
        }
        if enrich:
            token = os.environ.get(github_token_env)
            if token:
                try:
                    from github_enrich import sync_pr_facts
                    enrich_stats = sync_pr_facts(
                        db=db,
                        token=token,
                        max_prs=max(int(github_max_prs), 1),
                        scope=scope,
                        verbose=False,
                    )
                except Exception:
                    enrich_stats = {
                        "attempted": 0,
                        "updated": 0,
                        "errors": 1,
                        "commit_errors": 1,
                        "commits_final": 0,
                        "commit_events": 0,
                        "skipped": False,
                    }
            else:
                enrich_stats = {
                    "attempted": 0,
                    "updated": 0,
                    "errors": 0,
                    "commit_errors": 0,
                    "commits_final": 0,
                    "commit_events": 0,
                    "skipped": True,
                    "reason": f"{github_token_env}_not_set",
                }

        now_iso = datetime.now(timezone.utc).isoformat()
        db.set_meta("github_enrich_last_run", now_iso)
        db.set_meta("github_enrich_errors", str(int(enrich_stats.get("errors", 0))))
        db.set_meta("github_enrich_commit_last_run", now_iso)
        db.set_meta("github_enrich_commit_errors", str(int(enrich_stats.get("commit_errors", 0))))
        db.commit()
        return {
            "ok": True,
            "stats": stats,
            "facets_loaded": facets,
            "default_code_rate": default_code_rate,
            "enrich": enrich_stats,
            "project_dirs": [str(d) for d in dirs],
        }
    finally:
        db.close()


def _check_for_update() -> dict:
    if __version__ == "dev" or ".dev" in __version__:
        return {
            "ok": True,
            "local_version": __version__,
            "update_available": False,
            "reason": "dev_install",
        }
    local_sha: str | None = None
    if "+g" in __version__:
        local_sha = __version__.split("+g")[1][:7]
    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "sdlc-t"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    remote_sha = str(data["sha"])[:7]
    return {
        "ok": True,
        "local_version": __version__,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "update_available": bool(local_sha != remote_sha) if local_sha else True,
    }


def _ai_response(command: str, *, ok: bool, data: dict | None = None, errors: list[str] | None = None, warnings: list[str] | None = None) -> dict:
    return {
        "ok": ok,
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
        "errors": errors or [],
        "warnings": warnings or [],
    }


def run_ai_command(args) -> dict:
    command = str(getattr(args, "command", "") or "stats")
    scope = str(getattr(args, "scope", "all_activity") or "all_activity")
    limit = int(getattr(args, "limit", 10) or 10)
    repos = getattr(args, "repos", None)
    project_values = getattr(args, "projects", None)

    if command == "stats":
        data = {
            "repo_snapshot": get_cli_snapshot(scope=scope, repos=repos, limit=limit),
            "tui_parity": get_cli_tui_parity(scope=scope, limit=limit),
        }
        return _ai_response(command, ok=True, data=data)

    if command == "status":
        data = {
            "db_stats": get_db_stats(),
            "config": {
                "include_dirs": read_config(),
                "schema_outdated": _schema_outdated(),
            },
            "paths": {
                "data_dir": str(DATA_DIR),
                "config_file": str(CONFIG_FILE),
                "db_file": str(DB_FILE),
            },
        }
        return _ai_response(command, ok=True, data=data)

    if command == "projects.list":
        return _ai_response(command, ok=True, data={"projects": get_project_list()})

    if command == "config.get":
        include_dirs = read_config()
        dirs = resolve_dirs(include_dirs)
        return _ai_response(
            command,
            ok=True,
            data={
                "include_dirs": include_dirs,
                "resolved_paths": [str(d) for d in dirs],
            },
        )

    if command == "config.set":
        if not project_values:
            return _ai_response(command, ok=False, errors=["missing_projects"])
        dirs, unknown = _extract_paths_from_project_names(project_values)
        if not dirs:
            return _ai_response(command, ok=False, errors=["no_valid_projects"], warnings=unknown)
        dir_names = [d.name for d in dirs]
        write_config(dir_names)
        return _ai_response(
            command,
            ok=True,
            data={
                "include_dirs": dir_names,
                "resolved_paths": [str(d) for d in dirs],
            },
            warnings=unknown,
        )

    if command == "extract.run":
        warnings: list[str] = []
        if project_values:
            dirs, unknown = _extract_paths_from_project_names(project_values)
            warnings.extend(unknown)
        else:
            dirs = resolve_dirs(read_config())
        if not dirs:
            return _ai_response(command, ok=False, errors=["no_project_dirs"], warnings=warnings)
        try:
            data = _perform_extraction(
                dirs,
                full=bool(getattr(args, "full", False)),
                enrich=bool(getattr(args, "enrich", False)),
                scope=scope,  # type: ignore[arg-type]
                github_token_env=str(getattr(args, "github_token_env", "GITHUB_TOKEN") or "GITHUB_TOKEN"),
                github_max_prs=int(getattr(args, "github_max_prs", 500) or 500),
            )
            return _ai_response(command, ok=True, data=data, warnings=warnings)
        except Exception as exc:
            return _ai_response(command, ok=False, errors=[str(exc)], warnings=warnings)

    if command == "update.check":
        try:
            return _ai_response(command, ok=True, data=_check_for_update())
        except Exception as exc:
            return _ai_response(command, ok=False, errors=[str(exc)])

    if command == "update.apply":
        try:
            update_data = _check_for_update()
        except Exception as exc:
            return _ai_response(command, ok=False, errors=[str(exc)])
        if not update_data.get("update_available"):
            return _ai_response(command, ok=True, data={"update": update_data, "applied": False})
        proc = Popen(
            [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_URL],
            stdout=PIPE, stderr=STDOUT, text=True,
        )
        stdout, _ = proc.communicate()
        data = {
            "update": update_data,
            "applied": proc.returncode == 0,
            "returncode": int(proc.returncode or 0),
            "output_tail": (stdout or "")[-4000:],
        }
        return _ai_response(command, ok=proc.returncode == 0, data=data)

    if command == "uninstall":
        if not bool(getattr(args, "yes", False)):
            return _ai_response(command, ok=False, errors=["confirmation_required_use_yes"])
        removed: list[str] = []
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            removed.append(str(CONFIG_FILE))
        if bool(getattr(args, "remove_db", False)) and DB_FILE.exists():
            DB_FILE.unlink()
            removed.append(str(DB_FILE))
            for suffix in ("-shm", "-wal"):
                sib = DB_FILE.parent / (DB_FILE.name + suffix)
                if sib.exists():
                    sib.unlink()
                    removed.append(str(sib))
        return _ai_response(command, ok=True, data={"removed": removed})

    return _ai_response(command, ok=False, errors=[f"unknown_command:{command}"])

def get_velocity_banner(scope: Scope = "all_activity") -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            prs_week = conn.execute(
                f"""
                SELECT COUNT(DISTINCT pf.pr_url)
                FROM pr_facts pf
                JOIN pr_links pl ON pl.pr_url = pf.pr_url
                JOIN sessions s ON s.session_id = pl.session_id
                WHERE datetime(pf.merged_at) >= datetime('now', '-7 days')
                  AND {_scope_clause(scope, 's')}
                """
            ).fetchone()[0]
            commits_7d = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM git_operations g
                JOIN sessions s ON s.session_id = g.session_id
                WHERE g.git_op_type = 'commit'
                  AND datetime(g.timestamp) >= datetime('now', '-7 days')
                  AND {_scope_clause(scope, 's')}
                """
            ).fetchone()[0]
            avg_lead = conn.execute(
                f"""
                WITH scoped_sessions AS (
                    SELECT * FROM sessions s WHERE {_scope_clause(scope, 's')}
                ),
                branch_first AS (
                    SELECT git_branch, MIN(first_timestamp) AS first_session
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_primary AS (
                    SELECT DISTINCT pf.pr_url,
                           COALESCE(pf.head_branch, ss.git_branch) AS branch_name,
                           pf.opened_at
                    FROM pr_facts pf
                    JOIN pr_links pl ON pl.pr_url = pf.pr_url
                    JOIN scoped_sessions ss ON ss.session_id = pl.session_id
                    WHERE pf.opened_at IS NOT NULL
                )
                SELECT AVG((julianday(pp.opened_at) - julianday(bf.first_session)) * 24.0)
                FROM pr_primary pp
                JOIN branch_first bf ON bf.git_branch = pp.branch_name
                """
            ).fetchone()[0]
            push_row = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN g.git_op_type = 'push' THEN 1 ELSE 0 END) * 1.0,
                    SUM(CASE WHEN g.git_op_type = 'commit' THEN 1 ELSE 0 END)
                FROM git_operations g
                JOIN sessions s ON s.session_id = g.session_id
                WHERE {_scope_clause(scope, 's')}
                """
            ).fetchone()
            matched, total = _pr_coverage(conn, scope)
            push_rate = (push_row[0] or 0) / max(push_row[1] or 0, 1)
            return {
                "prs_week": prs_week or 0,
                "commits_day": (commits_7d or 0) / 7.0,
                "avg_lead_time_h": avg_lead or 0,
                "push_rate": push_rate,
                "coverage_num": matched,
                "coverage_den": total,
                "confidence": _confidence_label(matched, total),
            }
    except sqlite3.Error:
        return {}


def get_weekly_throughput(weeks: int = 8, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                f"""
                WITH scoped_sessions AS (
                    SELECT * FROM sessions s WHERE {_scope_clause(scope, 's')}
                ),
                branch_first AS (
                    SELECT git_branch, MIN(first_timestamp) AS first_session
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_primary AS (
                    SELECT DISTINCT
                        pf.pr_url,
                        COALESCE(pf.head_branch, ss.git_branch) AS branch_name,
                        pf.opened_at,
                        pf.merged_at
                    FROM pr_facts pf
                    JOIN pr_links pl ON pl.pr_url = pf.pr_url
                    JOIN scoped_sessions ss ON ss.session_id = pl.session_id
                ),
                created_by_week AS (
                    SELECT strftime('%Y-W%W', opened_at) AS week, COUNT(*) AS prs_created
                    FROM pr_primary
                    WHERE opened_at IS NOT NULL
                    GROUP BY week
                ),
                merged_by_week AS (
                    SELECT strftime('%Y-W%W', merged_at) AS week, COUNT(*) AS prs_merged
                    FROM pr_primary
                    WHERE merged_at IS NOT NULL
                    GROUP BY week
                ),
                commits_by_week AS (
                    SELECT strftime('%Y-W%W', g.timestamp) AS week, COUNT(*) AS commits
                    FROM git_operations g
                    JOIN scoped_sessions ss ON ss.session_id = g.session_id
                    WHERE g.git_op_type = 'commit' AND g.timestamp IS NOT NULL
                    GROUP BY week
                ),
                lead_by_week AS (
                    SELECT
                        strftime('%Y-W%W', pp.opened_at) AS week,
                        AVG((julianday(pp.opened_at) - julianday(bf.first_session)) * 24.0) AS avg_lead_h
                    FROM pr_primary pp
                    JOIN branch_first bf ON bf.git_branch = pp.branch_name
                    WHERE pp.opened_at IS NOT NULL
                    GROUP BY week
                ),
                weeks_to_show AS (
                    SELECT week FROM created_by_week
                    UNION
                    SELECT week FROM merged_by_week
                    UNION
                    SELECT week FROM commits_by_week
                    ORDER BY week DESC
                    LIMIT ?
                )
                SELECT
                    w.week,
                    COALESCE(cbw.prs_created, 0),
                    COALESCE(mbw.prs_merged, 0),
                    COALESCE(cmw.commits, 0),
                    COALESCE(lbw.avg_lead_h, 0)
                FROM weeks_to_show w
                LEFT JOIN created_by_week cbw ON cbw.week = w.week
                LEFT JOIN merged_by_week mbw ON mbw.week = w.week
                LEFT JOIN commits_by_week cmw ON cmw.week = w.week
                LEFT JOIN lead_by_week lbw ON lbw.week = w.week
                ORDER BY w.week DESC
                """,
                (weeks,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_recent_prs(limit: int = 10, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                f"""
                SELECT
                    substr(COALESCE(pf.opened_at, pl.timestamp), 1, 10) AS date,
                    COALESCE(pf.pr_number, pl.pr_number) AS pr_number,
                    COALESCE(pf.repo_full_name, pl.pr_repository) AS repo_name,
                    s.git_branch
                FROM pr_links pl
                JOIN sessions s ON s.session_id = pl.session_id
                LEFT JOIN pr_facts pf ON pf.pr_url = pl.pr_url
                WHERE {_scope_clause(scope, 's')}
                ORDER BY COALESCE(pf.opened_at, pl.timestamp) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_pr_lifecycle(limit: int = 15, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            matched, total = _pr_coverage(conn, scope)
            confidence = _confidence_label(matched, total)
            primary = conn.execute(
                f"""
                WITH scoped_sessions AS (
                    SELECT * FROM sessions s WHERE {_scope_clause(scope, 's')}
                ),
                branch_first AS (
                    SELECT git_branch, MIN(first_timestamp) AS first_session
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                branch_sessions AS (
                    SELECT git_branch, COUNT(DISTINCT session_id) AS sessions
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                branch_commits AS (
                    SELECT ss.git_branch, COUNT(*) AS commits
                    FROM git_operations g
                    JOIN scoped_sessions ss ON ss.session_id = g.session_id
                    WHERE g.git_op_type = 'commit'
                    GROUP BY ss.git_branch
                ),
                pr_primary AS (
                    SELECT DISTINCT
                        pf.pr_url,
                        COALESCE(pf.head_branch, ss.git_branch) AS branch_name,
                        pf.opened_at
                    FROM pr_facts pf
                    JOIN pr_links pl ON pl.pr_url = pf.pr_url
                    JOIN scoped_sessions ss ON ss.session_id = pl.session_id
                    WHERE pf.opened_at IS NOT NULL
                )
                SELECT
                    pp.branch_name AS git_branch,
                    (julianday(pp.opened_at) - julianday(bf.first_session)) * 24.0 AS lead_time_h,
                    COALESCE(bs.sessions, 0) AS sessions,
                    COALESCE(bc.commits, 0) AS commits,
                    substr(bf.first_session, 1, 10) AS first_session_date,
                    substr(pp.opened_at, 1, 10) AS pr_created_date
                FROM pr_primary pp
                JOIN branch_first bf ON bf.git_branch = pp.branch_name
                LEFT JOIN branch_sessions bs ON bs.git_branch = pp.branch_name
                LEFT JOIN branch_commits bc ON bc.git_branch = pp.branch_name
                ORDER BY lead_time_h DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            if primary:
                return [tuple(row) + (confidence,) for row in primary]

            fallback = conn.execute(
                f"""
                WITH scoped_sessions AS (
                    SELECT * FROM sessions s WHERE {_scope_clause(scope, 's')}
                ),
                branch_sessions AS (
                    SELECT
                        git_branch,
                        MIN(first_timestamp) AS first_session,
                        COUNT(DISTINCT session_id) AS sessions
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                ),
                pr_times AS (
                    SELECT ss.git_branch, MIN(g.timestamp) AS pr_created
                    FROM git_operations g
                    JOIN scoped_sessions ss ON ss.session_id = g.session_id
                    WHERE g.git_op_type = 'pr_create'
                    GROUP BY ss.git_branch
                ),
                branch_commits AS (
                    SELECT ss.git_branch, COUNT(*) AS commits
                    FROM git_operations g
                    JOIN scoped_sessions ss ON ss.session_id = g.session_id
                    WHERE g.git_op_type = 'commit'
                    GROUP BY ss.git_branch
                )
                SELECT
                    bs.git_branch,
                    (julianday(pt.pr_created) - julianday(bs.first_session)) * 24.0 AS lead_time_h,
                    bs.sessions,
                    COALESCE(bc.commits, 0) AS commits,
                    substr(bs.first_session, 1, 10) AS first_session_date,
                    substr(pt.pr_created, 1, 10) AS pr_created_date
                FROM branch_sessions bs
                JOIN pr_times pt ON bs.git_branch = pt.git_branch
                LEFT JOIN branch_commits bc ON bs.git_branch = bc.git_branch
                ORDER BY lead_time_h DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [tuple(row) + ("LOW",) for row in fallback]
    except sqlite3.Error:
        return []


def get_pr_commit_timing_summary(scope: Scope = "all_activity") -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute(
                _pr_commit_rollup_sql(scope)
                + """
                SELECT
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN pre_commits END), 0),
                    COALESCE(AVG(CASE WHEN confidence != 'LOW' THEN post_commits END), 0),
                    COALESCE(
                        SUM(CASE WHEN confidence != 'LOW' THEN post_commits ELSE 0 END) * 1.0
                        / NULLIF(
                            SUM(CASE WHEN confidence != 'LOW' THEN (pre_commits + post_commits) ELSE 0 END),
                            0
                        ),
                        0.0
                    ),
                    SUM(CASE WHEN confidence != 'LOW' THEN 1 ELSE 0 END) AS covered,
                    COUNT(*) AS total,
                    SUM(CASE WHEN confidence = 'HIGH' THEN 1 ELSE 0 END) AS high_count,
                    SUM(CASE WHEN confidence = 'MED' THEN 1 ELSE 0 END) AS med_count,
                    SUM(CASE WHEN confidence = 'LOW' THEN 1 ELSE 0 END) AS low_count
                FROM rollup
                """
            ).fetchone()
            covered = int((row[3] if row else 0) or 0)
            total = int((row[4] if row else 0) or 0)
            return {
                "avg_pre": float((row[0] if row else 0) or 0),
                "avg_post": float((row[1] if row else 0) or 0),
                "post_ratio": float((row[2] if row else 0) or 0),
                "coverage_num": covered,
                "coverage_den": total,
                "high_count": int((row[5] if row else 0) or 0),
                "med_count": int((row[6] if row else 0) or 0),
                "low_count": int((row[7] if row else 0) or 0),
                "confidence": _confidence_label(covered, total),
            }
    except sqlite3.Error:
        return {}


def get_pr_post_open_commit_outliers(limit: int = 10, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                _pr_commit_rollup_sql(scope)
                + """
                SELECT
                    pr_number,
                    repo_full_name,
                    opened_at,
                    merged_at,
                    pre_commits,
                    post_commits,
                    CASE
                        WHEN (pre_commits + post_commits) > 0
                        THEN (post_commits * 100.0) / (pre_commits + post_commits)
                        ELSE 0
                    END AS post_pct,
                    confidence
                FROM rollup
                WHERE confidence != 'LOW'
                ORDER BY post_commits DESC, post_pct DESC, opened_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_pr_commit_timing_details(limit: int = 15, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                _pr_commit_rollup_sql(scope)
                + """
                SELECT
                    pr_number,
                    repo_full_name,
                    opened_at,
                    merged_at,
                    pre_commits,
                    post_commits,
                    CASE
                        WHEN (pre_commits + post_commits) > 0
                        THEN (post_commits * 100.0) / (pre_commits + post_commits)
                        ELSE 0
                    END AS post_pct,
                    confidence
                FROM rollup
                ORDER BY opened_at DESC, post_commits DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_rework_hotspots(limit: int = 10, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                f"""
                WITH scoped_sessions AS (
                    SELECT * FROM sessions s WHERE {_scope_clause(scope, 's')}
                ),
                branch_sessions AS (
                    SELECT
                        git_branch,
                        COUNT(DISTINCT session_id) AS sessions,
                        SUM(COALESCE(total_duration_ms, 0)) / 1000.0 AS total_duration_s
                    FROM scoped_sessions
                    WHERE git_branch IS NOT NULL
                    GROUP BY git_branch
                    HAVING sessions > 2
                ),
                branch_commits AS (
                    SELECT ss.git_branch, COUNT(*) AS commits
                    FROM git_operations g
                    JOIN scoped_sessions ss ON ss.session_id = g.session_id
                    WHERE g.git_op_type = 'commit'
                    GROUP BY ss.git_branch
                )
                SELECT
                    bs.git_branch,
                    bs.sessions,
                    COALESCE(bc.commits, 0) AS commits,
                    bs.total_duration_s,
                    CAST(COALESCE(bc.commits, 0) AS REAL) / bs.sessions AS commits_per_session
                FROM branch_sessions bs
                LEFT JOIN branch_commits bc ON bs.git_branch = bc.git_branch
                ORDER BY bs.sessions DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_efficiency_metrics(scope: Scope = "all_activity") -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            total_sessions = conn.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {_scope_clause(scope, 's')}"
            ).fetchone()[0] or 1
            productive = conn.execute(
                f"""
                SELECT COUNT(DISTINCT s.session_id)
                FROM sessions s
                JOIN git_operations g ON g.session_id = s.session_id
                WHERE {_scope_clause(scope, 's')}
                  AND g.git_op_type = 'commit'
                """
            ).fetchone()[0]
            avg_dur = conn.execute(
                f"""
                SELECT AVG(total_duration_ms)
                FROM sessions s
                WHERE {_scope_clause(scope, 's')} AND total_duration_ms IS NOT NULL
                """
            ).fetchone()[0]
            sub_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
            main_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_subagent=0").fetchone()[0]
            total_commits = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM git_operations g
                JOIN sessions s ON s.session_id = g.session_id
                WHERE {_scope_clause(scope, 's')}
                  AND g.git_op_type = 'commit'
                """
            ).fetchone()[0] or 1
            rows = conn.execute(
                f"""
                SELECT usage_by_model
                FROM sessions s
                WHERE {_scope_clause(scope, 's')} AND usage_by_model IS NOT NULL
                """
            ).fetchall()
            total_tokens = 0
            for (ubm,) in rows:
                try:
                    for md in json.loads(ubm).values():
                        total_tokens += md.get("input", 0) + md.get("output", 0)
                except (json.JSONDecodeError, AttributeError):
                    pass
            return {
                "productivity_rate": productive / total_sessions * 100,
                "tokens_per_commit": total_tokens / total_commits,
                "avg_duration_s": (avg_dur or 0) / 1000.0,
                "subagent_ratio": sub_count / max(main_count, 1),
            }
    except sqlite3.Error:
        return {}


def get_model_efficiency(scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            model_stats: dict[str, dict] = {}
            for model, cnt, avg_dur in conn.execute(
                f"""
                SELECT model, COUNT(*) AS sessions, AVG(total_duration_ms)
                FROM sessions s
                WHERE {_scope_clause(scope, 's')} AND model IS NOT NULL
                GROUP BY model ORDER BY sessions DESC
                """
            ).fetchall():
                model_stats[model] = {
                    "sessions": cnt,
                    "avg_dur_s": (avg_dur or 0) / 1000.0,
                    "tokens": 0,
                    "commits": 0,
                }
            for model, commits in conn.execute(
                f"""
                SELECT s.model, COUNT(*) AS commits
                FROM sessions s
                JOIN git_operations g ON g.session_id = s.session_id
                WHERE {_scope_clause(scope, 's')}
                  AND g.git_op_type = 'commit' AND s.model IS NOT NULL
                GROUP BY s.model
                """
            ).fetchall():
                if model in model_stats:
                    model_stats[model]["commits"] = commits
            for model, ubm in conn.execute(
                f"""
                SELECT model, usage_by_model
                FROM sessions s
                WHERE {_scope_clause(scope, 's')}
                  AND model IS NOT NULL AND usage_by_model IS NOT NULL
                """
            ).fetchall():
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


def get_unproductive_sessions(limit: int = 10, scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                f"""
                SELECT
                    substr(s.first_timestamp, 1, 10),
                    s.total_duration_ms / 1000.0,
                    s.model,
                    s.first_user_message
                FROM sessions s
                LEFT JOIN (
                    SELECT g.session_id
                    FROM git_operations g
                    GROUP BY g.session_id
                ) gop ON gop.session_id = s.session_id
                WHERE {_scope_clause(scope, 's')}
                    AND s.total_duration_ms > 60000
                    AND gop.session_id IS NULL
                ORDER BY s.total_duration_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []


def get_tool_usage_enhanced(scope: Scope = "all_activity") -> list[tuple]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            return conn.execute(
                f"""
                SELECT
                    st.tool_name,
                    SUM(st.call_count) AS total_calls,
                    COUNT(DISTINCT st.session_id) AS session_count,
                    AVG(st.call_count) AS avg_per_session
                FROM session_tool_summary st
                JOIN sessions s ON s.session_id = st.session_id
                WHERE {_scope_clause(scope, 's')}
                GROUP BY st.tool_name
                ORDER BY total_calls DESC
                LIMIT 30
                """
            ).fetchall()
    except sqlite3.Error:
        return []


def get_aggregate_pipeline(scope: Scope = "all_activity") -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            phase_rows = conn.execute(
                f"""
                SELECT e.phase, COUNT(*) AS total_calls,
                       COUNT(DISTINCT e.session_id) AS session_count,
                       SUM(e.duration_ms) AS total_ms
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE {_scope_clause(scope, 's')}
                GROUP BY e.phase
                """
            ).fetchall()
            tool_rows = conn.execute(
                f"""
                SELECT e.phase, e.tool_name, COUNT(*) AS cnt
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE {_scope_clause(scope, 's')}
                GROUP BY e.phase, e.tool_name
                ORDER BY e.phase, cnt DESC
                """
            ).fetchall()
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


def get_recent_sessions_for_select(limit: int = 30, scope: Scope = "all_activity") -> list[tuple[str, str]]:
    if not DB_FILE.exists():
        return []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT DISTINCT e.session_id, s.slug, s.first_timestamp
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE {_scope_clause(scope, 's')}
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


def get_default_code_rate(scope: Scope = "all_activity") -> float:
    meta = _get_meta("default_code_rate")
    if meta:
        try:
            val = float(meta)
            if scope == "all_activity":
                return val
        except (TypeError, ValueError):
            pass
    if not DB_FILE.exists():
        return 0.0
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN e.phase = 'Code' THEN 1 ELSE 0 END),
                    COUNT(*)
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE {_scope_clause(scope, 's')}
                """
            ).fetchone()
            code_events = int((row[0] if row else 0) or 0)
            all_events = int((row[1] if row else 0) or 0)
            return (code_events / all_events) if all_events else 0.0
    except sqlite3.Error:
        return 0.0


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
    _scope: Scope = "all_activity"

    def compose(self) -> ComposeResult:
        yield AnimatedBanner()
        with Horizontal(id="scope-row"):
            yield Label("Scope")
            yield Select(
                [("All Activity", "all_activity"), ("Delivery Only", "delivery_only")],
                id="scope-select",
            )
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
            with TabPane("PR Commit Timing", id="pr-commit-timing"):
                with Vertical():
                    yield Static(id="pr-commit-banner")
                    yield Label("[bold] Post-Open Commit Outliers[/bold]")
                    yield DataTable(id="pr-commit-outliers-table", cursor_type="row", zebra_stripes=True)
                    yield Label("[bold] PR Commit Timing Details[/bold]")
                    yield DataTable(id="pr-commit-details-table", cursor_type="row", zebra_stripes=True)
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
        scope_select = self.query_one("#scope-select", Select)
        scope_select.value = "all_activity"
        self._scope = "all_activity"
        self._populate_all()

    @staticmethod
    def _prepare_table(table: DataTable, columns: tuple[str, ...]) -> None:
        if len(table.columns) == 0:
            table.add_columns(*columns)
        else:
            table.clear()

    def _populate_all(self) -> None:
        self._populate_throughput()
        self._populate_lead_time()
        self._populate_pr_commit_timing()
        self._populate_efficiency()
        self._populate_value_stream()

    def _populate_throughput(self) -> None:
        v = get_velocity_banner(self._scope)
        prs = v.get("prs_week", 0)
        cpd = v.get("commits_day", 0)
        lt = v.get("avg_lead_time_h")
        pr_pct = v.get("push_rate", 0) * 100
        conf = str(v.get("confidence", "LOW"))
        cov_num = int(v.get("coverage_num", 0) or 0)
        cov_den = int(v.get("coverage_den", 0) or 0)

        c_prs = _threshold_color(prs, *_THRESH["prs_week"])
        c_cpd = _threshold_color(cpd, *_THRESH["commits_day"])
        c_lt  = _threshold_color(lt, *_THRESH["lead_time_h"], lower_is_better=True)
        c_pr  = _threshold_color(pr_pct, *_THRESH["push_rate_pct"])
        c_conf = _confidence_style(conf)

        self.query_one("#velocity-banner", Static).update(
            f" Scope [bold]{_scope_label(self._scope)}[/bold]"
            f"  ·  Confidence [{c_conf}]{conf}[/]"
            f" ({cov_num}/{cov_den})"
            f"  ·"
            f" PRs/week {_colored(str(prs), c_prs)}"
            f"  ·  Commits/day {_colored(f'{cpd:.1f}', c_cpd)}"
            f"  ·  Avg lead time {_colored(f'{lt or 0:.1f}h', c_lt)}"
            f"  ·  Push rate {_colored(f'{pr_pct:.1f}%', c_pr)}"
        )
        wt = self.query_one("#weekly-throughput-table", DataTable)
        self._prepare_table(wt, ("Week", "PRs Created", "PRs Merged", "Commits", "Lead Time (avg)"))
        for week, prs_c, prs_m, commits, lead_h in get_weekly_throughput(scope=self._scope):
            lead_h_clamped, c = _lead_time_clamp_color(lead_h)
            lead_cell = Text(f"{lead_h_clamped:.1f}h" if lead_h_clamped is not None else "—", style=f"bold {c}" if c else "")
            wt.add_row(week, str(prs_c), str(prs_m), str(commits), lead_cell)
        rt = self.query_one("#recent-prs-table", DataTable)
        self._prepare_table(rt, ("Date", "PR#", "Repository", "Branch"))
        for date, pr_num, repo, branch in get_recent_prs(scope=self._scope):
            rt.add_row(date or "", f"#{pr_num}" if pr_num else "—",
                       (repo or "")[:30], (branch or "")[:40])

    def _populate_lead_time(self) -> None:
        lc = self.query_one("#pr-lifecycle-table", DataTable)
        self._prepare_table(lc, ("Branch", "Lead Time", "Sessions", "Commits", "First Session", "PR Created", "Conf"))
        for branch, lead_h, sessions, commits, first_s, pr_c, conf in get_pr_lifecycle(scope=self._scope):
            lead_h_clamped, c = _lead_time_clamp_color(lead_h)
            lead_cell = Text(_fmt_duration(lead_h_clamped * 3600) if lead_h_clamped is not None else "—", style=f"bold {c}" if c else "")
            conf_cell = Text(conf, style=_confidence_style(conf))
            lc.add_row((branch or "")[:40], lead_cell,
                       str(sessions), str(commits), first_s or "", pr_c or "", conf_cell)
        rw = self.query_one("#rework-table", DataTable)
        self._prepare_table(rw, ("Branch", "Sessions", "Commits", "Total Duration", "Commits/Session"))
        for branch, sessions, commits, dur_s, cps in get_rework_hotspots(scope=self._scope):
            rw.add_row((branch or "")[:40], str(sessions), str(commits),
                       _fmt_duration(dur_s), f"{cps:.1f}")

    def _populate_pr_commit_timing(self) -> None:
        summary = get_pr_commit_timing_summary(self._scope)
        avg_pre = float(summary.get("avg_pre", 0) or 0)
        avg_post = float(summary.get("avg_post", 0) or 0)
        post_ratio = float(summary.get("post_ratio", 0) or 0) * 100.0
        cov_num = int(summary.get("coverage_num", 0) or 0)
        cov_den = int(summary.get("coverage_den", 0) or 0)
        conf = str(summary.get("confidence", "LOW"))
        high_cnt = int(summary.get("high_count", 0) or 0)
        med_cnt = int(summary.get("med_count", 0) or 0)
        low_cnt = int(summary.get("low_count", 0) or 0)

        c_conf = _confidence_style(conf)
        self.query_one("#pr-commit-banner", Static).update(
            f" Scope [bold]{_scope_label(self._scope)}[/bold]"
            f"  ·  Confidence [{c_conf}]{conf}[/] ({cov_num}/{cov_den})"
            f"  ·  Avg pre-PR commits [bold]{avg_pre:.1f}[/bold]"
            f"  ·  Avg post-PR commits [bold]{avg_post:.1f}[/bold]"
            f"  ·  Post-open ratio [bold]{post_ratio:.1f}%[/bold]"
            f"  ·  Bands [bold]H:{high_cnt} M:{med_cnt} L:{low_cnt}[/bold]"
        )

        outliers = self.query_one("#pr-commit-outliers-table", DataTable)
        self._prepare_table(outliers, ("PR", "Repo", "Opened", "Merged", "Pre", "Post", "Post %", "Confidence"))
        for pr_num, repo, opened, merged, pre_c, post_c, post_pct, confidence in get_pr_post_open_commit_outliers(
            scope=self._scope
        ):
            conf_cell = Text(str(confidence), style=_confidence_style(str(confidence)))
            outliers.add_row(
                f"#{pr_num}" if pr_num else "—",
                (repo or "")[:36],
                str(opened or "")[:10],
                str(merged or "")[:10],
                str(pre_c or 0),
                str(post_c or 0),
                f"{float(post_pct or 0):.1f}%",
                conf_cell,
            )

        details = self.query_one("#pr-commit-details-table", DataTable)
        self._prepare_table(details, ("PR", "Repo", "Opened", "Merged", "Pre", "Post", "Post %", "Confidence"))
        for pr_num, repo, opened, merged, pre_c, post_c, post_pct, confidence in get_pr_commit_timing_details(
            scope=self._scope
        ):
            conf_cell = Text(str(confidence), style=_confidence_style(str(confidence)))
            details.add_row(
                f"#{pr_num}" if pr_num else "—",
                (repo or "")[:36],
                str(opened or "")[:10],
                str(merged or "")[:10],
                str(pre_c or 0),
                str(post_c or 0),
                f"{float(post_pct or 0):.1f}%",
                conf_cell,
            )

    def _populate_efficiency(self) -> None:
        e = get_efficiency_metrics(self._scope)
        prod = e.get('productivity_rate', 0)
        c_prod = _threshold_color(prod, *_THRESH["productivity"])
        self.query_one("#efficiency-banner", Static).update(
            f" Scope [bold]{_scope_label(self._scope)}[/bold]"
            f"  ·"
            f" Productivity {_colored(f'{prod:.0f}%', c_prod)}"
            f"  ·  Tokens/commit [bold]{_fmt_tokens(int(e.get('tokens_per_commit', 0)))}[/bold]"
            f"  ·  Avg session [bold]{_fmt_duration(e.get('avg_duration_s', 0))}[/bold]"
            f"  ·  Subagent ratio [bold]{e.get('subagent_ratio', 0):.1f}x[/bold]"
        )
        me = self.query_one("#model-efficiency-table", DataTable)
        self._prepare_table(me, ("Model", "Sessions", "Avg Duration", "Commits", "Tokens/Commit"))
        for model, sessions, avg_dur_s, commits, tpc in get_model_efficiency(self._scope):
            me.add_row(model or "unknown", f"{sessions:,}", _fmt_duration(avg_dur_s),
                       f"{commits:,}", _fmt_tokens(int(tpc)) if tpc else "—")
        ut = self.query_one("#unproductive-table", DataTable)
        self._prepare_table(ut, ("Date", "Duration", "Model", "First Prompt"))
        for date, dur_s, model, prompt in get_unproductive_sessions(scope=self._scope):
            dur_h = (dur_s / 3600) if dur_s is not None else None
            c = _threshold_color(dur_h, *_THRESH["unproductive_h"], lower_is_better=True)
            dur_cell = Text(_fmt_duration(dur_s), style=f"bold {c}" if c else "")
            ut.add_row(date or "", dur_cell, model or "unknown",
                       (prompt or "")[:80])
        tt = self.query_one("#tool-usage-table", DataTable)
        self._prepare_table(tt, ("Tool", "Total Calls", "Sessions", "Avg/Session"))
        for i, (tool, total, session_cnt, avg) in enumerate(get_tool_usage_enhanced(self._scope)):
            if i < 3:
                style = "bold"
            elif i < 10:
                style = ""
            else:
                style = "dim"
            tt.add_row(Text(tool, style=style), f"{total:,}", f"{session_cnt:,}", f"{avg:.1f}")


    def _populate_value_stream(self) -> None:
        pt = self.query_one("#phase-table", DataTable)
        self._prepare_table(pt, ("Phase", "Calls", "Time %", "Sessions", "Top Tools"))
        spt = self.query_one("#session-phase-table", DataTable)
        self._prepare_table(spt, ("Phase", "Calls", "Time %", "Top Tools"))

        phases = get_aggregate_pipeline(self._scope)
        banner = self.query_one("#vs-info-banner", Static)
        code_rate = get_default_code_rate(self._scope) * 100
        if not phases:
            banner.update(
                f"[yellow]No event data — run extraction to populate value stream.[/yellow] "
                f"[dim](Scope: {_scope_label(self._scope)} · default-to-Code {code_rate:.1f}%)[/dim]"
            )
            return
        banner.update(
            f"[dim]Scope: {_scope_label(self._scope)} · default-to-Code rate: {code_rate:.1f}%[/dim]"
        )
        self.query_one("#phase-bar", Static).update(render_phase_bar(phases))

        for p in sorted(phases, key=lambda x: _PHASE_ORDER.index(x["phase"]) if x["phase"] in _PHASE_ORDER else 99):
            tools_str = ", ".join(t[0] for t in p["top_tools"][:3])
            pt.add_row(_phase_cell(p["phase"]), f"{p['total_calls']:,}", f"{p['time_pct']}%",
                       f"{p['session_count']:,}", tools_str)

        sessions = get_recent_sessions_for_select(scope=self._scope)
        sel = self.query_one("#session-select", Select)
        if sessions:
            sel.set_options((label, sid) for label, sid in sessions)
            sel.value = sessions[0][1]
            self._populate_session_breakdown(sessions[0][1])
        else:
            sel.set_options([])
            self.query_one("#session-phase-bar", Static).update("[dim]No sessions for this scope.[/dim]")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "scope-select":
            if event.value is Select.BLANK:
                return
            self._scope = str(event.value)  # type: ignore[assignment]
            self._populate_all()
            return

        if event.select.id != "session-select" or event.value is Select.BLANK:
            return
        self._populate_session_breakdown(str(event.value))

    def _populate_session_breakdown(self, session_id: str) -> None:
        phases = get_session_pipeline(session_id)
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
        if _schema_outdated():
            self.notify(
                "Schema changed — run full backfill: `python3 sdlc_extract.py --full` and `--enrich-only`.",
                severity="warning",
            )

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
            row = db.conn.execute(
                "SELECT SUM(CASE WHEN phase='Code' THEN 1 ELSE 0 END), COUNT(*) FROM session_events"
            ).fetchone()
            code_events = int((row[0] if row else 0) or 0)
            all_events = int((row[1] if row else 0) or 0)
            default_code_rate = (code_events / all_events) if all_events else 0.0
            db.set_meta("schema_version", SCHEMA_VERSION)
            db.set_meta("default_code_rate", f"{default_code_rate:.4f}")
            db.set_meta("github_enrich_errors", "0")
            prev_enrich = db.conn.execute(
                "SELECT value FROM extraction_meta WHERE key = 'github_enrich_last_run'"
            ).fetchone()
            db.set_meta("github_enrich_last_run", (prev_enrich[0] if prev_enrich else ""))
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
    #scope-row {
        height: auto;
        padding: 0 2;
    }
    #scope-select {
        width: 28;
        margin-left: 1;
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
    ap.add_argument(
        "--cli",
        action="store_true",
        help="Legacy alias for AI mode stats output.",
    )
    ap.add_argument(
        "--ai",
        action="store_true",
        help="AI mode: non-interactive JSON command interface.",
    )
    ap.add_argument(
        "--scope",
        choices=("all_activity", "delivery_only"),
        default="all_activity",
        help="Scope for --cli output (default: all_activity).",
    )
    ap.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Filter --cli output to repository full name (owner/name). Repeat for multiple repos.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Row limit for --cli tables (default: 10).",
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON for AI/CLI output.",
    )
    ap.add_argument(
        "--command",
        choices=(
            "stats",
            "status",
            "projects.list",
            "config.get",
            "config.set",
            "extract.run",
            "update.check",
            "update.apply",
            "uninstall",
        ),
        default="stats",
        help="AI/CLI command to execute (default: stats).",
    )
    ap.add_argument(
        "--project",
        action="append",
        dest="projects",
        help="Project name/display for config.set or extract.run. Repeat for multiple values.",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Use full re-extraction for extract.run.",
    )
    ap.add_argument(
        "--enrich",
        action="store_true",
        help="Run GitHub enrichment during extract.run.",
    )
    ap.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="Token env var for extract.run --enrich (default: GITHUB_TOKEN).",
    )
    ap.add_argument(
        "--github-max-prs",
        type=int,
        default=500,
        help="Max PR links for extract.run --enrich (default: 500).",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Confirmation flag for destructive commands such as uninstall.",
    )
    ap.add_argument(
        "--remove-db",
        action="store_true",
        help="When used with uninstall, also remove the SQLite DB and WAL/SHM files.",
    )
    args = ap.parse_args()
    if args.cli or args.ai:
        payload = run_ai_command(args)
        if args.compact:
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print(json.dumps(payload, indent=2))
        return
    SdlcApp().run()

if __name__ == "__main__":
    main()
