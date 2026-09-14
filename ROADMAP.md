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
| `find_usages` — AST scanner | `agent/tools/usages.py` | Two-pass, cross-file. Resolves import aliases, instance chains, and shared clients imported from sibling modules. Measured at 12.1s over 4,204 files. |
| `get_changelog` | `agent/tools/changelog.py` | Paginated Releases API, PyPI repo resolution, changelog-file fallback. Verified on `urllib3` (20), `requests` (1), `fastapi` (32), `cryptography` (0 releases → `CHANGELOG.rst`, 28 sections). |
| `get_file_context` | `agent/tools/context.py` | Verified. Trivial by design. |
| `search_github_issues` | `agent/tools/issues.py` | Verified live; returns real results. |
| `list_dependencies` | `agent/tools/dependencies.py` | Parses requirements.txt + pyproject.toml (PEP 621 & Poetry). Verified on the fixture's 6 deps including a range and a marker. |
| `post_verdict` | `agent/tools/verdict.py` | Dry run verified. Live posting needs a write-scoped token and is untested against a real PR. |
| Strands agent + Azure wiring | `agent/agent.py` | Six tools registered. Verified end-to-end on all scenarios plus the cross-file repo. Azure works via `client=` injection (see README). |
| Web UI + SSE reasoning trace | `server.py`, `frontend/index.html` | Verified in a real browser, including the dependency-scan table and its handoff to an audit. |

**Ground truth used during development** (re-verify rather than trusting these if a change
depends on them):
- `urllib3` 2.2.1 — `HTTPResponse.getheader`/`getheaders` still exist despite 2.0.0's changelog
  saying they'd be removed in 2.1.0. Deprecated, not removed.
- `cryptography` 46.0.0 — `Blowfish` still importable from the old path; the returned class is
  `cryptography.hazmat.decrepit.ciphers.algorithms.Blowfish`.
- `pyca/cryptography` publishes **zero** GitHub Releases. `urllib3` and `requests` do.
- None of the three demo repos have GitHub Discussions enabled.

---

## Completed — 2026-09-14

All five original roadmap items are implemented and verified. Details below, including the
measurements and the two bugs the work surfaced.

### 1. PyPI → GitHub repo resolution ✅

`resolve_repo()` in `agent/tools/changelog.py` now tries, in order: an explicit `github_repo`
override, the built-in `_KNOWN_REPOS` fast path, then PyPI's JSON API — reading `project_urls`
(whose keys maintainers name freely, so several are checked) and falling back to `home_page`.
Results are cached per process, failures included. `_IMPORT_TO_PYPI` handles the import-name /
package-name mismatches (`yaml` → `PyYAML`, `sklearn` → `scikit-learn`, and others).

**Verified:** `fastapi`, `boto3`, `pandas`, `sqlalchemy`, `yaml`, `sklearn`, `rich`, `httpx`,
`pytest` all resolve with no manual input. `bs4` correctly returns `None` — BeautifulSoup genuinely
isn't hosted on GitHub. Unresolvable packages return a clear error naming the import-vs-package
distinction, not a crash.

**Bug this surfaced:** `fastapi` resolved correctly but returned *zero* releases. The Releases API
was only being fetched one page deep, and FastAPI has released more than 100 times since 0.115.0 —
so the requested range was never reached. Now paginated (up to 10 pages), stopping as soon as a page
reaches at-or-below `from_version`, and reporting `truncated` if the cap is hit. `fastapi
0.100.0 → 0.115.0` now returns 32 releases.

### 2. Auto-discover dependencies from the repo ✅

New tool `list_dependencies` in `agent/tools/dependencies.py`, plus a **Scan repo** button and
dependency table in the UI backed by `GET /api/dependencies`. Parses `requirements.txt` (following
`-r` includes and backslash continuations) and `pyproject.toml` (PEP 621 and Poetry layouts), using
`packaging.requirements.Requirement` rather than hand-rolled parsing so extras and environment
markers are handled. PyPI latest-version lookups run in a thread pool — serial requests made a
30-dependency repo unusably slow in the UI.

A dependency without an exact `==` pin has no single current version; `current_version` is null and
`has_update` is null for those, and the UI shows "not pinned" with the audit button disabled rather
than guessing.

**Verified:** `fixtures/demo_repo/requirements.txt` yields all 6 dependencies with correct pins,
latest versions, and drift status. Rows with a well-defined bump hand straight off to the auditor.

### 3. Cross-file instance tracking ✅

`find_usages` is now a two-pass scan. Pass 1 records what each module exports that traces back to
the library (module-level assignments and re-exported imports only — a name bound inside a function
can't be imported elsewhere). Pass 2 resolves `from clients import http` against that map and seeds
the importing file's `instance_vars`. Relative imports (`from ..clients import http`) resolve
against the importing file's own package path, with `__init__.py` handled as the package itself.
Files that inherit lineage this way report it under `inherited_from_other_modules`.

**Verified** against `fixtures/crossfile_repo`: `service.py` and `pkg/nested.py` both have their
`http.request(...)` / `resp.getheader(...)` calls correctly attributed to urllib3 despite never
naming it, including through a relative import. Original fixture results unchanged.

### 4. Test against a genuinely large repo ✅

Measured against a 4,212-file Python corpus (the project venv's `site-packages`).

**Bug this surfaced:** the first measurement took **67 seconds** for `urllib3`. Both passes were
parsing and fully walking every file, including thousands that never mention the library. Since an
import statement must literally contain the module name, filtering on raw source text before
parsing is sound and skips almost all of them.

| Library | Before | After | Files using | Call sites |
|---|---|---|---|---|
| urllib3 | 67.4s | **12.1s** | 15 | 116 |
| requests | 14.1s | **3.8s** | 5 | 6 |
| cryptography | — | **11.6s** | 68 | 1,723 |

Results are byte-identical before and after the optimisation. The `max_files` cap also no longer
truncates silently — it reports how many files went unscanned and warns against treating the result
as complete.

### 5. `post_verdict` — comment the analysis on a PR ✅

New tool in `agent/tools/verdict.py`. Formats the audit as a PR comment and **defaults to a dry
run**, returning the markdown without posting. Only `publish=true` actually posts, via
`POST /repos/{owner}/{repo}/issues/{number}/comments` (GitHub exposes PR comments under `/issues/`,
not `/pulls/`).

Every other tool works with a no-scope token, so this one is deliberately opt-in and does not widen
the README's stated token requirement. A 401/403/404 returns a message explaining the token most
likely lacks write access, without claiming to know which of the three causes it was — GitHub
returns 404 for both "no such repo" and "no permission".

The system prompt instructs the agent never to pass `publish=true` unless the user explicitly asked
for the verdict to be posted.

---

## Still open

Nothing from the original list. Genuine remaining gaps, in rough value order:

- **No automated test suite.** `tests/test_tools.py` prints for a human to read, with one assertion
  added for cross-file tracking. `find_usages` is pure and deterministic and would be trivial to
  unit-test properly.
- **No scope awareness in variable tracking.** Still a flat per-file dict. Two functions reusing a
  variable name for different things can collide. Cross-file tracking raises the stakes on this.
- **Dependent call sites still get their own verdict rows.** The system prompt asks the agent to
  fold `encryptor.update(...)` into the root finding it descends from; it complies inconsistently.
- **Verdicts are LLM judgement, not verification.** Nothing executes the code against both library
  versions. The rigorous version — install both in isolated environments and diff actual behaviour —
  is a much larger piece of work but would turn judgement into evidence.
- **Python only.** `ast` is the entire cost advantage; a polyglot analyser is a different project.

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
