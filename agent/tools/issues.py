"""search_github_issues -- for when the changelog is vague and the agent
needs to check whether other people actually hit a problem with this bump.
This is the second branch that makes the tool sequence differ bump-to-bump:
a clear changelog never needs this call at all.
"""

import os

import requests as http
from strands import tool

from .changelog import _KNOWN_REPOS, _headers

GITHUB_API = "https://api.github.com"


@tool
def search_github_issues(library: str, terms: str, github_repo: str = "") -> dict:
    """Search a library's GitHub issues/PRs for reports related to specific terms.

    Use this when a changelog entry is vague and you need real-world signal
    on whether a version bump actually broke something for other users --
    e.g. terms="verify SSL" or terms="getheader AttributeError". Results are
    sorted by comment count, as a rough proxy for how significant an issue is.

    Args:
        library: The library's import/package name, e.g. "urllib3".
        terms: Free-text search terms describing the behavior in question.
        github_repo: Optional "owner/repo" override if the library isn't in the built-in mapping.
    """
    repo = github_repo or _KNOWN_REPOS.get(library.lower())
    if not repo:
        return {
            "status": "error",
            "content": [{"text": f"No known GitHub repo for '{library}'. Pass github_repo explicitly."}],
        }

    query = f"repo:{repo} {terms}"
    try:
        resp = http.get(
            f"{GITHUB_API}/search/issues",
            headers=_headers(),
            params={"q": query, "sort": "comments", "order": "desc", "per_page": 8},
            timeout=15,
        )
    except http.RequestException as e:
        return {"status": "error", "content": [{"text": f"Could not reach GitHub: {e}"}]}

    if resp.status_code == 403:
        return {
            "status": "error",
            "content": [{"text": "GitHub API rate-limited (403). Set GITHUB_TOKEN -- unauthenticated is 60/hr and shared per IP."}],
        }
    if not resp.ok:
        return {"status": "error", "content": [{"text": f"GitHub returned HTTP {resp.status_code}: {resp.text[:300]}"}]}

    data = resp.json()
    items = [
        {
            "title": i["title"],
            "state": i["state"],
            "comments": i["comments"],
            "created_at": i["created_at"],
            "html_url": i["html_url"],
            "is_pull_request": "pull_request" in i,
            "body_excerpt": (i.get("body") or "")[:400],
        }
        for i in data.get("items", [])
    ]

    return {
        "status": "success",
        "content": [{"json": {"repo": repo, "query": query, "total_count": data.get("total_count", 0), "items": items}}],
    }
