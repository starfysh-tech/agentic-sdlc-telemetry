# Metric Contract

## Scope Modes

- `all_activity`: includes both main and subagent sessions.
- `delivery_only`: includes only `sessions.is_subagent = 0`.

Default UI scope is `all_activity`.

## Confidence Levels

Confidence is based on PR linkage coverage for metrics that depend on PR truth data.

- `HIGH`: coverage >= 90%
- `MED`: coverage >= 70%
- `LOW`: coverage < 70%

Coverage formula:

- numerator: distinct `pr_links.pr_url` that have matching `pr_facts.pr_url`
- denominator: distinct non-empty `pr_links.pr_url` in selected scope

## Canonical PR Linkage

Primary source:

- `pr_links.pr_url -> pr_facts.pr_url`

Fallback source:

- branch-based heuristics from local git operations when PR facts are missing.
- fallback-derived values are always considered `LOW` confidence.

## Metric Definitions

- `prs_week`: distinct PRs with `pr_facts.merged_at` in last 7 days.
- `commits_day`: commits in last 7 days divided by 7.
- `avg_lead_time_h`: average of `(pr_facts.opened_at - first_session_timestamp_for_branch)` in hours.
- `push_rate`: `push_ops / commit_ops` in selected scope.
- `productivity_rate`: `% sessions with at least one commit in selected scope`.

### PR Commit Timing Metrics

Canonical source priority:

1. `pr_commit_events` (`event_type='committed'`) with earliest event timestamp per commit.
2. Fallback to `pr_commits_final` using `COALESCE(authored_at, committed_at)`.

Per-PR formulas:

- `pre_pr_commits`: distinct commits where earliest commit-added timestamp `< pr_facts.opened_at`
- `post_pr_commits`: distinct commits where earliest commit-added timestamp `>= pr_facts.opened_at`
- `post_pr_ratio`: `post_pr_commits / (pre_pr_commits + post_pr_commits)`

Confidence hierarchy (per PR):

- `HIGH`: timeline commit events available and `opened_at` present.
- `MED`: no timeline commit events, but final PR commit timestamps available with `opened_at`.
- `LOW`: insufficient metadata (for example missing `opened_at` and no commit timing source).

Aggregate behavior:

- Summary ratio/averages include only `HIGH` and `MED`.
- `LOW` is retained for detail views but excluded from ratio aggregates.

## Time Semantics

All timestamps are treated as UTC-compatible ISO values and compared with SQLite datetime functions.

## Data Freshness and Versioning

Extraction writes the following metadata keys:

- `schema_version`
- `default_code_rate`
- `github_enrich_last_run`
- `github_enrich_errors`
- `github_enrich_commit_last_run`
- `github_enrich_commit_errors`

Backfill runbook after schema changes:

1. `python3 sdlc_extract.py --full`
2. `python3 sdlc_extract.py --enrich-only`
