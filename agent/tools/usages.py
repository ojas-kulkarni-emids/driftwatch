"""find_usages -- deterministic, free AST scan for how a repo actually uses
a library. This is the whole cost argument: we never send the repo to the
model, only the handful of call sites this returns, so cost scales with
usage of ONE library, not repo size.

Deliberately Python-only (stdlib `ast`), per scope. Tracks three kinds of
usage per matched name, keyed by (line, col) so a call site is never
double-counted as both a "call" and a bare "attribute":
  - import      the import statement itself, so a human can see which
                 alias to look for
  - call        `alias.method(...)` or a from-imported symbol called
                 directly -- the snippet is the full call expression
                 (all its arguments included), not just the function name
  - attribute   a non-call reference, e.g. an exception type in
                 `except urllib3.exceptions.MaxRetryError:` or a bare name
                 passed around without being called

Also does light, best-effort instance tracking: `http = urllib3.PoolManager()`
then `http.request(...)` -- without this, only the literal `urllib3.foo(...)`
spelling would match, missing the overwhelming majority of real usage, which
goes through an instance obtained from a factory call. This is a single-pass,
order-dependent heuristic (the assignment must appear before its use in
source order) with no real scope tracking -- two functions that each reuse a
local variable name for different things can collide. That's an accepted v1
limitation: real type inference is out of scope for four days, and this
heuristic still catches what matters for a version-bump audit -- chained
calls off a client/session object.
"""

import ast
import os

from strands import tool


def module_name_for(root: str, path: str) -> str:
    """Map a file path to its dotted module name relative to the repo root.

    `<root>/pkg/clients.py` -> `pkg.clients`; `<root>/pkg/__init__.py` -> `pkg`.
    """
    rel = os.path.relpath(path, root).replace("\\", "/")
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _resolve_relative_module(current_module: str, is_package: bool, level: int, module: str | None) -> str | None:
    """Resolve `from ..pkg import x` to an absolute intra-repo module name."""
    parts = current_module.split(".") if current_module else []
    # For a package's __init__.py the module name IS the package, so level 1
    # stays put; for a plain module, level 1 means its containing package.
    base = parts if is_package else parts[:-1]
    climb = level - 1
    if climb > len(base):
        return None
    base = base[: len(base) - climb] if climb else base
    full = base + ([module] if module else [])
    return ".".join(p for p in full if p) or None


class _ImportTracker(ast.NodeVisitor):
    def __init__(
        self,
        library: str,
        exports: dict[str, dict[str, str]] | None = None,
        module_name: str = "",
        is_package: bool = False,
    ):
        self.library = library
        self.top_level_aliases: set[str] = set()  # bound to the module itself, e.g. `requests`, `req`
        self.imported_names: dict[str, str] = {}  # bound name -> qualified name, e.g. `get` -> "requests.get"
        self.instance_vars: dict[str, str] = {}  # local var -> best-effort label, e.g. `http` -> "urllib3.PoolManager"
        self.imports: list[dict] = []
        self._seen: dict[tuple[int, int], dict] = {}
        # Pass-2 inputs: what other modules in this repo export, and who we are.
        self.exports = exports or {}
        self.module_name = module_name
        self.is_package = is_package
        self.inherited: list[dict] = []  # cross-file lineage picked up from a sibling module

    def _is_library_module(self, modname: str) -> bool:
        return modname == self.library or modname.startswith(self.library + ".")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self._is_library_module(alias.name):
                bound = alias.asname or alias.name.split(".")[0]
                self.top_level_aliases.add(bound)
                self.imports.append({"line": node.lineno, "statement": f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")})
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and self._is_library_module(node.module):
            names = []
            for alias in node.names:
                bound = alias.asname or alias.name
                self.imported_names[bound] = f"{node.module}.{alias.name}"
                names.append(alias.name + (f" as {alias.asname}" if alias.asname else ""))
            self.imports.append({"line": node.lineno, "statement": f"from {node.module} import {', '.join(names)}"})
            self.generic_visit(node)
            return

        # Not the library itself -- but it may be a module in THIS repo that holds
        # something built from the library (`from clients import http`). Without
        # this, the near-universal shared-client pattern is invisible.
        if self.exports:
            target = (
                _resolve_relative_module(self.module_name, self.is_package, node.level, node.module)
                if node.level
                else node.module
            )
            exported = self.exports.get(target or "")
            if exported:
                names = []
                for alias in node.names:
                    lineage = exported.get(alias.name)
                    if not lineage:
                        continue
                    bound = alias.asname or alias.name
                    self.instance_vars[bound] = lineage
                    names.append(alias.name + (f" as {alias.asname}" if alias.asname else ""))
                if names:
                    statement = f"from {target} import {', '.join(names)}"
                    self.imports.append({"line": node.lineno, "statement": statement, "via": target})
                    self.inherited.append({"line": node.lineno, "from_module": target, "names": names})
        self.generic_visit(node)

    def _resolve(self, node: ast.expr) -> str | None:
        """Walk a dotted attribute chain back to a tracked import; None if unrelated."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            root = cur.id
            if root in self.top_level_aliases:
                parts.append(root)
                return ".".join(reversed(parts))
            if root in self.imported_names:
                return self.imported_names[root] + ("." + ".".join(reversed(parts)) if parts else "")
            if root in self.instance_vars:
                return self.instance_vars[root] + ("." + ".".join(reversed(parts)) if parts else "")
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            target_label = self._resolve(node.value.func)
            if target_label:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.instance_vars[t.id] = target_label
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        if target:
            key = (node.lineno, node.col_offset)
            self._seen[key] = {"kind": "call", "matched": target, "end_line": getattr(node, "end_lineno", node.lineno)}
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        target = self._resolve(node)
        if target:
            key = (node.lineno, node.col_offset)
            if key not in self._seen:  # a Call already claimed this position -- don't downgrade it
                self._seen[key] = {"kind": "attribute", "matched": target, "end_line": getattr(node, "end_lineno", node.lineno)}
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.imported_names:
            key = (node.lineno, node.col_offset)
            if key not in self._seen:
                self._seen[key] = {"kind": "reference", "matched": self.imported_names[node.id], "end_line": node.lineno}
        self.generic_visit(node)


def _snippet(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1 : end_line]).strip()


def _read_and_parse(path: str) -> tuple[str, ast.Module] | dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as e:
        return {"file": path, "error": str(e)}
    try:
        return source, ast.parse(source, filename=path)
    except SyntaxError as e:
        return {"file": path, "error": f"SyntaxError: {e}"}


def _collect_exports(tree: ast.Module, library: str) -> dict[str, str]:
    """Module-level names in this file that trace back to the library.

    Only module level: a name bound inside a function can't be imported by
    another module, so including those would invent cross-file links that
    don't exist.
    """
    tracker = _ImportTracker(library)
    tracker.visit(tree)
    exported = dict(tracker.imported_names)  # re-exported symbols, e.g. `from urllib3 import PoolManager`
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        lineage = tracker._resolve(node.value.func)
        if not lineage:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                exported[target.id] = lineage
    return exported


def _scan_file(
    path: str,
    library: str,
    exports: dict[str, dict[str, str]] | None = None,
    module_name: str = "",
    is_package: bool = False,
    parsed: tuple[str, ast.Module] | None = None,
) -> dict | None:
    outcome = parsed or _read_and_parse(path)
    if isinstance(outcome, dict):
        return outcome
    source, tree = outcome

    tracker = _ImportTracker(library, exports=exports, module_name=module_name, is_package=is_package)
    tracker.visit(tree)
    if not tracker.imports:
        return None  # file doesn't import this library at all -- not worth reporting

    lines = source.splitlines()

    # Group by source line. One line often references several library symbols
    # -- `Cipher(algorithms.Blowfish(key), modes.CBC(iv))` resolves to three --
    # and emitting one entry per symbol makes a reader (or a model) produce
    # three near-duplicate findings for what is really one place in the code.
    by_line: dict[int, dict] = {}
    for (lineno, _col), info in sorted(tracker._seen.items()):
        entry = by_line.setdefault(lineno, {"line": lineno, "kinds": set(), "matched": [], "end_line": lineno})
        entry["kinds"].add(info["kind"])
        entry["end_line"] = max(entry["end_line"], info["end_line"])
        if info["matched"] not in entry["matched"]:
            entry["matched"].append(info["matched"])

    call_sites = [
        {
            "line": e["line"],
            "kinds": sorted(e["kinds"]),
            "matched": e["matched"],
            "snippet": _snippet(lines, e["line"], e["end_line"]),
        }
        for e in by_line.values()
    ]

    result = {"file": path, "imports": tracker.imports, "call_sites": call_sites}
    if tracker.inherited:
        result["inherited_from_other_modules"] = tracker.inherited
    return result


@tool
def find_usages(library: str, repo_path: str, max_files: int = 500) -> dict:
    """Scan a local Python codebase for every import of and call site using a given library.

    Deterministic AST scan, not a text/regex search -- resolves import aliases
    (`import requests as req`, `from requests import get as rget`) so a call
    site is matched even under a renamed alias. Returns full call expressions
    (all arguments included) so a reviewer can see things like `verify=False`
    directly, not just the function name. Only files that actually import the
    library are included in the result.

    Args:
        library: The top-level import name of the library, e.g. "requests" or "urllib3" (not the PyPI package name if they differ).
        repo_path: Absolute or relative path to the root of the codebase to scan.
        max_files: Safety cap on how many .py files to walk. Defaults to 500.
    """
    if not os.path.isdir(repo_path):
        return {"status": "error", "content": [{"text": f"repo_path does not exist or is not a directory: {repo_path}"}]}

    # Collect the file list first so truncation is reported rather than silent.
    all_files: list[str] = []
    for root, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", "build", "dist", "site-packages")]
        all_files.extend(os.path.join(root, f) for f in filenames if f.endswith(".py"))

    total_py_files = len(all_files)
    truncated = total_py_files > max_files
    files = all_files[:max_files]

    errors = []
    sources: dict[str, str] = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                sources[path] = f.read()
        except OSError as e:
            errors.append({"file": path, "error": str(e)})

    # Parsing and walking every file twice is the dominant cost on a large tree
    # (measured: 67s over 4,000 files). A file can only import the library if its
    # source literally contains the library's name, so filtering on raw text
    # first is sound and skips the overwhelming majority of files.
    parsed_files: dict[str, tuple[str, ast.Module]] = {}

    def _parse(path: str) -> tuple[str, ast.Module] | None:
        if path in parsed_files:
            return parsed_files[path]
        try:
            tree = ast.parse(sources[path], filename=path)
        except SyntaxError as e:
            errors.append({"file": path, "error": f"SyntaxError: {e}"})
            return None
        parsed_files[path] = (sources[path], tree)
        return parsed_files[path]

    # Pass 1 -- what does each module export that traces back to the library?
    # Only files naming the library directly can export such a thing.
    exports: dict[str, dict[str, str]] = {}
    for path, source in sources.items():
        if library not in source:
            continue
        parsed = _parse(path)
        if parsed is None:
            continue
        exported = _collect_exports(parsed[1], library)
        if exported:
            exports[module_name_for(repo_path, path)] = exported

    # Pass 2 -- files naming the library, plus files importing from a module that
    # exports it (the cross-file case, where the library is never named locally).
    exporting_tails = {m.split(".")[-1] for m in exports}
    candidates = [
        path
        for path, source in sources.items()
        if library in source or any(tail in source for tail in exporting_tails)
    ]

    results = []
    for path in candidates:
        parsed = _parse(path)
        if parsed is None:
            continue
        outcome = _scan_file(
            path,
            library,
            exports=exports,
            module_name=module_name_for(repo_path, path),
            is_package=os.path.basename(path) == "__init__.py",
            parsed=parsed,
        )
        if outcome is None:
            continue
        if "error" in outcome:
            errors.append(outcome)
        else:
            results.append(outcome)

    results.sort(key=lambda r: r["file"])

    total_call_sites = sum(len(r["call_sites"]) for r in results)

    payload = {
        "library": library,
        "repo_path": repo_path,
        "files_scanned": len(files),
        "total_python_files": total_py_files,
        "files_using_library": len(results),
        "total_call_sites": total_call_sites,
        "results": results,
        "errors": errors,
    }
    if truncated:
        payload["truncated"] = (
            f"Only the first {max_files} of {total_py_files} Python files were scanned "
            f"(max_files cap). Usages in the remaining {total_py_files - max_files} files were "
            f"NOT checked -- do not treat this as a complete result. Raise max_files to scan all."
        )

    return {"status": "success", "content": [{"json": payload}]}
