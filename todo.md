# sdlc-t TODO

## Prioritized


## Parking Lot
- Remove `SubprocessScreen` class from `manage.py` — no longer called (extraction is now in-process); keep for now until verified no other callers
- GitHub API integration for PR merge status — only 22/127 pr_merges captured; most PRs merged via GitHub UI, not Claude Code sessions
- CSV/JSON export for team/leadership reporting
- Cost estimation — apply model pricing to token counts from `usage_by_model`
- Ticket/issue ID parsing from branch names for richer lead time tracking
- Multi-user support — ingest other developers' sessions
- Fix `permission_mode` extraction — 100% NULL; extractor checks for `permissionMode` in system events but never matches
- Improve `summary_text` coverage — 96% NULL; only long-running sessions emit summary events
- Investigate `facets_json` population — only 2/2,759 sessions have data; facets directory may need separate process
- PR cycle time (pr_create → pr_merge) — requires GitHub API for complete merge data
- Session failure/error detection — no explicit success/fail state on sessions currently
