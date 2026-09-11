"""get_changelog -- pulls the real release notes between two versions of a
library.

Two sources, tried in order:

1. GitHub Releases API. Clean and structured, but OPTIONAL for maintainers.
   Verified live: 5,000 req/hr with a PAT vs 60/hr without.

2. The repo's own changelog FILE (CHANGELOG.rst / CHANGELOG.md / ...),
   fetched via the Contents API. This fallback exists because some projects
   never use GitHub's Releases feature at all -- pyca/cryptography tags every
   version but publishes zero Releases, so source 1 returns literally nothing
   for it while a complete 131KB CHANGELOG.rst sits in the repo root. Without
   this fallback the agent had no evidence at all for such libraries and
   (correctly but uselessly) answered "unclear" for every call site.

A whole changelog file is far too large to hand to a model, so it's sliced to
just the version sections inside the requested range, using a deliberately
conservative header detector -- a line only counts as a version header if its
first token parses as a version AND it is either a markdown heading, followed
by an RST underline, or carries a date. Prose like "3.14 support added" would
otherwise be misread as a section boundary.

PyPI import name -> GitHub owner/repo isn't derivable in general, so v1 ships
a small known-good mapping plus an explicit github_repo override. Documented
here rather than hidden, since it's a real v1 limitation.
"""

import base64
import os

import requests as http
from packaging.version import InvalidVersion, Version
from strands import tool

GITHUB_API = "https://api.github.com"

# Per-section and total caps on fallback changelog text handed to the model.
_MAX_SECTION_CHARS = 4000
_MAX_TOTAL_CHARS = 30000

_CHANGELOG_PATHS = [
    "CHANGELOG.rst",
    "CHANGELOG.md",
    "CHANGES.rst",
    "CHANGES.md",
    "CHANGELOG",
    "HISTORY.rst",
    "HISTORY.md",
    "docs/changelog.rst",
    "docs/CHANGELOG.md",
]

_RST_UNDERLINE_CHARS = set("~-=^\"'*+#`")

_KNOWN_REPOS = {
    "urllib3": "urllib3/urllib3",
    "requests": "psf/requests",
    "click": "pallets/click",
    "flask": "pallets/flask",
    "pydantic": "pydantic/pydantic",
    "numpy": "numpy/numpy",
    "django": "django/django",
    "cryptography": "pyca/cryptography",
}


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "driftwatch"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _normalize(tag: str) -> Version | None:
    try:
        return Version(tag.lstrip("vV"))
    except InvalidVersion:
        return None


def _has_date(line: str) -> bool:
    """Cheap check for a YYYY-MM-DD or (YYYY-MM-DD) style date on the line."""
    for i in range(len(line) - 9):
        chunk = line[i : i + 10]
        if chunk[4] == "-" and chunk[7] == "-" and chunk[:4].isdigit() and chunk[5:7].isdigit() and chunk[8:].isdigit():
            return True
    return False


def _is_rst_underline(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 3 and set(stripped) <= _RST_UNDERLINE_CHARS and len(set(stripped)) == 1


def _header_version(lines: list[str], i: int) -> Version | None:
    """Return the version if line i is a changelog section header, else None.

    Conservative on purpose: the first token must parse as a version AND the
    line must look structurally like a heading (markdown '#', an RST underline
    on the next line, or an accompanying date). Otherwise ordinary prose that
    happens to start with a number becomes a false section boundary.
    """
    line = lines[i]
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return None

    is_markdown_heading = stripped.startswith("#")
    candidate = stripped.lstrip("#").strip().lstrip("[").strip()
    tokens = candidate.split()
    if not tokens:
        return None

    version = _normalize(tokens[0].strip("[](),:"))
    if version is None:
        return None

    followed_by_underline = i + 1 < len(lines) and _is_rst_underline(lines[i + 1])
    if is_markdown_heading or followed_by_underline or _has_date(stripped):
        return version
    return None


def _fetch_changelog_file(repo: str) -> tuple[str, str] | None:
    """Fetch the first changelog file found in the repo. Returns (path, text)."""
    for path in _CHANGELOG_PATHS:
        try:
            resp = http.get(f"{GITHUB_API}/repos/{repo}/contents/{path}", headers=_headers(), timeout=15)
        except http.RequestException:
            return None
        if not resp.ok:
            continue
        payload = resp.json()
        if payload.get("encoding") != "base64" or "content" not in payload:
            continue
        try:
            text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            continue
        return path, text
    return None


def _extract_sections(text: str, from_v: Version, to_v: Version) -> list[dict]:
    """Slice a changelog file down to only the sections in (from_v, to_v]."""
    lines = text.splitlines()
    headers = []
    for i in range(len(lines)):
        version = _header_version(lines, i)
        if version is not None:
            headers.append((i, version))

    sections = []
    total = 0
    for idx, (line_i, version) in enumerate(headers):
        if not (from_v < version <= to_v):
            continue
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        body = "\n".join(lines[line_i:end]).strip()
        truncated = False
        if len(body) > _MAX_SECTION_CHARS:
            body = body[:_MAX_SECTION_CHARS] + "\n... [section truncated]"
            truncated = True
        if total + len(body) > _MAX_TOTAL_CHARS:
            break
        total += len(body)
        sections.append({"version": str(version), "body": body, "truncated": truncated})

    sections.sort(key=lambda s: Version(s["version"]))
    return sections


@tool
def get_changelog(library: str, from_version: str, to_version: str, github_repo: str = "") -> dict:
    """Fetch real changelog text for a library between two versions.

    Tries GitHub Releases first. If the project publishes no Releases in that
    range (common -- Releases are optional for maintainers), falls back to the
    changelog file committed in the repo itself, sliced to just the relevant
    version sections. The `source` field in the result says which was used.

    Treat an empty or vague result as a signal to look elsewhere (e.g. search
    issues), not as evidence the bump is safe.

    Args:
        library: The library's import/package name, e.g. "urllib3".
        from_version: The version currently in use, e.g. "1.26.15".
        to_version: The version being proposed, e.g. "2.2.1".
        github_repo: Optional "owner/repo" override if the library isn't in the built-in mapping.
    """
    repo = github_repo or _KNOWN_REPOS.get(library.lower())
    if not repo:
        return {
            "status": "error",
            "content": [
                {
                    "text": (
                        f"No known GitHub repo for '{library}'. Pass github_repo explicitly "
                        f"(e.g. 'owner/repo') -- v1 only auto-resolves: {', '.join(_KNOWN_REPOS)}."
                    )
                }
            ],
        }

    from_v = _normalize(from_version)
    to_v = _normalize(to_version)
    if from_v is None or to_v is None:
        return {
            "status": "error",
            "content": [{"text": f"Could not parse version range {from_version!r} -> {to_version!r}."}],
        }

    try:
        resp = http.get(f"{GITHUB_API}/repos/{repo}/releases", headers=_headers(), params={"per_page": 100}, timeout=15)
    except http.RequestException as e:
        return {"status": "error", "content": [{"text": f"Could not reach GitHub: {e}"}]}

    if resp.status_code == 403:
        return {
            "status": "error",
            "content": [{"text": "GitHub API rate-limited (403). Set GITHUB_TOKEN -- unauthenticated is 60/hr and shared per IP."}],
        }
    if not resp.ok:
        return {"status": "error", "content": [{"text": f"GitHub returned HTTP {resp.status_code}: {resp.text[:300]}"}]}

    in_range = []
    unparseable_tags = []
    for r in resp.json():
        v = _normalize(r["tag_name"])
        if v is None:
            unparseable_tags.append(r["tag_name"])
            continue
        if from_v < v <= to_v:
            in_range.append(
                {
                    "tag": r["tag_name"],
                    "name": r.get("name"),
                    "published_at": r.get("published_at"),
                    "prerelease": r.get("prerelease", False),
                    "body": r.get("body") or "(no release notes written for this tag)",
                    "html_url": r.get("html_url"),
                }
            )
    in_range.sort(key=lambda r: r["published_at"] or "")

    result = {
        "repo": repo,
        "from_version": from_version,
        "to_version": to_version,
        "source": "github_releases",
        "releases_in_range": in_range,
        "release_count": len(in_range),
        "unparseable_tags_skipped": unparseable_tags,
    }

    if in_range:
        return {"status": "success", "content": [{"json": result}]}

    # No Releases in range -- fall back to the changelog file in the repo.
    fetched = _fetch_changelog_file(repo)
    if fetched is None:
        result["source"] = "none"
        result["note"] = (
            f"{repo} published no GitHub Releases in this range and no changelog file was found "
            f"at any of: {', '.join(_CHANGELOG_PATHS)}. No changelog evidence is available from "
            "this tool -- do not treat that as evidence the bump is safe."
        )
        return {"status": "success", "content": [{"json": result}]}

    path, text = fetched
    sections = _extract_sections(text, from_v, to_v)
    result["source"] = "changelog_file"
    result["changelog_file"] = path
    result["changelog_file_url"] = f"https://github.com/{repo}/blob/HEAD/{path}"
    result["sections_in_range"] = sections
    result["section_count"] = len(sections)
    if not sections:
        result["note"] = (
            f"Found {path} in {repo} but no version sections matched the range "
            f"{from_version} -> {to_version}. The file may use an unrecognized heading format."
        )
    return {"status": "success", "content": [{"json": result}]}
