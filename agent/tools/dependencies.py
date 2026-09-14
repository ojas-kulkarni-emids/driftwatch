"""list_dependencies -- reads a repo's declared dependencies and what version
each is pinned to, then asks PyPI what the latest version is.

This turns the product from "audit this one bump I already knew about" into
"point it at a repo and show me what's drifted". It is deliberately a separate
tool from find_usages: this one answers *which libraries could move*, and
find_usages answers *whether we actually use them*.

Supports requirements.txt (including -r includes) and pyproject.toml in both
PEP 621 and Poetry layouts. Version specifiers are parsed with `packaging`
rather than by hand, so extras and environment markers don't trip it up.
"""

import os
import tomllib
from concurrent.futures import ThreadPoolExecutor

import requests as http
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version
from strands import tool

from .changelog import _IMPORT_TO_PYPI, PYPI_API

_REQUIREMENTS_NAMES = ["requirements.txt", "requirements/base.txt", "requirements/prod.txt", "requirements-dev.txt"]

# Reverse of the import->PyPI map, for guessing what find_usages should be given.
_PYPI_TO_IMPORT = {v.lower(): k for k, v in _IMPORT_TO_PYPI.items()}


def _import_name_for(package: str) -> str:
    """Best guess at the import name for a PyPI package name."""
    return _PYPI_TO_IMPORT.get(package.lower(), package.replace("-", "_").lower())


def _pinned_version(spec: str) -> str | None:
    """Extract an exact pinned version from a specifier set, if there is one.

    `==1.2.3` gives a version. `>=1.0,<2` does not -- there's no single current
    version, and reporting one would be a guess presented as a fact.
    """
    for clause in str(spec).split(","):
        clause = clause.strip()
        if clause.startswith("==") and not clause.endswith("*"):
            candidate = clause[2:].strip()
            try:
                Version(candidate)
                return candidate
            except InvalidVersion:
                return None
    return None


def _parse_requirements(path: str, seen: set[str], out: dict) -> None:
    """Parse a requirements file, following -r includes."""
    real = os.path.realpath(path)
    if real in seen or not os.path.isfile(real):
        return
    seen.add(real)

    with open(real, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.read().splitlines()

    # Join backslash continuations before parsing.
    lines, buffer = [], ""
    for line in raw_lines:
        if line.rstrip().endswith("\\"):
            buffer += line.rstrip()[:-1]
            continue
        lines.append(buffer + line)
        buffer = ""
    if buffer:
        lines.append(buffer)

    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            included = line.split(maxsplit=1)[1].strip()
            _parse_requirements(os.path.join(os.path.dirname(real), included), seen, out)
            continue
        if line.startswith("-"):  # -e, --index-url, --find-links, etc.
            continue
        if "://" in line:  # direct URL / VCS install -- no meaningful version
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        out.setdefault(
            req.name,
            {
                "package": req.name,
                "import_name": _import_name_for(req.name),
                "specifier": str(req.specifier) or "(unpinned)",
                "current_version": _pinned_version(req.specifier),
                "source_file": os.path.basename(real),
            },
        )


def _parse_pyproject(path: str, out: dict) -> None:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return

    # PEP 621: [project] dependencies + optional-dependencies
    project = data.get("project") or {}
    pep621 = list(project.get("dependencies") or [])
    for extra_deps in (project.get("optional-dependencies") or {}).values():
        pep621.extend(extra_deps)
    for entry in pep621:
        try:
            req = Requirement(entry)
        except InvalidRequirement:
            continue
        out.setdefault(
            req.name,
            {
                "package": req.name,
                "import_name": _import_name_for(req.name),
                "specifier": str(req.specifier) or "(unpinned)",
                "current_version": _pinned_version(req.specifier),
                "source_file": "pyproject.toml",
            },
        )

    # Poetry: [tool.poetry.dependencies] is a table, not PEP 508 strings.
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        if isinstance(spec, dict):
            spec = spec.get("version", "")
        spec = str(spec or "")
        # Poetry's ^1.2.3 / ~1.2.3 are caret/tilde ranges, not exact pins.
        exact = spec.lstrip("=").strip() if spec.startswith("==") else None
        out.setdefault(
            name,
            {
                "package": name,
                "import_name": _import_name_for(name),
                "specifier": spec or "(unpinned)",
                "current_version": exact,
                "source_file": "pyproject.toml",
            },
        )


def _latest_version(package: str) -> str | None:
    try:
        resp = http.get(PYPI_API.format(package), timeout=10, headers={"User-Agent": "driftwatch"})
    except http.RequestException:
        return None
    if not resp.ok:
        return None
    try:
        return ((resp.json() or {}).get("info") or {}).get("version")
    except ValueError:
        return None


@tool
def list_dependencies(repo_path: str, check_latest: bool = True) -> dict:
    """List a repo's declared dependencies, their pinned versions, and the latest available.

    Reads requirements.txt (following -r includes) and pyproject.toml (both PEP 621
    and Poetry layouts). Use this to find which libraries could be bumped before
    auditing any specific one -- it answers "what could move", not "do we use it".

    A dependency whose specifier is a range rather than an exact `==` pin has no
    single current version; `current_version` is null for those and
    `has_update` cannot be determined.

    Args:
        repo_path: Path to the root of the codebase to inspect.
        check_latest: Query PyPI for each package's latest version. Set false to skip the network calls.
    """
    if not os.path.isdir(repo_path):
        return {"status": "error", "content": [{"text": f"repo_path does not exist or is not a directory: {repo_path}"}]}

    found: dict[str, dict] = {}
    files_read = []

    for name in _REQUIREMENTS_NAMES:
        candidate = os.path.join(repo_path, name)
        if os.path.isfile(candidate):
            _parse_requirements(candidate, set(), found)
            files_read.append(name)

    pyproject = os.path.join(repo_path, "pyproject.toml")
    if os.path.isfile(pyproject):
        _parse_pyproject(pyproject, found)
        files_read.append("pyproject.toml")

    if not found:
        return {
            "status": "success",
            "content": [
                {
                    "json": {
                        "repo_path": repo_path,
                        "files_read": files_read,
                        "dependencies": [],
                        "note": "No requirements.txt or pyproject.toml dependencies found at this path.",
                    }
                }
            ],
        }

    deps = sorted(found.values(), key=lambda d: d["package"].lower())

    if check_latest:
        # Serial lookups would make a 30-dependency repo painfully slow in the UI.
        with ThreadPoolExecutor(max_workers=8) as pool:
            latest_versions = list(pool.map(lambda d: _latest_version(d["package"]), deps))
        for dep, latest in zip(deps, latest_versions):
            dep["latest_version"] = latest
            current = dep.get("current_version")
            if current and latest:
                try:
                    dep["has_update"] = Version(latest) > Version(current)
                except InvalidVersion:
                    dep["has_update"] = None
            else:
                dep["has_update"] = None

    return {
        "status": "success",
        "content": [
            {
                "json": {
                    "repo_path": repo_path,
                    "files_read": files_read,
                    "dependency_count": len(deps),
                    "dependencies": deps,
                }
            }
        ],
    }
