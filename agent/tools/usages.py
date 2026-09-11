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


class _ImportTracker(ast.NodeVisitor):
    def __init__(self, library: str):
        self.library = library
        self.top_level_aliases: set[str] = set()  # bound to the module itself, e.g. `requests`, `req`
        self.imported_names: dict[str, str] = {}  # bound name -> qualified name, e.g. `get` -> "requests.get"
        self.instance_vars: dict[str, str] = {}  # local var -> best-effort label, e.g. `http` -> "urllib3.PoolManager"
        self.imports: list[dict] = []
        self._seen: dict[tuple[int, int], dict] = {}

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


def _scan_file(path: str, library: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as e:
        return {"file": path, "error": str(e)}

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return {"file": path, "error": f"SyntaxError: {e}"}

    tracker = _ImportTracker(library)
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

    return {"file": path, "imports": tracker.imports, "call_sites": call_sites}


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

    results = []
    errors = []
    files_scanned = 0

    for root, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".venv", "venv", "node_modules")]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            if files_scanned >= max_files:
                break
            files_scanned += 1
            full_path = os.path.join(root, filename)
            outcome = _scan_file(full_path, library)
            if outcome is None:
                continue
            if "error" in outcome:
                errors.append(outcome)
            else:
                results.append(outcome)

    total_call_sites = sum(len(r["call_sites"]) for r in results)

    return {
        "status": "success",
        "content": [
            {
                "json": {
                    "library": library,
                    "repo_path": repo_path,
                    "files_scanned": files_scanned,
                    "files_using_library": len(results),
                    "total_call_sites": total_call_sites,
                    "results": results,
                    "errors": errors,
                }
            }
        ],
    }
