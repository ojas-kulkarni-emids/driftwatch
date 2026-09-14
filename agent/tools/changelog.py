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
    "CHANGELOG.txt",
    "HISTORY.rst",
    "HISTORY.md",
    "NEWS.rst",
    "docs/changelog.rst",
    "docs/changelog.md",
    "docs/CHANGELOG.md",
    "docs/release-notes.md",
    "docs/en/docs/release-notes.md",  # FastAPI and projects following its docs layout
    "doc/changelog.rst",
]

# Releases come back newest-first. Page until we're safely past the bottom of the
# requested range rather than assuming it fits in one page -- an actively-released
# library can have 100+ releases newer than the version you're asking about, which
# silently returned "no changelog" before this was paginated.
_MAX_RELEASE_PAGES = 10

_RST_UNDERLINE_CHARS = set("~-=^\"'*+#`")

# Fast path and override list. PyPI resolution (below) handles everything else,
# but these are kept because they're the demo libraries and because PyPI
# occasionally points at a mirror or a docs site rather than the real repo.
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

# find_usages takes the *import* name, which differs from the PyPI package name
# more often than you'd expect. Only the cases where guessing fails.
_IMPORT_TO_PYPI = {
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "dateutil": "python-dateutil",
    "jwt": "PyJWT",
    "dotenv": "python-dotenv",
    "attr": "attrs",
    "openssl": "pyOpenSSL",
    "serial": "pyserial",
    "usb": "pyusb",
    "crypto": "pycryptodome",
    "pkg_resources": "setuptools",
    "google": "protobuf",
    "mpl_toolkits": "matplotlib",
    "zoneinfo": "backports.zoneinfo",
}

PYPI_API = "https://pypi.org/pypi/{}/json"

# Project-URL keys that actually point at source, most reliable first. PyPI lets
# maintainers name these freely, so there's no canonical key to rely on.
_SOURCE_URL_KEYS = ("source", "source code", "repository", "code", "github", "homepage", "home")

# Paths under github.com that are never a repo.
_NON_REPO_OWNERS = {"sponsors", "orgs", "features", "about", "pricing", "apps", "marketplace"}

_repo_cache: dict[str, str | None] = {}


def _extract_github_repo(url: str) -> str | None:
    """Pull 'owner/repo' out of any GitHub URL form, or None if it isn't one."""
    if not url or "github.com" not in url.lower():
        return None
    tail = url.split("github.com", 1)[1]
    tail = tail.lstrip(":/")  # handles both https://github.com/x/y and git@github.com:x/y
    parts = [p for p in tail.split("?")[0].split("#")[0].split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo or owner.lower() in _NON_REPO_OWNERS:
        return None
    return f"{owner}/{repo}"


def _pypi_candidates(library: str) -> list[str]:
    """Package names to try on PyPI for a given import name, best guess first."""
    lowered = library.lower()
    candidates = []
    mapped = _IMPORT_TO_PYPI.get(lowered)
    if mapped:
        candidates.append(mapped)
    candidates.append(library)
    if "_" in library:
        candidates.append(library.replace("_", "-"))
    if "-" in library:
        candidates.append(library.replace("-", "_"))
    seen = set()
    return [c for c in candidates if not (c.lower() in seen or seen.add(c.lower()))]


def _resolve_from_pypi(library: str) -> str | None:
    """Look up a library's GitHub repo via PyPI project metadata."""
    for name in _pypi_candidates(library):
        try:
            resp = http.get(PYPI_API.format(name), timeout=10, headers={"User-Agent": "driftwatch"})
        except http.RequestException:
            return None
        if not resp.ok:
            continue
        try:
            info = resp.json().get("info") or {}
        except ValueError:
            continue

        project_urls = {k.lower(): v for k, v in (info.get("project_urls") or {}).items() if v}
        for key in _SOURCE_URL_KEYS:
            repo = _extract_github_repo(project_urls.get(key, ""))
            if repo:
                return repo
        # No recognised key matched -- take any project URL that is a GitHub repo.
        for value in project_urls.values():
            repo = _extract_github_repo(value)
            if repo:
                return repo
        repo = _extract_github_repo(info.get("home_page") or "")
        if repo:
            return repo
    return None


def resolve_repo(library: str, github_repo: str = "") -> str | None:
    """Resolve a library to 'owner/repo': explicit override, then the built-in
    map, then PyPI metadata. Results (including failures) are cached per process
    so a repeated lookup in one agent run costs nothing."""
    if github_repo:
        return github_repo
    key = library.lower()
    if key in _KNOWN_REPOS:
        return _KNOWN_REPOS[key]
    if key in _repo_cache:
        return _repo_cache[key]
    resolved = _resolve_from_pypi(library)
    _repo_cache[key] = resolved
    return resolved


def _unresolved_error(library: str) -> dict:
    return {
        "status": "error",
        "content": [
            {
                "text": (
                    f"Could not resolve a GitHub repo for '{library}'. It wasn't in the built-in map "
                    f"and PyPI has no GitHub project URL for it (note: find_usages takes the *import* "
                    f"name, which sometimes differs from the PyPI package name). "
                    f"Pass github_repo explicitly as 'owner/repo' to continue."
                )
            }
        ],
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
    repo = resolve_repo(library, github_repo)
    if not repo:
        return _unresolved_error(library)

    from_v = _normalize(from_version)
    to_v = _normalize(to_version)
    if from_v is None or to_v is None:
        return {
            "status": "error",
            "content": [{"text": f"Could not parse version range {from_version!r} -> {to_version!r}."}],
        }

    in_range = []
    unparseable_tags = []
    pages_fetched = 0
    reached_bottom = False

    for page in range(1, _MAX_RELEASE_PAGES + 1):
        try:
            resp = http.get(
                f"{GITHUB_API}/repos/{repo}/releases",
                headers=_headers(),
                params={"per_page": 100, "page": page},
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

        batch = resp.json()
        pages_fetched = page
        if not batch:
            reached_bottom = True
            break

        for r in batch:
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

        # Releases are newest-first, so once a page contains anything at or below
        # from_version we've covered the whole range and can stop paging.
        if any((v := _normalize(r["tag_name"])) is not None and v <= from_v for r in batch):
            reached_bottom = True
            break
        if len(batch) < 100:
            reached_bottom = True
            break

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
    if not reached_bottom:
        result["truncated"] = (
            f"Stopped after {pages_fetched} pages ({pages_fetched * 100} releases) without reaching "
            f"{from_version}. Older releases in the range may be missing."
        )

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
