# CLAUDE.md

Project instructions for Claude Code working in this repo.

## What this is

`driftwatch` audits a single dependency version bump against a codebase's **actual usage** of
that library, and reports per call site whether the bump touches it. Built on the Strands
Agents SDK. See [README.md](README.md) for the product, [ROADMAP.md](ROADMAP.md) for open work.

## Commands

```bash
source .venv/Scripts/activate        # Windows Git Bash; .venv/bin/activate elsewhere
pip install -r requirements.txt
uvicorn server:app --port 8000       # UI at http://127.0.0.1:8000
python -m tests.test_tools           # manual checks against live data
```

**Restarting the server on Windows:** `pkill -f uvicorn` does **not** work — the old process
keeps port 8000 and the new one silently fails to bind, so you end up testing stale code. Kill
by PID instead:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**`curl` in Git Bash on Windows** may fail TLS with `CRYPT_E_NO_REVOCATION_CHECK`. Use
PowerShell's `Invoke-RestMethod` or Python's `requests` instead.

## Design invariants — do not break without discussion

1. **The repo never enters the model's context.** `find_usages` scans locally and deterministically;
   only matched call-site snippets reach the LLM. This is what makes cost independent of repo
   size. Never add a tool that hands the model whole files or lets it browse the codebase.
2. **No hardcoded tool sequence.** The model chooses which tools to call and when to stop.
   Different bumps producing different sequences is the intended behaviour, not a bug to fix.
3. **Verdicts must not over-claim.** "Removed" in a changelog often means the documented home
   moved, not that the caller crashes. The verdict rules in `agent/agent.py`'s system prompt
   encode this. Being wrong in the alarming direction destroys trust in every verdict.

## Verify against reality, don't assume

This codebase was built by checking claims empirically, and several assumptions turned out
wrong (see BUILDLOG.md). Before relying on a library's documented behaviour, test it:
`pip install pkg==version` and check the actual import. Before relying on an API's shape, call
it. Changelogs state removals that never happened.

## Build log — update after every build

**After completing any meaningful change, prepend an entry to [BUILDLOG.md](BUILDLOG.md).**
Newest entries go at the top, directly under the `## Entries` heading.

A "meaningful change" is: a new feature or tool, a bug fix, a behaviour change, a dependency
change, or a correction to something previously believed true. Not: typo fixes, formatting,
or comment rewording.

Use this format:

```markdown
### YYYY-MM-DD — Short title

**What changed.** One or two sentences.

**Why.** The problem it solves, or the reason for the change.

**Verification.** How you know it works — what you actually ran or observed. Write "unverified"
if you didn't check; never imply verification you didn't do.

**Files.** `path/one.py`, `path/two.py`
```

Two rules for entries:

- **Record corrections, including your own.** If something previously assumed or stated turns
  out wrong, log it explicitly. The corrections in this log have been more useful than the
  feature entries.
- **Be honest about verification.** "Verified against the installed library" and "looks right"
  are different claims. Say which one it is.
