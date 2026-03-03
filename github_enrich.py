#!/usr/bin/env python3
"""GitHub PR enrichment for sdlc_extract.

This module syncs PR metadata into the local `pr_facts` table using
PR URLs captured in `pr_links`.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

_RE_GITHUB_PR_URL = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_TIMELINE_ACCEPT = "application/vnd.github+json, application/vnd.github.mockingbird-preview+json"


def extract_repo_pr_from_url(pr_url: str | None) -> tuple[str, str, int] | None:
    """Parse owner/repo/pull-number from a GitHub PR URL."""
    if not pr_url:
        return None
    match = _RE_GITHUB_PR_URL.match(pr_url.strip())
    if not match:
        return None
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def _request_json_any(
    url: str,
    token: str,
    retries: int = 3,
    verbose: bool = False,
    accept: str = "application/vnd.github+json",
) -> Any:
    headers = {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "User-Agent": "sdlc-t-enricher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_exc: Exception | None = None

    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in _TRANSIENT_HTTP_CODES and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Failed to fetch JSON from GitHub API")


def _request_paginated(
    url: str,
    token: str,
    retries: int = 3,
    verbose: bool = False,
    accept: str = "application/vnd.github+json",
    per_page: int = 100,
) -> list[dict]:
    page = 1
    rows: list[dict] = []
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}per_page={per_page}&page={page}"
        payload = _request_json_any(
            page_url,
            token=token,
            retries=retries,
            verbose=verbose,
            accept=accept,
        )
        if not isinstance(payload, list):
            raise ValueError(f"Expected list payload for paginated endpoint: {page_url}")
        page_rows = [item for item in payload if isinstance(item, dict)]
        rows.extend(page_rows)
        if len(payload) < per_page:
            break
        page += 1
    return rows


def fetch_pr(owner: str, repo: str, number: int, token: str, verbose: bool = False) -> dict:
    """Fetch one PR from GitHub REST API and normalize fields for `pr_facts`."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    payload = _request_json_any(url, token=token, verbose=verbose)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected PR payload type for {owner}/{repo}#{number}")
    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        "pr_url": payload.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}",
        "repo_full_name": payload.get("base", {}).get("repo", {}).get("full_name") or f"{owner}/{repo}",
        "pr_number": int(payload.get("number") or number),
        "state": payload.get("state"),
        "is_merged": 1 if payload.get("merged") else 0,
        "opened_at": payload.get("created_at"),
        "closed_at": payload.get("closed_at"),
        "merged_at": payload.get("merged_at"),
        "merge_commit_sha": (payload.get("merge_commit_sha") or None),
        "author_login": (payload.get("user") or {}).get("login"),
        "base_branch": (payload.get("base") or {}).get("ref"),
        "head_branch": (payload.get("head") or {}).get("ref"),
        "last_synced_at": now_iso,
    }


def fetch_pr_commits(owner: str, repo: str, number: int, token: str, verbose: bool = False) -> list[dict]:
    """Fetch the final commit set attached to a PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/commits"
    payload = _request_paginated(url, token=token, verbose=verbose)
    commits: list[dict] = []
    for item in payload:
        sha = item.get("sha")
        if not sha:
            continue
        commit_meta = item.get("commit") or {}
        author_meta = commit_meta.get("author") or {}
        committer_meta = commit_meta.get("committer") or {}
        msg = str(commit_meta.get("message") or "")
        commits.append(
            {
                "commit_sha": sha,
                "authored_at": author_meta.get("date"),
                "committed_at": committer_meta.get("date"),
                "author_login": (item.get("author") or {}).get("login"),
                "committer_login": (item.get("committer") or {}).get("login"),
                "message_subject": msg.splitlines()[0][:500] if msg else None,
            }
        )
    return commits


def fetch_pr_timeline_committed_events(
    owner: str, repo: str, number: int, token: str, verbose: bool = False
) -> list[dict]:
    """Fetch commit-added timeline events for a PR."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/timeline"
    payload = _request_paginated(
        url,
        token=token,
        verbose=verbose,
        accept=_TIMELINE_ACCEPT,
    )
    events: list[dict] = []
    for item in payload:
        if item.get("event") != "committed":
            continue
        sha = item.get("commit_id") or item.get("sha")
        if not sha:
            continue
        author_meta = item.get("author") or {}
        committer_meta = item.get("committer") or {}
        event_at = item.get("created_at") or author_meta.get("date") or committer_meta.get("date")
        raw_id = item.get("id") or f"{sha}:{event_at or ''}"
        events.append(
            {
                "event_id": f"{owner}/{repo}#{number}:{raw_id}",
                "commit_sha": sha,
                "event_type": "committed",
                "event_at": event_at,
                "actor_login": (item.get("actor") or {}).get("login"),
            }
        )
    return events


def sync_pr_facts(db, token: str | None, max_prs: int = 500, scope: str = "all_activity", verbose: bool = False) -> dict:
    """Sync PR facts from GitHub for PR links in the local DB.

    Args:
      db: sdlc_extract.DB-like object.
      token: GitHub token. If missing, returns skipped stats.
      max_prs: Max PR links to process.
      scope: all_activity | delivery_only.
    """
    if not token:
        return {
            "attempted": 0,
            "updated": 0,
            "errors": 0,
            "commit_errors": 0,
            "commits_final": 0,
            "commit_events": 0,
            "skipped": True,
        }

    seen_urls: set[str] = set()
    attempted = updated = errors = 0
    commit_errors = commits_final = commit_events = 0

    for pr_url, pr_repo, pr_number, _branch in db.iter_pr_links(limit=max_prs, scope=scope):
        if not pr_url or pr_url in seen_urls:
            continue
        seen_urls.add(pr_url)
        attempted += 1

        parsed = extract_repo_pr_from_url(pr_url)
        if parsed is None:
            # Fallback if URL has odd shape but repo/number metadata is present.
            if pr_repo and pr_number:
                parts = str(pr_repo).split("/", 1)
                if len(parts) == 2:
                    parsed = (parts[0], parts[1], int(pr_number))
            if parsed is None:
                errors += 1
                commit_errors += 1
                continue

        owner, repo, number = parsed
        try:
            fact = fetch_pr(owner, repo, number, token=token, verbose=verbose)
            db.upsert_pr_fact(fact)
            synced_at = datetime.now(timezone.utc).isoformat()

            final_rows = []
            for row in fetch_pr_commits(owner, repo, number, token=token, verbose=verbose):
                final_rows.append(
                    {
                        "pr_url": fact["pr_url"],
                        "repo_full_name": fact["repo_full_name"],
                        "pr_number": fact["pr_number"],
                        "commit_sha": row.get("commit_sha"),
                        "authored_at": row.get("authored_at"),
                        "committed_at": row.get("committed_at"),
                        "author_login": row.get("author_login"),
                        "committer_login": row.get("committer_login"),
                        "message_subject": row.get("message_subject"),
                        "last_synced_at": synced_at,
                    }
                )
            db.replace_pr_commits_final(fact["pr_url"], final_rows)

            timeline_rows = []
            for row in fetch_pr_timeline_committed_events(owner, repo, number, token=token, verbose=verbose):
                timeline_rows.append(
                    {
                        "event_id": row.get("event_id"),
                        "pr_url": fact["pr_url"],
                        "repo_full_name": fact["repo_full_name"],
                        "pr_number": fact["pr_number"],
                        "commit_sha": row.get("commit_sha"),
                        "event_type": row.get("event_type") or "committed",
                        "event_at": row.get("event_at"),
                        "actor_login": row.get("actor_login"),
                        "last_synced_at": synced_at,
                    }
                )
            db.clear_pr_commit_events_for_pr(fact["pr_url"])
            db.upsert_pr_commit_events(timeline_rows)

            updated += 1
            commits_final += len(final_rows)
            commit_events += len(timeline_rows)
            if updated % 25 == 0:
                db.commit()
        except Exception as exc:
            errors += 1
            commit_errors += 1
            if verbose:
                print(f"GitHub enrichment failed for {pr_url}: {exc}")

    db.commit()
    return {
        "attempted": attempted,
        "updated": updated,
        "errors": errors,
        "commit_errors": commit_errors,
        "commits_final": commits_final,
        "commit_events": commit_events,
        "skipped": False,
    }
