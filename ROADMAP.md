# Roadmap & contributor guide

This document is for someone picking up work on `driftwatch` who wasn't involved in the initial
build. It covers what exists and is verified, what doesn't exist yet, and enough implementation
detail on each open item to start without reverse-engineering the codebase first.

Read [README.md](README.md) first for setup. This file assumes you can already run the server.

---

## Design invariants — please don't break these

Three decisions the whole design rests on. If a change violates one of these, it needs a
conversation first, not a PR.

1. **The repo never enters the model's context.** `find_usages` does a complete, deterministic
   AST scan locally; only the matched call-site snippets go to the LLM. This is what makes cost
   independent of repo size. Do not add a tool that hands the model whole files, directory
   listings, or "search the codebase" capability.

2. **No hardcoded tool sequence.** The model chooses which tools to call and when to stop. It is
   valid and desirable for different bumps to produce different tool sequences. Do not "fix"
   variability by forcing an order in code.

3. **Verdicts must not over-claim.** A changelog saying "removed" frequently means the documented
   home moved, not that the caller crashes — verified concretely: `cryptography` 46.0.0 says
   `Blowfish` was "removed from the cipher module", but `from cryptography.hazmat.primitives.
   ciphers.algorithms import Blowfish` still succeeds via a compatibility re-export. The verdict
   rules in `agent/agent.py`'s system prompt encode this. Being wrong in the alarming direction
   destroys trust in every other verdict.

---

## What's built and verified

| Component | File | Verification status |
|---|---|---|
| `find_usages` — AST scanner | `agent/tools/usages.py` | Verified on 3 fixture files. Resolves import aliases and instance chains (`http = urllib3.PoolManager()` → `http.request()` → `resp.getheader()`). Groups results one-per-line. |
| `get_changelog` | `agent/tools/changelog.py` | Verified live against `urllib3` (20 releases in range), `requests` (1), `cryptography` (0 releases → falls back to `CHANGELOG.rst`, 28 sections). |
| `get_file_context` | `agent/tools/context.py` | Verified. Trivial by design. |
| `search_github_issues` | `agent/tools/issues.py` | Verified live; returns real results. |
| Strands agent + Azure wiring | `agent/agent.py` | Verified end-to-end on all three scenarios. Azure works via `client=` injection (see README). |
| Web UI + SSE reasoning trace | `server.py`, `frontend/index.html` | Verified end-to-end in a real browser. |

**Ground truth used during development** (re-verify rather than trusting these if a change
depends on them):
- `urllib3` 2.2.1 — `HTTPResponse.getheader`/`getheaders` still exist despite 2.0.0's changelog
  saying they'd be removed in 2.1.0. Deprecated, not removed.
- `cryptography` 46.0.0 — `Blowfish` still importable from the old path; the returned class is
  `cryptography.hazmat.decrepit.ciphers.algorithms.Blowfish`.
- `pyca/cryptography` publishes **zero** GitHub Releases. `urllib3` and `requests` do.
- None of the three demo repos have GitHub Discussions enabled.

---

## Open items

Ordered by value. Each is independent — no item blocks another.

### 1. PyPI → GitHub repo resolution

**Effort:** ~30 min · **Value:** high · **Files:** `agent/tools/changelog.py`

**Problem.** `_KNOWN_REPOS` is a hardcoded dict of eight libraries. Anything else fails with
"No known GitHub repo" unless the user manually supplies `owner/name`. If anyone tries
`boto3`, `fastapi`, `pandas`, or `sqlalchemy` in the UI, they hit a wall. This is the most
visible fragility in the product right now.

**Approach.** Query `https://pypi.org/pypi/{package}/json` (no auth, no rate limit worth
worrying about). Look in `info.project_urls` — a dict with inconsistent keys across projects
(`Source`, `Source Code`, `Repository`, `Homepage`, `Code`, `GitHub`). Take the first value
containing `github.com` and parse `owner/repo` out of the path. Fall back to `info.home_page`.
Keep `_KNOWN_REPOS` as a fast path and as an override for projects PyPI gets wrong.

**Watch out for:** the import name and the PyPI package name differ more often than you'd
expect — `import yaml` is `PyYAML`, `import cv2` is `opencv-python`, `import sklearn` is
`scikit-learn`. `find_usages` takes the *import* name. Either try both, or add a small
alias map alongside `_KNOWN_REPOS`.

**Done when:** `get_changelog("fastapi", "0.100.0", "0.115.0")` works with no `github_repo`
argument, and an unresolvable package returns a clear error rather than a crash.

---

### 2. Auto-discover dependencies from the repo

**Effort:** ~1-2 hrs · **Value:** high · **Files:** new tool in `agent/tools/`, plus UI

**Problem.** Today the user must already know the library *and* both version numbers and type
them in. That's a reasonable fit for the real trigger (a Dependabot PR title tells you all
three), but it makes the demo weaker than the pitch: "point it at your repo and see what's at
risk" is a much stronger story than "tell me about this one bump I already knew about."

**Approach.** New tool `list_dependencies(repo_path)` returning each dependency with its
currently-pinned version:
- `requirements.txt` — line-based. Handle `==`, `>=`, `~=`, `!=`, extras (`package[extra]`),
  environment markers (`; python_version < "3.11"`), comments, blank lines, and `-r other.txt`
  includes.
- `pyproject.toml` — use `tomllib` (stdlib on 3.11+). Two layouts: PEP 621
  `[project].dependencies` (list of PEP 508 strings) and `[tool.poetry.dependencies]` (a table).
- Optionally `Pipfile`, `poetry.lock`, `requirements/*.txt`.

Then, to know what a bump would even *be*, query PyPI's JSON API for `info.version` (latest).
That gives you `current → latest` candidate bumps without needing a Dependabot PR to exist.

**UI change:** add a "scan repo" mode that lists discovered dependencies with their candidate
bumps and lets the user audit one with a click, instead of typing four fields.

**Done when:** pointing at a repo with a `requirements.txt` lists its dependencies and their
current versions, and each row can be audited without manual typing.

---

### 3. Cross-file instance tracking

**Effort:** ~2-3 hrs · **Value:** medium-high · **Files:** `agent/tools/usages.py`

**Problem.** `_scan_file` analyses each file independently. The extremely common real-world
pattern of a shared client in a utils module is therefore invisible:

```python
# clients.py
http = urllib3.PoolManager()

# service.py
from clients import http
resp = http.request("GET", url)   # ← not detected as urllib3 usage
```

This directly undercuts the "works on real repos" claim, since almost every production codebase
does this.

**Approach.** Two passes:
1. **Pass 1** — scan every file as today, but also export a map of module-level names to their
   library lineage: `{"clients": {"http": "urllib3.PoolManager"}}`. You need a file-path →
   module-name mapping (`<root>/pkg/clients.py` → `pkg.clients`), which means handling
   `__init__.py` packages and the repo root.
2. **Pass 2** — re-scan, and when a file does `from clients import http`, resolve `clients.http`
   against the pass-1 map and seed that file's `instance_vars` with the inherited lineage.

Handle relative imports (`from .clients import http`, `from ..pkg import http`) by resolving
against the importing file's own package path.

**Watch out for:** `_ImportTracker.instance_vars` is currently a flat per-file dict with no
scope awareness (documented in the module docstring). Cross-file tracking makes collisions more
likely, not less. If you're touching this code anyway, consider whether per-function scoping is
worth adding at the same time.

**Done when:** a two-file fixture using the pattern above reports the `http.request()` call in
`service.py` as a `urllib3` usage.

---

### 4. Test against a genuinely large repo

**Effort:** ~1 hr · **Value:** medium · **Files:** none (investigation)

**Problem.** Correctness is verified only on 1-2 file fixtures. The headline cost argument
("a 500-file repo costs about the same as a 5-file one") is sound in principle — `ast.parse` is
linear and cheap — but has never been measured. Right now that claim is reasoning, not evidence.

**Approach.** Clone something substantial that depends on a mapped library — `sentry`, `airflow`,
`ansible`, or the `requests` test suite itself. Then measure:
- Wall-clock time for `find_usages` across the whole tree.
- How many call sites come back, and whether the resulting prompt is a sane size.
- Whether `max_files=500` (the safety cap in `find_usages`) is hit, and what should happen if so
  — right now it silently stops, which is arguably a bug. It should at minimum report truncation.
- Spot-check for false negatives against `grep -rn "libraryname"`.

**Done when:** there's a documented measurement in this file, and the `max_files` cap either
reports truncation or is replaced with something better.

---

### 5. `post_verdict` — comment the analysis on a PR

**Effort:** ~45 min · **Value:** medium · **Files:** new `agent/tools/verdict.py`

**Problem.** The agent reports; it doesn't act. A tool that writes its findings back to the PR
that triggered it closes the loop, and "the agent took an action" reads considerably better than
"the agent produced text."

**Approach.** New tool `post_verdict(github_repo, pr_number, body)` doing
`POST /repos/{owner}/{repo}/issues/{number}/comments`. Note GitHub treats PRs as issues for the
comments endpoint — use `/issues/`, not `/pulls/`.

**Important:** this needs a token with **write** access (`repo` scope for private,
`public_repo` for public) — unlike every other tool here, which needs no scopes at all. Don't
silently widen the token requirement in the README's setup instructions; make this tool
optional and degrade cleanly when the token can't write. Consider a dry-run mode that returns
the formatted comment without posting, so the demo doesn't depend on live write access.

**Done when:** a verdict can be posted to a PR on a repo you control, and the tool fails with a
clear message (not a stack trace) when the token lacks permission.

---

## Deliberately not doing

- **GitHub Discussions search.** Would need the GraphQL API (`search(type: DISCUSSION)`) since
  the REST search endpoint structurally cannot return Discussions. GraphQL was confirmed working
  with a standard PAT, so it's buildable in ~20 min — but none of the three demo libraries have
  Discussions enabled, so it would be a registered tool that can never fire. Revisit if the
  library coverage broadens (item 1).
- **Deployment.** Not required by the hackathon rules; a public repo plus the demo video
  satisfies the submission. Revisit only if a hosted demo becomes worth the time.
- **Auto-fixing code.** Explicitly out of scope. Flagging only.
- **Non-Python languages.** `ast` is the entire cost advantage. A polyglot analyser is a
  different project.

---

## Testing

```bash
python -m tests.test_tools
```

Degrades gracefully by credential availability — AST tools need nothing, GitHub tools need
`GITHUB_TOKEN`, the full agent loop needs the Azure variables too.

There is **no automated test suite** — `tests/test_tools.py` prints results for a human to read.
If you're adding a non-trivial feature, adding real assertions (especially for `find_usages`,
which is pure and deterministic and therefore trivially unit-testable) would be a genuine
improvement.

## Gotchas found the hard way

- **Restarting the server:** `pkill -f uvicorn` does not work reliably on Windows/Git Bash. The
  old process keeps port 8000 and the new one silently fails to bind, so you end up testing
  stale code. Kill by PID:
  `Get-NetTCPConnection -LocalPort 8000 -State Listen | Stop-Process -Id {$_.OwningProcess} -Force`
- **`curl` in Git Bash on Windows** may fail TLS with `CRYPT_E_NO_REVOCATION_CHECK`. Use
  PowerShell's `Invoke-RestMethod` or Python's `requests` instead.
- **Unauthenticated GitHub API** is 60 req/hr *per IP, shared*. On a corporate network it may
  already be exhausted before you make a single call. Always set `GITHUB_TOKEN`.
