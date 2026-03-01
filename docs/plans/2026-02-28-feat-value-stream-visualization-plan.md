---
title: "feat: Value Stream Visualization"
type: feat
date: 2026-02-28
---

# feat: Value Stream Visualization

## Overview

Add a 4th "Value Stream" tab to `StatsScreen` that visualizes the SDLC process as a pipeline of 6 phases (Discover → Plan → Code → Test → Review → Deliver). The feature extends `sdlc_extract.py` to capture per-tool-call events with timestamps and phase classification, stores them in a new `session_events` table, and renders an aggregate ASCII pipeline plus per-session drill-down in the TUI.

## Problem Statement

The existing StatsScreen delivers useful aggregate metrics (throughput, lead time, efficiency) but provides no view of _how_ work flows through the SDLC. The raw JSONL files contain full per-event timelines (`tool_use` → `tool_result` with timestamps), but `sdlc_extract.py` discards this data — only aggregate counts land in `session_tool_summary`. Without phase-level visibility, bottlenecks and time allocation across the development process remain invisible.

## Proposed Solution

### Phase Classification

| Phase | Classified Tools |
|-------|-----------------|
| Discover | `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `LSP`, MCP resource tools, `context7` |
| Plan | `EnterPlanMode`, `ExitPlanMode`, `AskUserQuestion`, `TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`, `Agent` (Plan/Explore subagent_type) |
| Code | `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash` (default), `Skill` (default), `Agent` (general-purpose) |
| Test | `Bash` matching: `pytest`, `jest`, `npm test`, `cargo test`, `vitest`, `mocha`, `rspec`, `go test` |
| Review | `Skill` matching `review-*`, `Agent` matching `*reviewer`, `Bash` matching: `eslint`, `rubocop`, `flake8`, `mypy`, `ruff check` |
| Deliver | `Bash` matching: `git commit`, `git push`, `gh pr create`; `Skill` matching `commit`, `commitcraft` |

**Classification priority**: Test > Review > Deliver > Plan (Bash subclassification runs before default "Code" fallback). Unknown tools default to "Code".

### ASCII Pipeline Output (render_pipeline)

```
┌────────────┐     ┌────────────┐     ┌────────────┐     ┌────────────┐     ┌────────────┐     ┌────────────┐
│  DISCOVER  │──▶  │    PLAN    │──▶  │    CODE    │──▶  │    TEST    │──▶  │   REVIEW   │──▶  │  DELIVER   │
│ Read(12)   │     │ ExitPlan(3)│     │ Edit(45)   │     │ Bash(8)    │     │ Bash(5)    │     │ Bash(4)    │
│ Grep(8)    │     │ Task(2)    │     │ Write(12)  │     │            │     │            │     │            │
│            │     │            │     │            │     │            │     │            │     │            │
│   32%      │     │    8%      │     │   45%      │     │   10%      │     │    3%      │     │    2%      │
└────────────┘     └────────────┘     └────────────┘     └────────────┘     └────────────┘     └────────────┘
```

Colors: Discover=#00bfff, Plan=#9370db, Code=#00ff7f, Test=#ffa500, Review=#ff69b4, Deliver=#00ffff

---

## Technical Approach

### Architecture

Two files change. No new files. No new dependencies (stdlib only; `frozenset`, `re` already used).

### Implementation Phases

#### Phase 1: Schema + Classification — `sdlc_extract.py`

**Schema addition** (after `pr_links` table, ~line 77):

```sql
-- sdlc_extract.py ~L77
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
```

**New functions** (after `GitOpExtractor`, ~line 220):

```python
# sdlc_extract.py ~L220
_DISCOVER = frozenset(["Read", "Grep", "Glob", "WebFetch", "WebSearch", "LSP",
                        "ListMcpResourcesTool", "ReadMcpResourceTool", "ToolSearch"])
_PLAN     = frozenset(["EnterPlanMode", "ExitPlanMode", "AskUserQuestion",
                        "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"])
_DELIVER_RE = re.compile(r"git (commit|push)|gh pr (create|merge)")
_TEST_RE    = re.compile(r"pytest|jest|npm test|cargo test|vitest|mocha|rspec|go test")
_REVIEW_RE  = re.compile(r"eslint|rubocop|flake8|mypy|ruff check")

def classify_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name in _DISCOVER:
        return "Discover"
    if tool_name in _PLAN:
        return "Plan"
    if tool_name == "Agent":
        agent_type = (tool_input or {}).get("subagent_type", "")
        if "Plan" in agent_type or "Explore" in agent_type:
            return "Plan"
        if "reviewer" in agent_type.lower():
            return "Review"
        return "Code"
    if tool_name == "Skill":
        skill = (tool_input or {}).get("skill", "")
        if "review" in skill.lower():
            return "Review"
        if skill in ("commit", "commitcraft"):
            return "Deliver"
        return "Code"
    if tool_name == "Bash":
        cmd = (tool_input or {}).get("command", "")
        if _DELIVER_RE.search(cmd):
            return "Deliver"
        if _TEST_RE.search(cmd):
            return "Test"
        if _REVIEW_RE.search(cmd):
            return "Review"
        return "Code"
    return "Code"  # default fallback

def extract_detail(tool_name: str, tool_input: dict) -> str | None:
    inp = tool_input or {}
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = inp.get("file_path") or inp.get("path")
        return Path(path).name if path else None
    if tool_name == "Bash":
        return inp.get("command", "")[:100]
    if tool_name in ("Grep", "Glob"):
        return inp.get("pattern")
    if tool_name == "Skill":
        return inp.get("skill")
    if tool_name == "Agent":
        return inp.get("subagent_type")
    return None
```

**DB class additions** (~line 145):

```python
# sdlc_extract.py DB class ~L145
def insert_events(self, events: list[dict]) -> None:
    if not events:
        return
    self._cur.executemany(
        "INSERT OR REPLACE INTO session_events "
        "(session_id, seq, timestamp, tool_name, phase, duration_ms, detail) "
        "VALUES (:session_id, :seq, :timestamp, :tool_name, :phase, :duration_ms, :detail)",
        events,
    )

def delete_session(self, session_id: str) -> None:
    # sdlc_extract.py ~L107 — add session_events to existing cascade
    for table in ("session_tool_summary", "git_operations", "pr_links",
                  "session_events", "sessions"):
        self._cur.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
```

#### Phase 2: Event Extraction in `_process()` — `sdlc_extract.py`

**New accumulators** (line 318, alongside `tools`):

```python
# sdlc_extract.py ~L318
events: list[dict] = []
pending_tools: dict[str, dict] = {}  # tool_use_id → {name, ts_ms, input, seq}
event_seq = 0
```

**In assistant handler** (lines 371–379) — record each `tool_use`:

```python
# sdlc_extract.py ~L371 (inside block["type"] == "tool_use" branch)
event_seq += 1
pending_tools[block["id"]] = {
    "name": block["name"],
    "ts_ms": _ts_ms(msg.get("timestamp")),
    "input": block.get("input", {}),
    "seq": event_seq,
}
```

**In user handler** (lines 349–354) — match `tool_result` blocks:

```python
# sdlc_extract.py ~L349
for block in msg.get("content", []):
    if block.get("type") != "tool_result":
        continue
    tool_id = block.get("tool_use_id")
    pending = pending_tools.pop(tool_id, None)
    if pending is None:
        continue
    start_ms = pending["ts_ms"]
    end_ms = _ts_ms(msg.get("timestamp"))
    duration_ms = (end_ms - start_ms) if (start_ms and end_ms) else None
    events.append({
        "session_id": session_id,
        "seq": pending["seq"],
        "timestamp": msg.get("timestamp"),
        "tool_name": pending["name"],
        "phase": classify_tool(pending["name"], pending["input"]),
        "duration_ms": duration_ms,
        "detail": extract_detail(pending["name"], pending["input"]),
    })
```

**After main loop** (~line 393) — flush orphaned pending_tools (interrupted sessions):

```python
# sdlc_extract.py ~L393
for tool_id, pending in pending_tools.items():
    events.append({
        "session_id": session_id,
        "seq": pending["seq"],
        "timestamp": None,
        "tool_name": pending["name"],
        "phase": classify_tool(pending["name"], pending["input"]),
        "duration_ms": None,
        "detail": extract_detail(pending["name"], pending["input"]),
    })
```

**Return dict** (line 421):

```python
return {"session": {...}, "tools": {...}, "git_ops": [...], "pr_links": [...], "events": events}
```

**In `run()`** (~line 444):

```python
self.db.insert_events(result["events"])
```

**Note**: Incremental runs skip unchanged files (size+mtime check). `session_events` only populates for files that are re-processed. A `--full` run is required to backfill existing sessions.

#### Phase 3: New Queries — `manage.py`

**`get_aggregate_pipeline()`** — filter `is_subagent=0` (matching existing stats convention):

```python
# manage.py ~L200 (query functions section)
def get_aggregate_pipeline() -> list[dict]:
    """Returns per-phase stats across all main sessions. Returns [] if table absent."""
    with get_db() as con:
        try:
            rows = con.execute("""
                SELECT e.phase,
                       COUNT(*)                          AS total_calls,
                       COUNT(DISTINCT e.session_id)      AS session_count,
                       SUM(e.duration_ms)                AS total_ms,
                       GROUP_CONCAT(e.tool_name)         AS tools_raw
                FROM session_events e
                JOIN sessions s ON s.session_id = e.session_id
                WHERE s.is_subagent = 0
                GROUP BY e.phase
            """).fetchall()
        except sqlite3.OperationalError:
            return []
    grand_total_ms = sum(r["total_ms"] or 0 for r in rows) or 1
    result = []
    for r in rows:
        tools = Counter(r["tools_raw"].split(",")).most_common(2)
        result.append({
            "phase": r["phase"],
            "total_calls": r["total_calls"],
            "session_count": r["session_count"],
            "time_pct": round((r["total_ms"] or 0) / grand_total_ms * 100),
            "top_tools": tools,
        })
    return result
```

**`get_session_pipeline(session_id)`**:

```python
def get_session_pipeline(session_id: str) -> list[dict]:
    with get_db() as con:
        try:
            rows = con.execute("""
                SELECT phase,
                       COUNT(*)           AS calls,
                       SUM(duration_ms)   AS total_ms,
                       GROUP_CONCAT(tool_name) AS tools_raw
                FROM session_events
                WHERE session_id = ?
                GROUP BY phase
            """, (session_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
    grand_total_ms = sum(r["total_ms"] or 0 for r in rows) or 1
    result = []
    for r in rows:
        tools = Counter(r["tools_raw"].split(",")).most_common(2)
        result.append({
            "phase": r["phase"],
            "total_calls": r["calls"],
            "total_ms": r["total_ms"],
            "time_pct": round((r["total_ms"] or 0) / grand_total_ms * 100),
            "top_tools": tools,
        })
    return result
```

**`get_recent_sessions_for_select(limit=30)`** — display label is `slug + date` or session_id fallback:

```python
def get_recent_sessions_for_select(limit: int = 30) -> list[tuple[str, str]]:
    with get_db() as con:
        try:
            rows = con.execute("""
                SELECT s.session_id,
                       COALESCE(s.slug, s.session_id) AS slug,
                       DATE(s.first_timestamp)         AS dt
                FROM sessions s
                WHERE s.is_subagent = 0
                  AND EXISTS (
                      SELECT 1 FROM session_events e WHERE e.session_id = s.session_id
                  )
                ORDER BY s.first_timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [(r["session_id"], f"{r['dt']} — {r['slug'][:40]}") for r in rows]
```

#### Phase 4: Pipeline Renderer — `manage.py`

**`render_pipeline(phases: list[dict], box_width=12) -> Text`**:

```python
# manage.py (new function, after query functions)
_PHASE_COLORS = {
    "Discover": "#00bfff", "Plan": "#9370db", "Code": "#00ff7f",
    "Test": "#ffa500", "Review": "#ff69b4", "Deliver": "#00ffff",
}
_PHASE_ORDER = ["Discover", "Plan", "Code", "Test", "Review", "Deliver"]

def render_pipeline(phases: list[dict], box_width: int = 12) -> Text:
    """
    Build the pipeline by constructing each row of all 6 boxes in parallel,
    then joining rows with newlines:
      row0 = top borders:    "┌────────────┐     ┌────────────┐ ..."
      row1 = phase names:    "│  DISCOVER  │──▶  │    PLAN    │ ..."
      row2 = top tool:       "│ Read(12)   │     │ ExitPlan(3)│ ..."
      row3 = 2nd tool:       "│ Grep(8)    │     │ Task(2)    │ ..."
      row4 = blank:          "│            │     │            │ ..."
      row5 = time pct:       "│   32%      │     │    8%      │ ..."
      row6 = bottom borders: "└────────────┘     └────────────┘ ..."
    Arrow "──▶" appears in row1 between boxes; other rows use "     " spacer.
    Each row is appended to a Rich Text object with per-phase color styling on the box content.
    """
    phase_map = {p["phase"]: p for p in phases}
    rows = [""] * 7
    sep = "     "
    w = box_width
    for i, phase_name in enumerate(_PHASE_ORDER):
        color = _PHASE_COLORS[phase_name]
        data = phase_map.get(phase_name, {"top_tools": [], "time_pct": 0, "total_calls": 0})
        tool1 = f"{data['top_tools'][0][0][:w-2]}({data['top_tools'][0][1]})" if data["top_tools"] else ""
        tool2 = f"{data['top_tools'][1][0][:w-2]}({data['top_tools'][1][1]})" if len(data["top_tools"]) > 1 else ""
        conn = "──▶  " if i < 5 else ""
        gap  = sep if i < 5 else ""
        # Phase name row gets arrow connector; border rows get blank spacer
        rows[0] += f"┌{'─'*w}┐{gap}"
        rows[1] += f"[{color}]│{phase_name.upper():^{w}}│[/{color}]{conn}"
        rows[2] += f"[{color}]│{tool1:<{w}}│[/{color}]{gap}"
        rows[3] += f"[{color}]│{tool2:<{w}}│[/{color}]{gap}"
        rows[4] += f"[{color}]│{'':<{w}}│[/{color}]{gap}"
        rows[5] += f"[{color}]│{str(data['time_pct'])+'%':^{w}}│[/{color}]{gap}"
        rows[6] += f"└{'─'*w}┘{gap}"
    return Text.from_markup("\n".join(rows))
```

> Arrow placement: row1 uses `──▶` between boxes; rows 0 and 2–6 use a blank spacer of equal width. The box content cells accept Rich color markup per phase — apply via `Text.assemble()` or `f"[{color}]{cell}[/{color}]"` markup approach.

#### Phase 5: Value Stream Tab — `manage.py`

**Import addition** (line 29 — add `Select` to existing `textual.widgets` import):

```python
# manage.py L29
from textual.widgets import (Button, DataTable, Footer, Header, Label,
                              OptionList, RichLog, Select, Static, TabbedContent,
                              TabPane, SelectionList)
```

**New TabPane** in `StatsScreen.compose()`:

```python
# manage.py — StatsScreen.compose(), 4th tab
with TabPane("Value Stream", id="value-stream"):
    yield Static(id="vs-info-banner")
    yield Label("[bold]Aggregate Pipeline[/bold]")
    yield Static(id="aggregate-pipeline")
    yield Label("[bold]Session Drill-down[/bold]")
    yield Select([], id="session-select", prompt="Select a session…")
    yield Static(id="session-pipeline")
```

**`_populate_value_stream()`** called from `on_mount`:

```python
def _populate_value_stream(self) -> None:
    phases = get_aggregate_pipeline()
    sessions = get_recent_sessions_for_select()
    banner = self.query_one("#vs-info-banner", Static)
    agg_pipeline = self.query_one("#aggregate-pipeline", Static)
    sess_select = self.query_one("#session-select", Select)

    if not phases:
        banner.update("[yellow]Run extraction with --full to populate Value Stream data.[/yellow]")
        return

    total_events = sum(p["total_calls"] for p in phases)
    total_sessions = max((p["session_count"] for p in phases), default=0)
    banner.update(f"[dim]{total_events:,} events across {total_sessions:,} sessions[/dim]")
    agg_pipeline.update(render_pipeline(phases))

    if sessions:
        sess_select.set_options((label, sid) for sid, label in sessions)
```

**Event handler**:

```python
def on_select_changed(self, event: Select.Changed) -> None:
    if event.select.id != "session-select":
        return
    session_id = event.value
    if session_id == Select.BLANK:
        return
    phases = get_session_pipeline(session_id)
    self.query_one("#session-pipeline", Static).update(
        render_pipeline(phases) if phases else "[dim]No event data for this session.[/dim]"
    )
```

**CSS additions** (in `SdlcApp.CSS`):

```css
#aggregate-pipeline { height: auto; padding: 1 0; }
#session-pipeline   { height: auto; padding: 1 0; }
#vs-info-banner     { height: auto; padding: 0 0 1 0; }
#session-select     { margin-bottom: 1; }
```

---

## Alternative Approaches Considered

- **Using `Sparkline` or `ProgressBar`**: Textual has built-in progress bars, but they lack phase color-coding and don't convey the sequential nature of a pipeline. Rich Text box-drawing is more expressive for this shape.
- **Lazy tab loading**: Could defer populating the Value Stream tab until first visit. Rejected in favor of consistency with existing `on_mount` pattern (all 3 tabs populate synchronously on mount).
- **Populating events on incremental runs**: Would require re-processing unchanged files. Rejected to keep extraction fast; `--full` is already the explicit re-extraction path.

---

## Acceptance Criteria

### Functional Requirements

- [ ] `session_events` table is created by `CREATE TABLE IF NOT EXISTS` — works on existing DBs without migration
- [ ] `classify_tool()` correctly maps all tools in the phase table above; `Bash` subclassification runs before "Code" default
- [ ] `extract_detail()` returns meaningful strings for Read/Write/Edit (filename), Bash (command[:100]), Grep/Glob (pattern), Skill/Agent (name/type)
- [ ] `pending_tools` orphans (interrupted sessions) are written with `duration_ms=NULL`, not discarded
- [ ] `delete_session()` cascades to `session_events` before other tables
- [ ] `get_aggregate_pipeline()` and `get_session_pipeline()` return `[]` (not raise) when `session_events` table is absent
- [ ] Aggregate pipeline uses `is_subagent=0` filter (matches existing stats convention)
- [ ] Value Stream tab renders aggregate pipeline with 6 colored boxes and time percentages
- [ ] Session dropdown shows up to 30 most recent sessions with `date — slug` labels; limited to sessions that have events
- [ ] Selecting a session renders per-session pipeline below
- [ ] When no event data exists: yellow banner shown, aggregate pipeline empty, dropdown empty — no exceptions
- [ ] Existing 3 tabs (Throughput, Lead Time, Efficiency) are unchanged

### Non-Functional Requirements

- [ ] `render_pipeline()` total width fits in full-width StatsScreen (~82 chars at box_width=12)
- [ ] `on_mount` remains synchronous — no `@work` added
- [ ] No new dependencies (stdlib `re`, `collections.Counter` only — add `from collections import Counter` to manage.py imports)
- [ ] Time percentages across 6 phases sum to ~100% (rounding may cause ±1%)

### Quality Gates

- [ ] Run extraction `--full`, verify events table populated via `sqlite3` query
- [ ] Verify top tools per phase are semantically correct (Read in Discover, Edit in Code, etc.)
- [ ] Open TUI _without_ running `--full` → yellow banner visible, no traceback
- [ ] `todo.md` updated with any discovered parking-lot items during implementation

---

## Success Metrics

- At least 4 of 6 phases show non-zero data after `--full` extraction of a representative session
- Time percentages reflect real workflow: Code phase should be largest in typical sessions
- Session drill-down renders correctly for at least 3 different sessions

---

## Dependencies & Prerequisites

- `sdlc_extract.py` Phase 1 & 2 must complete before `manage.py` Phase 3+ (DB table must exist)
- No external dependencies; `collections.Counter` is stdlib
- `Select` widget is in `textual.widgets` (confirmed in Textual docs) — only needs import added at line 29

---

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| `Select.BLANK` sentinel differs across Textual versions | Low | Medium | Check `event.value == Select.BLANK` exactly as spec'd; test with current installed version |
| `_ts_ms()` returns `0` (not `None`) on parse failure | Confirmed | Medium | Guard: `if (start_ms and end_ms)` before computing duration — `0` is falsy |
| JSONL tool_use order assumption: assistant before user | Low | High | `pending_tools` dict handles out-of-order gracefully via `pop(tool_id, None)` |
| Large `detail` text for Bash commands | Low | Low | `command[:100]` cap already in spec |
| DB locked during concurrent TUI + extraction | Very Low | Low | Existing pattern: WAL mode or short transactions per query |

---

## Files to Modify

| File | Changes |
|------|---------|
| `sdlc_extract.py` | `_SCHEMA` (+`session_events` table + 2 indexes), `DB.insert_events()`, `DB.delete_session()` (cascade), `_DISCOVER`/`_PLAN`/regex constants, `classify_tool()`, `extract_detail()`, `_process()` (pending_tools + events accumulator), `run()` (insert_events call) |
| `manage.py` | `Select` import (L29), 3 new query functions, `render_pipeline()`, `_PHASE_COLORS`/`_PHASE_ORDER` constants, `StatsScreen.compose()` 4th TabPane, `_populate_value_stream()`, `on_select_changed()`, `SdlcApp.CSS` (+4 rules) |
| `todo.md` | Parking lot additions as discovered during implementation |

## Existing Code to Reuse

- `_ts_ms()` (`sdlc_extract.py:253`) — timestamp to milliseconds; returns `0` on failure
- `Rich Text` (already imported `manage.py:22`) — for pipeline rendering
- `render_pipeline()` accepts same data shape for both aggregate and session views (polymorphic)
- `Counter` from `collections` — **not currently imported in manage.py**; add `from collections import Counter` near the top-of-file imports

---

## Verification

```bash
# 1. Run full extraction
sdlc-t  # then press r, or:
python3 sdlc_extract.py -v --full --project-dirs ~/Code

# 2. Verify events populated
sqlite3 ~/.claude/usage-data/sdlc-analytics/sdlc_analytics.db \
  "SELECT phase, COUNT(*) as n FROM session_events GROUP BY phase ORDER BY n DESC"

# 3. Open TUI and navigate to Value Stream tab
sdlc-t  # then s → "Value Stream" tab

# 4. Assertions:
#    - Aggregate pipeline: 6 colored boxes, non-zero data in ≥4 phases
#    - Time percentages sum to ~100%
#    - Top tools per phase are semantically correct
#    - Select dropdown shows ≤30 sessions with date — slug labels
#    - Selecting a session renders per-session pipeline

# 5. Without --full (fallback test):
#    Drop session_events table or use fresh DB, open TUI → yellow banner, no traceback

# 6. Existing tabs still work
#    Throughput, Lead Time, Efficiency tabs render correctly
```

## References

### Internal References

- `sdlc_extract.py:94` — DB class definition
- `sdlc_extract.py:107` — `delete_session()` cascade (add `session_events` here)
- `sdlc_extract.py:145` — DB insert methods (add `insert_events` here)
- `sdlc_extract.py:253` — `_ts_ms()` (returns `0` on failure, not `None`)
- `sdlc_extract.py:318` — `_process()` accumulator init (add `events`, `pending_tools`, `event_seq`)
- `sdlc_extract.py:349` — user message handler (add `tool_result` matching)
- `sdlc_extract.py:371` — assistant message handler (add `tool_use` capture)
- `sdlc_extract.py:421` — `_process()` return dict (add `"events": events`)
- `sdlc_extract.py:444` — `run()` insert calls (add `insert_events`)
- `manage.py:22` — Rich Text import (already present)
- `manage.py:29` — Textual widget imports (add `Select`)
- `manage.py:706` — `StatsScreen` class definition
- `manage.py:793` — `StatsScreen.compose()` end (add 4th TabPane before close)
- `manage.py:1006` — `SdlcApp.CSS` (add 4 new rules)
