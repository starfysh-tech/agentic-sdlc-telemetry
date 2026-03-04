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
- Discoveries outside the current task go to `todo.md`, not into the code. Add a one-line description under the appropriate section (Backlog for ideas, Prioritized for approved work). Do not implement without approval.
- No new dependencies without discussion — stdlib preferred
