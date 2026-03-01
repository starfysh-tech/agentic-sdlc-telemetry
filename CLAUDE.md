# agentic-sdlc-telemetry

## What this is
CLI tool (`sdlc-t`) for extracting and visualizing SDLC telemetry from Claude Code sessions.
Primary metrics: lead time (branch → merged PR), throughput (PRs per week/month).
Audience: developer + team/leadership.

## Codebase
- `manage.py` — TUI (Textual framework), entry point via `sdlc-t`
- `sdlc_extract.py` — JSONL session extraction to SQLite
- DB: `~/.claude/usage-data/sdlc-analytics/sdlc_analytics.db`

## Development rules
- During code changes, add discovered enhancements to `todo.md` instead of implementing them
  - "Parking Lot" section: ideas and out-of-scope improvements
  - "Prioritized" section: approved items queued for implementation
- Stay focused on the current task — defer scope creep to todo.md
- No new dependencies without discussion — stdlib preferred
