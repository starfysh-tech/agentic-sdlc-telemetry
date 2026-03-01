# Plan: UX Improvements — Update Safety, Extraction Progress, Value Stream Layout

## Context

Three issues surfaced from testing:

1. **Update overwrites local dev version** — Hitting "Update" from dashboard runs `pip install --upgrade git+...` which replaces the local editable install with the remote `main` branch, reverting all local changes (screenshots confirm: tabs changed from Throughput/Lead Time/Efficiency/Value Stream → Git/Overview/Tools after update)
2. **Extraction shows raw verbose log** — SubprocessScreen displays scrolling `[50/2764] 0 processed, 50 skipped` lines on a full separate screen. User wants a simple progress bar on the main dashboard screen instead.
3. **Value Stream layout wastes space** — ASCII boxes are 75 chars wide on a 120+ char terminal. Lots of empty space below the pipeline. Doesn't match the DataTable patterns used by other tabs.

---

## Files to Modify

| File | Changes |
|------|---------|
| `manage.py` | Update guard, in-process extraction with progress bar, Value Stream layout redesign |
| `sdlc_extract.py` | Add `on_progress` callback to `SessionExtractor.run()` |

---

## Fix 1: Update Safety — `manage.py:1162`

**Problem**: `_check_and_update` always installs when `local_sha` doesn't match remote — even for dev/dirty versions where local code should be preserved.

**Fix**: Guard at the top of `_check_and_update()`:

```python
@work(thread=True)
def _check_and_update(self) -> None:
    cft = self.app.call_from_thread
    if __version__ == "dev" or ".dev" in __version__:
        cft(self.notify, f"Dev install ({__version__}) — update from source instead", severity="warning")
        return
    # ... rest of existing logic unchanged
```

- `__version__ == "dev"` catches the `PackageNotFoundError` fallback at `manage.py:38` (bare `"dev"` has no dot)
- `".dev" in __version__` catches setuptools-scm dev versions like `0.2.1.dev1+gb1bcc7918`
- Released versions (e.g., `0.2.1`) still update normally

---

## Fix 2: Extraction Progress Bar — `sdlc_extract.py` + `manage.py`

### 2a: Add callback to `SessionExtractor.run()` — `sdlc_extract.py:556`

Add `on_progress` parameter. Call at start, every 50 files, and at end:

```python
def run(self, project_dirs: list, full: bool = False, on_progress=None) -> dict:
    files = self.discover_files(project_dirs)
    total = len(files)
    processed = skipped = errors = 0

    if on_progress:
        on_progress(0, total)

    if self.verbose:
        print(f"Discovered {total} JSONL files", file=sys.stderr)

    for i, (fp, is_sub, parent_id) in enumerate(files):
        # ... existing processing unchanged ...

        if (i + 1) % 50 == 0:
            self.db.commit()
            if on_progress:
                on_progress(i + 1, total)
            if self.verbose:
                print(f"  [{i+1}/{total}] {processed} processed, {skipped} skipped",
                      file=sys.stderr)

        # ... existing exception handling unchanged ...

    self.db.commit()
    if on_progress:
        on_progress(total, total)
    # ... rest unchanged
```

Backward-compatible — existing CLI callers pass no callback.

### 2b: In-process extraction on DashboardScreen — `manage.py`

**Import** (top of file, after existing imports):
```python
from sdlc_extract import DB as ExtractDB, SessionExtractor
```

**Add progress widget to `DashboardScreen.compose()`** — place above the OptionList:
```python
yield Static(id="extraction-progress")
```

**Add re-entrancy flag** in `DashboardScreen`:
```python
class DashboardScreen(Screen):
    _extraction_running: bool = False
    # ... existing code
```

**CSS**:
```css
#extraction-progress { height: auto; padding: 0 1; display: none; }
#extraction-progress.visible { display: block; }
```

**Replace `action_run()`** with re-entrancy guard + in-process extraction:

```python
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
```

**Key fixes from validation:**
- `try/finally` ensures `db.close()` always runs (prevents connection/WAL lock leak)
- `db = None` before try, `if db is not None` in finally (handles DB init failure)
- `self._extraction_running` flag prevents concurrent extraction (replaces SubprocessScreen's natural mutex)
- `cft(lambda: self.app.push_screen(StatsScreen()))` — lambda defers StatsScreen construction to main thread (Textual widgets must not be created on worker threads)
- `total == 0` guard shows warning instead of misleading "0 sessions" message
- `on_progress(0, total)` in run() provides immediate feedback
- `update_progress` returns early if `total == 0` (avoids division issues)

**Remove**: `_push_stats_on_resume` logic and the SubprocessScreen call from `action_run`. SubprocessScreen class stays (defer cleanup to todo.md).

---

## Fix 3: Value Stream Layout — `manage.py`

**Problem**: ASCII boxes at `box_width=8` produce 75-char output, leaving 40+ chars unused on standard terminals. Empty space below.

**New layout**: Match existing tab patterns — stacked bar at top + DataTable for details.

### 3a: Replace `render_pipeline()` with `render_phase_bar()`

Two-pass allocation to prevent bar overflow:

```python
def render_phase_bar(phases: list[dict], bar_width: int = 80) -> Text:
    by_phase = {p["phase"]: p for p in phases}
    text = Text()

    # Two-pass allocation: phases with data get proportional width,
    # phases without data are skipped (no min-1 overflow)
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
```

**Fixes from validation:**
- Only active phases (with data) get bar segments — no `max(1, ...)` for empty phases
- Last phase gets `remaining` width — guarantees total equals exactly `bar_width`
- Empty bar case handled gracefully

### 3b: Compose — replace old widgets with DataTable layout

```python
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
```

### 3c: `_populate_value_stream()` — add columns once for both tables

```python
def _populate_value_stream(self) -> None:
    # Add columns to both tables (only runs once in on_mount)
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
        pt.add_row(p["phase"], f"{p['total_calls']:,}", f"{p['time_pct']}%",
                   f"{p['session_count']:,}", tools_str)

    sessions = get_recent_sessions_for_select()
    if sessions:
        sel = self.query_one("#session-select", Select)
        sel.set_options((label, sid) for label, sid in sessions)
```

### 3d: `on_select_changed()` — full handler with DataTable clearing

```python
def on_select_changed(self, event: Select.Changed) -> None:
    if event.value is Select.BLANK:
        return
    phases = get_session_pipeline(str(event.value))
    bar = self.query_one("#session-phase-bar", Static)
    table = self.query_one("#session-phase-table", DataTable)
    table.clear()  # clear rows only, keep columns

    if not phases:
        bar.update("[dim]No events for this session.[/dim]")
        return

    bar.update(render_phase_bar(phases))
    for p in sorted(phases, key=lambda x: _PHASE_ORDER.index(x["phase"]) if x["phase"] in _PHASE_ORDER else 99):
        tools_str = ", ".join(t[0] for t in p["top_tools"][:3])
        table.add_row(p["phase"], f"{p['total_calls']:,}", f"{p['time_pct']}%", tools_str)
```

**Fixes from validation:**
- Columns added once in `_populate_value_stream()` (runs in `on_mount`)
- Handler calls `table.clear()` (rows only) then re-adds rows — no duplicate columns
- Both bar and table updated together in handler

### 3e: CSS

```css
#phase-bar          { height: auto; padding: 1 0; }
#phase-table        { height: 10; }
#session-phase-bar  { height: auto; padding: 1 0; }
#session-phase-table { height: 10; }
#vs-info-banner     { height: auto; padding: 0 0 1 0; }
#session-select     { margin-bottom: 1; }
```

Remove old `#aggregate-pipeline` and `#session-pipeline` CSS rules.

---

## Cleanup

- Remove `render_pipeline()` function (replaced by `render_phase_bar()`)
- Remove `#aggregate-pipeline` and `#session-pipeline` CSS rules and widget yields
- Keep `_PHASE_COLORS` / `_PHASE_ORDER` (used by new bar renderer)
- Add to `todo.md`: remove SubprocessScreen class if no longer used

---

## Verification

```bash
# 1. Update guard
sdlc-t → u → should see "Dev install (...) — update from source instead"

# 2. Extraction progress
sdlc-t → r → progress bar on dashboard, no separate screen
# Also: press r twice quickly → "Extraction already running" notification
# Also: empty project dirs → "No JSONL files found" warning

# 3. Value Stream tab
sdlc-t → s → "Value Stream" tab → colored bar + DataTable
# Select a session → session bar + table populate, select another → clears and repopulates

# 4. Existing tabs still work: Throughput, Lead Time, Efficiency
```

---

# Validation Results

**Validated:** 2026-02-28
**Verdict:** CAUTION — 2 critical, 4 high-risk issues found; all incorporated into plan above

## Issues Found & Resolved

### Critical (Fixed in Plan)

- **DB connection leak**: No `try/finally` for `db.close()` in worker. Exception after `ExtractDB()` but before `close()` leaks connection + WAL lock → "database is locked" on next run.
  - _Fix_: `db = None` before try, `if db is not None: db.close()` in finally block

- **StatsScreen constructed on worker thread**: `cft(self.app.push_screen, StatsScreen())` evaluates `StatsScreen()` on the worker thread. Textual widgets must be created on the main thread.
  - _Fix_: `cft(lambda: self.app.push_screen(StatsScreen()))` defers construction

### High Risk (Fixed in Plan)

- **No re-entrancy guard**: SubprocessScreen was a natural mutex (can't trigger run twice). In-process approach allows double-press `r` → concurrent workers on same DB.
  - _Fix_: `_extraction_running` flag checked in `action_run()`

- **No progress for <50 files**: Callback only fires at `(i+1) % 50`. Projects with <50 files show nothing until completion.
  - _Fix_: `on_progress(0, total)` before loop starts in `run()`

- **`render_phase_bar` overflow**: `max(1, ...)` for 6 phases with only 3 having data → total exceeds `bar_width` (e.g., 48+24+8+1+1+1 = 83 > 80).
  - _Fix_: Two-pass allocation — only active phases get segments, last gets remainder

- **DataTable double-column bug**: `on_select_changed` calling `add_columns()` on every selection → duplicate columns.
  - _Fix_: Add columns once in `_populate_value_stream()`, use `clear()` (rows only) per selection

### Medium (Fixed in Plan)

- **Version guard misses bare `"dev"`**: `__version__ = "dev"` (PackageNotFoundError fallback at `manage.py:38`) has no dot → `".dev" in "dev"` is `False`.
  - _Fix_: `if __version__ == "dev" or ".dev" in __version__:`

- **`total == 0` shows `0/0` progress**: `resolve_dirs` can return dirs with zero JSONL files.
  - _Fix_: Guard in `update_progress` and post-run warning notification

## Plan Revisions Made

- Changed Fix 1 guard from `".dev" in __version__` to `__version__ == "dev" or ".dev" in __version__` (catches bare "dev" fallback)
- Added `try/finally` block with `db = None` guard to Fix 2 extraction worker
- Added `_extraction_running` re-entrancy flag to `DashboardScreen`
- Changed `StatsScreen()` push to `cft(lambda: self.app.push_screen(StatsScreen()))` (main thread construction)
- Added `on_progress(0, total)` initial callback to `run()`
- Rewrote `render_phase_bar` with two-pass allocation (no overflow)
- Split DataTable column setup into `_populate_value_stream()` (once) vs `on_select_changed()` (clear + add rows)
- Added full handler code for `on_select_changed()` (was "same pattern" — insufficient)
- Added `total == 0` handling in extraction worker

## Decisions Confirmed

- [x] **In-process vs subprocess extraction**: In-process — enables progress callback without parsing text output
- [x] **DataTable vs ASCII boxes for Value Stream**: DataTable — matches existing tab patterns, better space utilization
- [x] **Keep SubprocessScreen class**: Yes — defer removal to todo.md to avoid risk
