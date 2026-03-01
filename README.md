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

## Database schema

```
sessions             — one row per JSONL file (main + subagent)
session_tool_summary — tool call counts per session
git_operations       — git commits, branches, pushes extracted from sessions
pr_links             — pull request URLs linked to sessions
extraction_meta      — key/value store (last_run timestamp, etc.)
```

## Requirements

- Python 3.9+
- Claude Code with session data in `~/.claude/projects/`

## License

MIT
