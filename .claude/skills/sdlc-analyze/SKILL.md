---
name: sdlc-analyze
description: Use when the developer wants to review delivery velocity, workflow efficiency, or code quality signals from Claude Code sessions. Triggers on questions like "how is my process?", "where are bottlenecks?", "am I making progress?", or any request to analyze SDLC telemetry.
---

# sdlc-analyze

## Purpose

Analyze SDLC telemetry from Claude Code sessions and produce a structured developer health check. Designed for individual developers and team leads who want to understand delivery velocity, workflow bottlenecks, and rework patterns without navigating the TUI or parsing JSON manually. Output is a RAG-rated markdown report with actionable recommendations.

## Workflow

### Step 1: Check freshness

```bash
sdlc-t --ai --command status --compact
```

Parse `data.db_stats.last_run`:
- If `null` → tell the user to run `sdlc-t` setup first; stop here.
- If last_run is > 24 hours ago → ask user: "Data is stale (last updated: {date}). Re-extract now via `sdlc-t --ai --command extract.run --enrich`?"
- If recent → proceed to Step 2.

### Step 2: Fetch metrics

```bash
sdlc-t --ai --command stats --compact
```

Check the response for `data.repo_snapshot.error` key — this signals a missing or corrupt DB.

**Do NOT trust `ok: true` alone.** The CLI returns `ok: true` even when the DB is missing or empty. Metric functions return `{}` on failure, not zero-valued dicts. Always inspect the actual data.

### Step 3: Assess data quality

Check `data.tui_parity.velocity_banner`:
- If `{}` (empty dict) → DB is missing or has no PR data. Report "No data available" and stop.
- If non-empty → check `velocity_banner.confidence`.
- If `confidence == "LOW"` → caveat all PR-derived metrics in the report.

Use `.get()` with defaults on all dict access — metric functions return `{}` on failure, not zero-valued dicts.

### Step 4: Generate health check

Load `references/thresholds.md` for threshold definitions and heuristics, then produce the report using the template below.

### Step 5: Offer drill-downs

After the report, present the drill-down menu from the Drill-Down Paths section.

---

## Report Template

```
## SDLC Health Check — {date}

**Data coverage:** {coverage_num}/{coverage_den} PRs | Confidence: {confidence}
{if LOW confidence: ⚠️ Low coverage — PR-derived metrics may be incomplete}

---

### Delivery Velocity

| Metric | Value | Status |
|--------|-------|--------|
| PRs / week | {prs_week} | 🟢/🟡/🔴 |
| Commits / day | {commits_day:.1f} | 🟢/🟡/🔴 |
| Avg lead time | {avg_lead_time_h:.1f}h | 🟢/🟡/🔴 |
| Push rate | {push_rate * 100:.0f}% | 🟢/🟡/🔴 |

*Push rate is the fraction of sessions that include a git push (0–1 scale internally).*

---

### Bottleneck Analysis

**Value stream phase distribution:**

| Phase | Time % | Top Tools |
|-------|--------|-----------|
{for each phase in aggregate_pipeline: | {name} | {time_pct}% | {top_tools} |}

**Session efficiency:**
- Productivity rate: {productivity_rate}% {🟢/🟡/🔴}
- Avg session duration: {avg_duration_s / 60:.1f} min *(stored as seconds)*
- Unproductive sessions: {len(unproductive_sessions)} sessions
- Default code rate: {default_code_rate}% *(high rate = phase classification noise)*

{bottleneck heuristics from thresholds.md — include only triggered conditions}

---

### Rework & Quality

| Metric | Value | Status |
|--------|-------|--------|
| Post-open commit ratio | {post_ratio * 100:.0f}% | 🟢/🟡/🔴 |
| Rework hotspot branches | {len(rework_hotspots)} | 🟢/🟡/🔴 |
| Avg pre-open commits | {avg_pre:.1f} |  |
| Avg post-open commits | {avg_post:.1f} |  |

*Post-open ratio is a 0–1 fraction internally; displayed as %.*

{if rework_hotspots non-empty:}
**Hotspot branches:**
{for each branch: - `{branch}`: {sessions} sessions, {commits_per_session:.1f} commits/session}

---

### Tool Value Assessment

*(Aggregate patterns — per-session tool correlation is not available from stats JSON)*

**Top tools by usage:**

| Tool | Total Calls | Sessions | Avg/Session |
|------|-------------|----------|-------------|
{top 10 rows from tool_usage}

{tool value heuristics from thresholds.md — include only triggered conditions}

---

### Observations

1. {data-backed finding}
2. {data-backed finding}
3. {data-backed finding}
{up to 5 — only include if supported by metric values}

### Recommendations

{conditional recommendations from thresholds.md — only include triggered conditions}

---

*Source: `sdlc-t --ai --command stats` | {timestamp}*

---

**Drill-downs available:** weekly trend · post-open outliers · rework hotspots · unproductive sessions · value stream · model efficiency · tool usage · session drill-down · repo breakdown

Ask for any of these to get detailed data.
```

---

## Drill-Down Paths

| User request | Data path in stats response |
|---|---|
| "weekly trend" | `tui_parity.throughput.weekly_throughput` |
| "post-open outliers" | `tui_parity.pr_commit_timing.outliers` |
| "rework hotspots" | `tui_parity.lead_time.rework_hotspots` |
| "unproductive sessions" | `tui_parity.efficiency.unproductive_sessions` |
| "value stream" | `tui_parity.value_stream.aggregate_pipeline` |
| "model efficiency" | `tui_parity.efficiency.model_efficiency` |
| "tool usage" | `tui_parity.efficiency.tool_usage` (top 30 tools) |
| "session drill-down" | `tui_parity.value_stream.sample_session_breakdowns` |
| "repo breakdown" | re-run `sdlc-t --ai --command stats --repo {repo_name}` |

For each drill-down, extract the relevant sub-object from the already-fetched stats response before making additional CLI calls.

---

## Error Handling

- `sdlc-t` not installed → "Install sdlc-t first: see project README."
- `ok: false` in response → display `data.error` message and stop.
- `velocity_banner` is `{}` → "No PR data found. Run `sdlc-t` with `--enrich` to populate."
- Any metric dict is `{}` → show "N/A" for that metric rather than crashing.
