# sdlc-analyze: Thresholds & Heuristics

## CLI Thresholds

From `manage.py:205-212` `_THRESH` dict:

| JSON Key | Green | Yellow | Red | Direction | Unit |
|----------|-------|--------|-----|-----------|------|
| `prs_week` | >= 5 | >= 2 | < 2 | higher-is-better | integer count |
| `commits_day` | >= 3 | >= 1 | < 1 | higher-is-better | float |
| `avg_lead_time_h` | <= 4 | <= 24 | > 24 | lower-is-better | float hours |
| `push_rate` | >= 0.60 | >= 0.40 | < 0.40 | higher-is-better | **0-1 ratio** — multiply by 100 for display |
| `productivity_rate` | >= 40 | >= 20 | < 20 | higher-is-better | 0-100 percentage |
| unproductive_h | <= 0.17 (~10 min) | <= 1 | > 1 | lower-is-better | derived: `sum(unproductive_sessions[].duration_s) / 3600` |

## Skill-Defined Soft Thresholds

Not in CLI — applied by this skill only:

| JSON Key | Green | Yellow | Red | Rationale | Unit |
|----------|-------|--------|-----|-----------|------|
| `post_ratio` | <= 0.10 | <= 0.30 | > 0.30 | High = scope changes after PR open | **0-1 fraction** — multiply by 100 for display |
| `len(rework_hotspots)` | 0 | 1-2 | > 2 | Many branches with repeated sessions | count of branches with > 2 sessions |

**Rework hotspot context:** The count alone ignores per-branch severity. Always show each hotspot branch's `sessions` count and `commits_per_session` ratio for context.

## Confidence Semantics

From `docs/metric-contract.md`:

| Level | Coverage |
|-------|----------|
| HIGH | >= 90% of PRs enriched |
| MED | >= 70% |
| LOW | < 70% |

**LOW confidence** → caveat all PR-derived metrics (velocity, lead time, post_ratio). Show: ⚠️ Low coverage — treat PR metrics as estimates.

## Conditional Recommendations

Apply only triggered conditions (skip GREEN items):

| Metric | Status | Recommendation |
|--------|--------|----------------|
| `prs_week` | RED | Check for accumulating branches — are you finishing features? Check for work outside tracked Claude sessions. |
| `avg_lead_time_h` | YELLOW | Review `pr_lifecycle` for branches with long idle periods. Check if PRs are waiting on external review. |
| `avg_lead_time_h` | RED | Lead time is high — drill down on `rework hotspots` and `post-open outliers` to identify bottlenecks. |
| `productivity_rate` | RED | Review `unproductive sessions` for patterns (same prompts? repeated exploration?). Consider using skills or context shortcuts. |
| `post_ratio` | RED | PRs are getting substantial work after opening. Either PRs are opened too early, or scope is expanding. Check `post-open outliers` for worst cases. |
| `post_ratio` | YELLOW | Some post-open commits — check outliers for PRs driving the average up. |
| `len(rework_hotspots)` | YELLOW/RED | Repeated sessions on same branches may signal unclear requirements, large scope, or lack of upfront planning. Check specific hotspot branches. |
| `push_rate` | RED | Less than 40% of sessions end in a push. Many sessions may be exploratory with no committed output. |
| `commits_day` | RED | Low commit frequency — check if work is being committed in large batches (high `commits_per_session` on some branches). |

## Bottleneck Analysis Heuristics

Apply when interpreting `value_stream.aggregate_pipeline`. Include only triggered conditions in the report.

| Condition | Interpretation |
|-----------|----------------|
| Discover phase > 40% of total time | Excessive exploration. Consider better upfront planning, or invoke skills to shortcut known patterns. |
| Test phase < 5% AND `productivity_rate` < 40% | Possible lack of test feedback loop — low test activity correlates with low productivity. |
| Code phase > 70% AND `default_code_rate` > 60% | Many tool calls defaulting to Code phase. Phase classification may be noisy; Code-phase dominance may not reflect actual coding time. |
| Review phase near 0% | Code reviews may be happening outside Claude sessions — PR review data may be incomplete. |

## Tool Value Assessment Heuristics

**Aggregate-only** — per-session tool correlation is NOT available from the stats JSON. `tool_usage` is a GROUP BY aggregation; `unproductive_sessions` has no tool breakdown.

| Condition | Interpretation |
|-----------|----------------|
| Read + Grep + Glob + LS share > 60% of total tool calls AND `productivity_rate` is low | Heavy exploration relative to output. Consider skills or CLAUDE.md context to reduce discovery overhead. |
| High `subagent_ratio` (from `efficiency.banner`) AND low `productivity_rate` | Possible subagent overhead — subagents may be spinning up for tasks that don't need them. |
| Top tools in `tool_usage` don't align with dominant phase in `aggregate_pipeline` | E.g., Edit/Write are low but Code phase dominates → phase classification may be driven by default_code_rate, not actual edits. |
| Any tool with `avg_per_session` > 20 | Potential automation candidate or workflow smell — high repetition per session may indicate a manual loop that could be scripted or skill-ified. |
