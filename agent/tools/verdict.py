"""post_verdict -- writes the audit back to the PR that triggered it.

Everything else in this toolkit is read-only and works with a no-scope token.
This one needs write access, so it is deliberately opt-in and defaults to a
dry run: it formats the comment and returns it without posting unless the
caller explicitly asks to publish. That keeps the demo working, and the token
requirement in the README honest, for anyone who only wants the analysis.

Note GitHub exposes PR comments under /issues/{number}/comments, not /pulls/ --
the /pulls/ comments endpoint is for line-level review comments instead.
"""

import os

import requests as http
from strands import tool

from .changelog import _headers, _unresolved_error, resolve_repo

GITHUB_API = "https://api.github.com"

_FOOTER = (
    "\n\n---\n*Posted by [driftwatch](https://github.com/ojas-kulkarni-emids/driftwatch) — "
    "audits a dependency bump against how your code actually uses the library. "
    "Verdicts are evidence-based judgements, not guarantees; confirm before merging.*"
)


def _format_comment(library: str, from_version: str, to_version: str, body: str) -> str:
    return f"## Dependency audit: `{library}` {from_version} → {to_version}\n\n{body.strip()}{_FOOTER}"


@tool
def post_verdict(
    library: str,
    from_version: str,
    to_version: str,
    body: str,
    pr_number: int,
    github_repo: str = "",
    publish: bool = False,
) -> dict:
    """Format the audit as a PR comment, and optionally post it to GitHub.

    Defaults to a dry run that returns the formatted comment without posting.
    Only set publish=true when the user has explicitly asked for the verdict to
    be posted to the pull request.

    Args:
        library: The library that was audited, e.g. "urllib3".
        from_version: The version being bumped from.
        to_version: The version being bumped to.
        body: The full per-call-site verdict text, in markdown.
        pr_number: The pull request number to comment on.
        github_repo: Target repo as "owner/name". Required when it differs from the audited library's repo.
        publish: Actually post the comment. Requires GITHUB_TOKEN to have write access.
    """
    repo = resolve_repo(library, github_repo)
    if not repo:
        return _unresolved_error(library)

    comment = _format_comment(library, from_version, to_version, body)

    if not publish:
        return {
            "status": "success",
            "content": [
                {
                    "json": {
                        "posted": False,
                        "mode": "dry_run",
                        "target": f"{repo}#{pr_number}",
                        "comment_markdown": comment,
                        "note": "Dry run -- nothing was posted. Call again with publish=true to post it.",
                    }
                }
            ],
        }

    if not os.getenv("GITHUB_TOKEN"):
        return {
            "status": "error",
            "content": [{"text": "GITHUB_TOKEN is not set, so the comment cannot be posted."}],
        }

    try:
        resp = http.post(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            headers=_headers(),
            json={"body": comment},
            timeout=15,
        )
    except http.RequestException as e:
        return {"status": "error", "content": [{"text": f"Could not reach GitHub: {e}"}]}

    if resp.status_code in (401, 403, 404):
        # 404 is what GitHub returns for a private/nonexistent repo AND for a
        # token without write access, so don't claim to know which it is.
        return {
            "status": "error",
            "content": [
                {
                    "text": (
                        f"GitHub refused the comment (HTTP {resp.status_code}). The token most likely "
                        f"lacks write access to {repo} -- the read-only setup in the README is not "
                        f"enough for this tool. It needs 'public_repo' scope (or 'repo' for private). "
                        f"The repo or PR number may also be wrong."
                    )
                }
            ],
        }
    if not resp.ok:
        return {"status": "error", "content": [{"text": f"GitHub returned HTTP {resp.status_code}: {resp.text[:300]}"}]}

    created = resp.json()
    return {
        "status": "success",
        "content": [
            {
                "json": {
                    "posted": True,
                    "target": f"{repo}#{pr_number}",
                    "comment_url": created.get("html_url"),
                }
            }
        ],
    }
