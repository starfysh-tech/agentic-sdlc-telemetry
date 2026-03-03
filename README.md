# agentic-sdlc-telemetry

SDLC session analytics for Claude Code. Processes session JSONL files into a SQLite database for analysing rework loops, post-PR churn, and wasted sessions.

## Install

```bash
pipx install git+https://github.com/starfysh-tech/agentic-sdlc-telemetry
```

Or with pip:

```bash
pip install git+https://github.com/starfysh-tech/agentic-sdlc-telemetry
```

## Usage

```bash
sdlc-t
```

Launches an interactive TUI. On first run, use **Configure projects** to select which projects to include.

The tool is self-contained under `sdlc-t`:

- TUI mode: `sdlc-t`
- AI/automation mode: `sdlc-t --ai --command ...`

Stats support two scopes:

- **All Activity** (default) — includes main + subagent sessions
- **Delivery Only** — includes only main sessions

Stats now include a **PR Commit Timing** view with pre-vs-post PR-open commit counts, post-open ratio, and confidence bands.

### AI Command Mode (for automation/AI)

Use `--ai` to run non-interactive JSON commands (optimized for agent consumption):

```bash
sdlc-t --ai --command stats
```

Every response is a stable JSON envelope:

```json
{
  "ok": true,
  "command": "stats",
  "generated_at": "2026-03-03T00:00:00+00:00",
  "data": {},
  "errors": [],
  "warnings": []
}
```

Output is optimized for machine consumption:

- no interactive prompts in AI mode
- stable top-level keys (`ok`, `command`, `generated_at`, `data`, `errors`, `warnings`)
- deterministic JSON with optional `--compact`

Common commands:

```bash
# TUI parity stats payload + repo snapshot
sdlc-t --ai --command stats --scope all_activity --limit 20

# DB/config status
sdlc-t --ai --command status

# List discovered projects under ~/.claude/projects
sdlc-t --ai --command projects.list

# Get configured include dirs
sdlc-t --ai --command config.get

# Set configured projects by project name/display
sdlc-t --ai --command config.set --project my-project --project org/repo

# Run extraction (incremental by default)
sdlc-t --ai --command extract.run

# Run full extraction + GitHub enrichment
sdlc-t --ai --command extract.run --full --enrich --github-token-env GITHUB_TOKEN --github-max-prs 500

# Check/update package
sdlc-t --ai --command update.check
sdlc-t --ai --command update.apply

# Uninstall config (+ optional DB)
sdlc-t --ai --command uninstall --yes
sdlc-t --ai --command uninstall --yes --remove-db
```

TUI action parity via `sdlc-t --ai --command`:

| TUI Action | AI Command |
|---|---|
| Run extraction | `extract.run` |
| Configure projects | `config.get`, `config.set`, `projects.list` |
| View stats | `stats` |
| Update | `update.check`, `update.apply` |
| Uninstall | `uninstall` |
| Dashboard status | `status` |

Repo filtering for stats/rework analytics:

```bash
sdlc-t --ai --command stats --scope delivery_only --repo mqol-inc/aerie --repo starfysh-tech/cc-hooks-metrics --limit 20
```

Compact JSON output:

```bash
sdlc-t --ai --command stats --compact
```

`--cli` remains as a legacy alias for AI mode.

## Menu

| Option | Description |
|---|---|
| Run extraction | Process selected project sessions into the SQLite database |
| Configure projects | Choose which `~/.claude/projects/` directories to include |
| Update | Pull and install the latest version from GitHub |
| Uninstall | Remove config and optionally the database |

## How it works

Claude Code writes session data as JSONL files under `~/.claude/projects/`. This tool reads those files and extracts:

- **Sessions** — timestamps, git branch, working directory, model, token usage, session summary
- **Subagent sessions** — linked to their parent session
- **Git operations** — commits, branches, pushes, PRs detected from tool calls
- **PR links** — pull request numbers and URLs from `pr-link` events

Data is stored in `~/.claude/usage-data/sdlc-analytics/sdlc_analytics.db`.

Extraction is incremental — files are skipped if size and mtime haven't changed since the last run.

## GitHub PR Enrichment

Use enrichment to fill PR lifecycle truth data and PR commit timing truth:

- PR metadata (`opened_at`, `merged_at`, branches, status, `merge_commit_sha`)
- Final PR commit set (`/pulls/{n}/commits`)
- PR timeline commit-added events (`/issues/{n}/timeline`)

```bash
sdlc-t --ai --command extract.run --enrich --github-token-env GITHUB_TOKEN
```

Common backfill flow after schema upgrades:

```bash
sdlc-t --ai --command extract.run --full --enrich --github-token-env GITHUB_TOKEN
```

Direct script usage still works, but `sdlc-t` is the preferred self-contained interface.

## Database schema

```
sessions             — one row per JSONL file (main + subagent)
session_tool_summary — tool call counts per session
git_operations       — git commits, branches, pushes extracted from sessions
pr_links             — pull request URLs linked to sessions
pr_facts             — GitHub PR truth data (opened/closed/merged timestamps, branches, merge SHA, state)
pr_commits_final     — final commit set currently attached to each PR
pr_commit_events     — GitHub timeline commit-added events for PRs
session_events       — per-tool timeline events with SDLC phase classification
extraction_meta      — key/value store (last_run timestamp, etc.)
```

## Confidence

PR-derived metrics include confidence based on PR linkage coverage:

- `HIGH` >= 90%
- `MED` >= 70%
- `LOW` < 70%

PR Commit Timing metrics use an additional per-PR confidence:

- `HIGH`: timeline commit events + `opened_at`
- `MED`: fallback to final commit timestamps + `opened_at`
- `LOW`: incomplete metadata (excluded from ratio aggregates)

## Requirements

- Python 3.9+
- Claude Code with session data in `~/.claude/projects/`

## License

MIT
