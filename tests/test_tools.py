"""Manual checks against real data, not mocks.

Run: python -m tests.test_tools

find_usages needs nothing (pure local AST scan). get_changelog and
search_github_issues hit live GitHub and need GITHUB_TOKEN in the
environment (unauthenticated is 60/hr, shared per IP -- already exhausted
once during verification). The full agent loop needs the Azure env vars
and only runs if they're set.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from agent.tools.changelog import get_changelog
from agent.tools.context import get_file_context
from agent.tools.issues import search_github_issues
from agent.tools.usages import find_usages

FIXTURE_REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "demo_repo")


def _print(label: str, result: dict) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(result, indent=2)[:3000])


def main() -> None:
    _print("find_usages: urllib3 in the fixture repo", find_usages("urllib3", FIXTURE_REPO))
    _print("find_usages: requests in the fixture repo", find_usages("requests", FIXTURE_REPO))
    _print("find_usages: a library not used anywhere (sanity check for empty result)", find_usages("flask", FIXTURE_REPO))

    fixture_file = os.path.join(FIXTURE_REPO, "api_client.py")
    _print("get_file_context: around the getheader() call", get_file_context(fixture_file, 22, context=3))

    if os.getenv("GITHUB_TOKEN"):
        _print(
            "get_changelog: urllib3 1.26.15 -> 2.2.1 (real breaking-change release)",
            get_changelog("urllib3", "1.26.15", "2.2.1"),
        )
        _print(
            "get_changelog: requests 2.30.0 -> 2.31.0 (real CVE fix release)",
            get_changelog("requests", "2.30.0", "2.31.0"),
        )
        _print(
            "search_github_issues: requests + proxy redirect",
            search_github_issues("requests", "Proxy-Authorization redirect"),
        )
    else:
        print("\n(skipping GitHub-backed tools -- GITHUB_TOKEN not set)")

    azure_ready = all(os.getenv(k) for k in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL", "AZURE_DEPLOYMENT_NAME"))
    if azure_ready and os.getenv("GITHUB_TOKEN"):
        from agent.agent import analyze_bump, build_agent

        agent = build_agent()
        print("\n=== full agent loop: urllib3 1.26.15 -> 2.2.1 against the fixture repo ===")
        print(analyze_bump(agent, FIXTURE_REPO, "urllib3", "1.26.15", "2.2.1"))
    else:
        print("\n(skipping full agent test -- Azure env vars and/or GITHUB_TOKEN not set)")


if __name__ == "__main__":
    main()
