#!/usr/bin/env python3
"""SDLC Session Analytics Extraction Script

Processes Claude Code session JSONL files into a SQLite database for
SDLC flow analysis: rework loops, post-PR churn, wasted sessions.

Usage: python3 sdlc_extract.py [-v] [--full] [--db PATH] [--project-dirs DIR...]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# Database
# ============================================================

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    source_dir         TEXT,
    source_file        TEXT,
    is_subagent        INTEGER NOT NULL DEFAULT 0,
    parent_session_id  TEXT,
    slug               TEXT,
    first_timestamp    TEXT,
    last_timestamp     TEXT,
    total_duration_ms  INTEGER,
    git_branch         TEXT,
    cwd                TEXT,
    permission_mode    TEXT,
    model              TEXT,
    claude_version     TEXT,
    summary_text       TEXT,
    first_user_message TEXT,
    usage_by_model     TEXT,
    facets_json        TEXT,
    file_size_bytes    INTEGER,
    file_mtime         REAL
);

CREATE TABLE IF NOT EXISTS session_tool_summary (
    session_id TEXT NOT NULL,
    tool_name  TEXT NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, tool_name)
);

CREATE TABLE IF NOT EXISTS git_operations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    timestamp      TEXT,
    git_op_type    TEXT NOT NULL,
    command_text   TEXT,
    commit_message TEXT,
    branch_name    TEXT
);

CREATE TABLE IF NOT EXISTS pr_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    pr_number     INTEGER,
    pr_url        TEXT,
    pr_repository TEXT,
    timestamp     TEXT,
    UNIQUE (session_id, pr_number)
);

CREATE TABLE IF NOT EXISTS extraction_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_branch    ON sessions(git_branch);
CREATE INDEX IF NOT EXISTS idx_sessions_slug      ON sessions(slug);
CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sessions(first_timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_subagent  ON sessions(is_subagent);
CREATE INDEX IF NOT EXISTS idx_git_ops_session    ON git_operations(session_id);
CREATE INDEX IF NOT EXISTS idx_git_ops_type       ON git_operations(git_op_type);
CREATE INDEX IF NOT EXISTS idx_pr_links_pr        ON pr_links(pr_number);

CREATE TABLE IF NOT EXISTS session_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    timestamp   TEXT,
    tool_name   TEXT NOT NULL,
    phase       TEXT NOT NULL,
    duration_ms INTEGER,
    detail      TEXT,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_phase   ON session_events(phase);
"""


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def get_file_meta(self, source_file: str):
        """Returns (file_size_bytes, file_mtime, session_id) or None."""
        return self.conn.execute(
            "SELECT file_size_bytes, file_mtime, session_id FROM sessions WHERE source_file = ?",
            (source_file,),
        ).fetchone()

    def delete_session(self, session_id: str):
        for table in ("session_tool_summary", "git_operations", "pr_links", "session_events", "sessions"):
            self.conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))

    def upsert_session(self, data: dict):
        cols = list(data.keys())
        self.conn.execute(
            f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            list(data.values()),
        )

    def insert_tools(self, session_id: str, counts: dict):
        self.conn.executemany(
            "INSERT OR REPLACE INTO session_tool_summary VALUES (?, ?, ?)",
            [(session_id, name, cnt) for name, cnt in counts.items()],
        )

    def insert_git_ops(self, ops: list):
        self.conn.executemany(
            "INSERT INTO git_operations"
            " (session_id, timestamp, git_op_type, command_text, commit_message, branch_name)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (op["session_id"], op.get("timestamp"), op["git_op_type"],
                 op.get("command_text"), op.get("commit_message"), op.get("branch_name"))
                for op in ops
            ],
        )

    def insert_pr_links(self, links: list):
        self.conn.executemany(
            "INSERT OR IGNORE INTO pr_links"
            " (session_id, pr_number, pr_url, pr_repository, timestamp) VALUES (?, ?, ?, ?, ?)",
            [
                (lnk["session_id"], lnk.get("pr_number"), lnk.get("pr_url"),
                 lnk.get("pr_repository"), lnk.get("timestamp"))
                for lnk in links
            ],
        )

    def insert_events(self, events: list[dict]) -> None:
        if not events:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO session_events "
            "(session_id, seq, timestamp, tool_name, phase, duration_ms, detail) "
            "VALUES (:session_id, :seq, :timestamp, :tool_name, :phase, :duration_ms, :detail)",
            events,
        )

    def update_facets(self, session_id: str, facets_json: str):
        self.conn.execute(
            "UPDATE sessions SET facets_json = ? WHERE session_id = ?",
            (facets_json, session_id),
        )

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO extraction_meta VALUES (?, ?)", (key, value)
        )

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# Git Operation Extractor
# ============================================================

_RE_COMMIT_INLINE = re.compile(r"""-m\s+(?:"(.*?)"|'(.*?)')""", re.DOTALL)
_RE_COMMIT_HEREDOC = re.compile(r"<<['\"]?EOF['\"]?\s*\n(.*?)\n\s*EOF", re.DOTALL)
_RE_CHECKOUT_BRANCH = re.compile(r"git\s+checkout\s+(?:-b|-B)\s+(\S+)", re.IGNORECASE)


class GitOpExtractor:
    def extract(self, command: str, session_id: str, timestamp: str | None) -> list:
        ops = []
        cmd_lower = command.lower()
        cmd_text = command[:1000]

        if "git commit" in cmd_lower:
            ops.append(self._op("commit", session_id, timestamp, cmd_text,
                                commit_message=self._commit_msg(command)))

        if "git push" in cmd_lower:
            ops.append(self._op("push", session_id, timestamp, cmd_text))

        if "gh pr create" in cmd_lower:
            ops.append(self._op("pr_create", session_id, timestamp, cmd_text))

        if "gh pr merge" in cmd_lower:
            ops.append(self._op("pr_merge", session_id, timestamp, cmd_text))

        if re.search(r"git\s+checkout\s+(?:-b|-B)", command, re.IGNORECASE):
            m = _RE_CHECKOUT_BRANCH.search(command)
            ops.append(self._op("branch_create", session_id, timestamp, cmd_text,
                                branch_name=m.group(1) if m else None))

        return ops

    def _commit_msg(self, command: str) -> str | None:
        m = _RE_COMMIT_INLINE.search(command)
        if m:
            return (m.group(1) or m.group(2) or "")[:500]
        m = _RE_COMMIT_HEREDOC.search(command)
        if m:
            return m.group(1)[:500]
        return None

    @staticmethod
    def _op(op_type, session_id, timestamp, cmd_text,
            commit_message=None, branch_name=None) -> dict:
        return {
            "session_id": session_id,
            "timestamp": timestamp,
            "git_op_type": op_type,
            "command_text": cmd_text,
            "commit_message": commit_message,
            "branch_name": branch_name,
        }


# ============================================================
# Phase Classification
# ============================================================

_DISCOVER = frozenset({
    "WebSearch", "WebFetch", "ListMcpResourcesTool", "ReadMcpResourceTool",
})

_PLAN = frozenset({
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "TodoWrite", "TodoRead",
    "TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "TeamCreate", "TeamDelete",
    "SendMessage",
})

_DELIVER_RE = re.compile(
    r"git\s+push|gh\s+pr\s+(create|merge)|heroku\s+deploy|vercel\s+deploy|kubectl\s+apply",
    re.IGNORECASE,
)
_TEST_RE = re.compile(
    r"pytest|jest|npm\s+test|yarn\s+test|pnpm\s+test|rspec|go\s+test|cargo\s+test"
    r"|phpunit|mvn\s+test|gradle\s+test|\.test\.\w+|_test\.py|spec\.\w+",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(r"gh\s+pr\s+(review|comment|view)\b", re.IGNORECASE)


def classify_tool(tool_name: str, tool_input: dict) -> str:
    """Map a tool call to an SDLC phase. Priority: Test > Review > Deliver > Plan > Discover > Code."""
    inp = tool_input or {}
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        if _TEST_RE.search(cmd):
            return "Test"
        if _REVIEW_RE.search(cmd):
            return "Review"
        if _DELIVER_RE.search(cmd):
            return "Deliver"
    if tool_name in {"Agent", "Skill"}:
        return "Review"
    if tool_name in _PLAN:
        return "Plan"
    if tool_name in _DISCOVER:
        return "Discover"
    return "Code"


def extract_detail(tool_name: str, tool_input: dict) -> str | None:
    """Extract a short human-readable detail string from a tool call."""
    inp = tool_input or {}
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookRead", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path")
        return Path(path).name if path else None
    if tool_name in ("Glob", "Grep"):
        return (inp.get("pattern") or "")[:100] or None
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:100] if cmd else None
    if tool_name == "WebSearch":
        return (inp.get("query") or "")[:100] or None
    if tool_name == "WebFetch":
        return (inp.get("url") or "")[:100] or None
    return None


# ============================================================
# JSONL Parser
# ============================================================

_SKIP_TYPES = frozenset({"progress", "queue-operation", "hook_progress"})


class JSONLParser:
    def parse(self, filepath: Path):
        """Yields (event_type, event_dict), skipping noise events."""
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype not in _SKIP_TYPES:
                        yield etype, ev
        except OSError:
            return


# ============================================================
# Session Extractor (Orchestrator)
# ============================================================

def _ts_ms(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return 0


def _first_text(content) -> str | None:
    """Extract first text string from a string or content-block array."""
    if isinstance(content, str):
        return content[:500] if content else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                return text[:500] if text else None
    return None


class SessionExtractor:
    def __init__(self, db: DB, verbose: bool = False):
        self.db = db
        self.verbose = verbose
        self.parser = JSONLParser()
        self.git_extractor = GitOpExtractor()

    def discover_files(self, project_dirs: list) -> list:
        """Returns [(filepath, is_subagent, parent_session_id), ...]."""
        files = []
        for d in project_dirs:
            project_dir = Path(d)
            if not project_dir.exists():
                if self.verbose:
                    print(f"  WARNING: {project_dir} not found", file=sys.stderr)
                continue
            # Main sessions: *.jsonl at top level of project dir
            for f in sorted(project_dir.glob("*.jsonl")):
                files.append((f, False, None))
            # Subagent sessions: {session_uuid}/subagents/*.jsonl
            for f in sorted(project_dir.glob("*/subagents/*.jsonl")):
                parent_uuid = f.parent.parent.name
                files.append((f, True, parent_uuid))
        return files

    def _process(self, filepath: Path, is_subagent: bool,
                 parent_id: str | None) -> dict | None:
        """Parse one JSONL file. Returns extracted data dict or None if unchanged."""
        stat = filepath.stat()
        size, mtime = stat.st_size, stat.st_mtime

        stored = self.db.get_file_meta(str(filepath))
        if stored and stored[0] == size and abs(stored[1] - mtime) < 0.01:
            return None  # file unchanged, skip

        session_id = f"{parent_id}:{filepath.stem}" if is_subagent else filepath.stem

        # Accumulators
        first_ts = last_ts = slug = branch = cwd = perm_mode = version = None
        summary_text = first_user_msg = None
        model_hits: dict[str, int] = defaultdict(int)
        usage: dict[str, dict] = defaultdict(
            lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "turns": 0}
        )
        tools: dict[str, int] = defaultdict(int)
        git_ops: list = []
        pr_links: list = []
        events: list[dict] = []
        pending_tools: dict[str, dict] = {}  # tool_use_id → {name, ts_ms, input, seq}
        event_seq = 0

        for etype, ev in self.parser.parse(filepath):
            # Timestamp from event or snapshot sub-object
            ts = ev.get("timestamp") or (ev.get("snapshot") or {}).get("timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            # Context fields — first occurrence wins
            if not slug:
                slug = ev.get("slug")
            if not branch:
                branch = ev.get("gitBranch")
            if not cwd:
                cwd = ev.get("cwd")
            if not version:
                version = ev.get("version")

            if etype == "system":
                if not perm_mode:
                    # permissionMode may be top-level or inside content dict
                    perm_mode = ev.get("permissionMode")
                    if not perm_mode:
                        content = ev.get("content")
                        if isinstance(content, dict):
                            perm_mode = content.get("permissionMode")

            elif etype == "user":
                msg = ev.get("message") or {}
                if msg and not ev.get("isMeta") and first_user_msg is None:
                    extracted = _first_text(msg.get("content"))
                    if extracted:
                        first_user_msg = extracted
                if isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        tool_id = block.get("tool_use_id")
                        if not tool_id:
                            continue
                        pending = pending_tools.pop(tool_id, None)
                        if pending is None:
                            continue
                        start_ms = pending["ts_ms"]
                        end_ms = _ts_ms(ts)
                        duration_ms = (end_ms - start_ms) if (start_ms and end_ms) else None
                        events.append({
                            "session_id": session_id,
                            "seq": pending["seq"],
                            "timestamp": ts,
                            "tool_name": pending["name"],
                            "phase": classify_tool(pending["name"], pending["input"]),
                            "duration_ms": duration_ms,
                            "detail": extract_detail(pending["name"], pending["input"]),
                        })

            elif etype == "assistant":
                msg = ev.get("message") or {}
                if not msg:
                    continue
                mdl = msg.get("model")
                if mdl:
                    model_hits[mdl] += 1
                u = msg.get("usage") or {}
                if mdl and u:
                    m = usage[mdl]
                    m["input"] += u.get("input_tokens", 0)
                    m["output"] += u.get("output_tokens", 0)
                    m["cache_read"] += u.get("cache_read_input_tokens", 0)
                    m["cache_create"] += u.get("cache_creation_input_tokens", 0)
                    m["turns"] += 1
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "unknown")
                    tools[name] += 1
                    tool_use_id = block.get("id")
                    if tool_use_id:
                        event_seq += 1
                        pending_tools[tool_use_id] = {
                            "name": name,
                            "ts_ms": _ts_ms(ts),
                            "input": block.get("input") or {},
                            "seq": event_seq,
                        }
                    if name == "Bash":
                        cmd = (block.get("input") or {}).get("command", "")
                        if cmd:
                            git_ops.extend(self.git_extractor.extract(cmd, session_id, ts))

            elif etype == "summary":
                if not summary_text:
                    summary_text = ev.get("summary")

            elif etype == "pr-link":
                pr_links.append({
                    "session_id": ev.get("sessionId") or session_id,
                    "pr_number": ev.get("prNumber"),
                    "pr_url": ev.get("prUrl"),
                    "pr_repository": ev.get("prRepository"),
                    "timestamp": ev.get("timestamp"),
                })

        for pending in pending_tools.values():
            events.append({
                "session_id": session_id,
                "seq": pending["seq"],
                "timestamp": None,
                "tool_name": pending["name"],
                "phase": classify_tool(pending["name"], pending["input"]),
                "duration_ms": None,
                "detail": extract_detail(pending["name"], pending["input"]),
            })

        duration_ms = (
            (_ts_ms(last_ts) - _ts_ms(first_ts)) if first_ts and last_ts else None
        )
        dominant_model = max(model_hits, key=lambda k: model_hits[k]) if model_hits else None

        session = {
            "session_id": session_id,
            "source_dir": str(filepath.parent),
            "source_file": str(filepath),
            "is_subagent": 1 if is_subagent else 0,
            "parent_session_id": parent_id,
            "slug": slug,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "total_duration_ms": duration_ms,
            "git_branch": branch,
            "cwd": cwd,
            "permission_mode": perm_mode,
            "model": dominant_model,
            "claude_version": version,
            "summary_text": summary_text,
            "first_user_message": first_user_msg,
            "usage_by_model": json.dumps(dict(usage)) if usage else None,
            "facets_json": None,
            "file_size_bytes": size,
            "file_mtime": mtime,
        }
        return {"session": session, "tools": dict(tools), "git_ops": git_ops, "pr_links": pr_links, "events": events}

    def run(self, project_dirs: list, full: bool = False, on_progress=None) -> dict:
        files = self.discover_files(project_dirs)
        total = len(files)
        processed = skipped = errors = 0

        if self.verbose:
            print(f"Discovered {total} JSONL files", file=sys.stderr)

        if on_progress:
            on_progress(0, total)

        for i, (fp, is_sub, parent_id) in enumerate(files):
            try:
                sid = f"{parent_id}:{fp.stem}" if is_sub else fp.stem
                if full:
                    self.db.delete_session(sid)

                result = self._process(fp, is_sub, parent_id)

                if result is None:
                    skipped += 1
                else:
                    self.db.delete_session(result["session"]["session_id"])
                    self.db.upsert_session(result["session"])
                    if result["tools"]:
                        self.db.insert_tools(result["session"]["session_id"], result["tools"])
                    if result["git_ops"]:
                        self.db.insert_git_ops(result["git_ops"])
                    if result["pr_links"]:
                        self.db.insert_pr_links(result["pr_links"])
                    if result["events"]:
                        self.db.insert_events(result["events"])
                    processed += 1

                if (i + 1) % 50 == 0:
                    self.db.commit()
                    if on_progress:
                        on_progress(i + 1, total)
                    if self.verbose:
                        print(f"  [{i+1}/{total}] {processed} processed, {skipped} skipped",
                              file=sys.stderr)

            except Exception as e:
                errors += 1
                if self.verbose:
                    print(f"  ERROR {fp}: {e}", file=sys.stderr)

        self.db.commit()
        if on_progress:
            on_progress(total, total)
        if self.verbose:
            print(f"Done: {processed} processed, {skipped} unchanged, {errors} errors",
                  file=sys.stderr)
        return {"processed": processed, "skipped": skipped, "errors": errors, "total": total}

    def load_facets(self) -> int:
        """Match facet JSON files to sessions by session_id."""
        facets_dir = Path.home() / ".claude" / "usage-data" / "facets"
        if not facets_dir.exists():
            return 0
        count = 0
        for f in facets_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sid = data.get("session_id") or f.stem
                self.db.update_facets(sid, json.dumps(data))
                count += 1
            except (json.JSONDecodeError, OSError):
                pass
        self.db.commit()
        return count


# ============================================================
# Entry Point
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="SDLC Session Analytics Extractor")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose progress output")
    ap.add_argument("--full", action="store_true", help="Force full re-extraction")
    ap.add_argument(
        "--db",
        default=str(
            Path.home() / ".claude" / "usage-data" / "sdlc-analytics" / "sdlc_analytics.db"
        ),
        help="Output SQLite database path",
    )
    ap.add_argument(
        "--project-dirs", nargs="+", metavar="DIR",
        help="Project directories to process (default: all dirs in ~/.claude/projects/)"
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.project_dirs:
        project_dirs = [Path(d) for d in args.project_dirs]
    else:
        base = Path.home() / ".claude" / "projects"
        project_dirs = sorted(d for d in base.iterdir() if d.is_dir()) if base.exists() else []

    if not project_dirs:
        print("ERROR: No project directories found.", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"DB: {db_path}", file=sys.stderr)
        for d in project_dirs:
            print(f"  dir: {d}", file=sys.stderr)

    db = DB(db_path)
    extractor = SessionExtractor(db, verbose=args.verbose)
    stats = extractor.run(project_dirs, full=args.full)
    facet_count = extractor.load_facets()

    db.set_meta("last_run", datetime.now(timezone.utc).isoformat())
    db.set_meta("sessions_processed", str(stats["processed"]))
    db.set_meta("sessions_skipped", str(stats["skipped"]))
    db.commit()

    print(
        f"Extracted {stats['processed']} sessions "
        f"({stats['skipped']} unchanged, {stats['errors']} errors). "
        f"Facets: {facet_count}. DB: {db_path}"
    )
    db.close()


if __name__ == "__main__":
    main()
