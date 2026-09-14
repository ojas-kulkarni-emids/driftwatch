# Build log

Chronological record of what was built, why, and how it was verified. Newest first.

Format and conventions: see [CLAUDE.md](CLAUDE.md).

## Entries

### 2026-09-14 — Dropped the demo-handoff slide from the deck

**What changed.** Removed the fifth slide ("Demo handoff") from `docs/deck.html`, then stripped the
CSS it left behind. The deck is now four slides and ends on the architecture slide. 593 → 525 lines.

Dead code removed: the slide's rule block (`.demo-grid`, `.demo-script`, `.bar`, `.demo-body`,
`.watch-list`, `.watch`, `.handoff`), three orphan rules that were already unreferenced before this
change (`h1 .dim`, `.mono`, `.pill.u`), and three unused custom properties — `--surface-3` and
`--ok`, plus `--unclear`, which only `.pill.u` consumed.

**Why.** Requested — the handoff slide restated in text what the live walkthrough shows anyway.

**Correction.** The entry below describes a five-slide deck. That is no longer accurate.

**Verification.** Dead selectors found by diffing classes declared in `<style>` against classes
referenced in the markup and JS, not by eyeballing the removed slide — that's what surfaced the
three orphans predating this change. After the strip, the same diff reports nothing dead, braces
balance at 64/64, and no empty rules remain. All four slides re-rendered headlessly through Edge at
1280×720; the architecture slide's PNG is byte-identical (same MD5) to the pre-cleanup render, so
the CSS removal changed no pixels. Navigation needed no change — it derives entirely from
`slides.length`, with no hardcoded count.

**Files.** `docs/deck.html`

---

### 2026-09-14 - Reworked demo intro deck

**What changed.** Replaced `docs/deck.html` with a cleaner five-slide intro deck: product context,
the dependency-review gap, per-call-site output, the local-scan/agent architecture, and a final
demo handoff slide.

**Why.** The demo video needs a short setup that lands directly in the browser walkthrough. The
new ending tells the presenter exactly what to show next instead of stopping on architecture.

**Verification.** Checked that `docs/deck.html` contains five slides and no non-ASCII rendering
artifacts. Did not browser-record the deck.

**Files.** `docs/deck.html`

---

### 2026-09-14 — Demo deck for the submission video

**What changed.** Added `docs/deck.html`, a self-contained four-slide deck (problem / who it's for /
why it matters / architecture) for screen-recording alongside the live demo. No build step, no
external assets. Reuses the app's exact palette from `frontend/index.html` so the slides and the
running product read as one thing on video. Fixed 1280×720 stage scaled to the viewport, so the
layout that gets rehearsed is the layout that records at any window size. Arrow/space/click
navigation, `F` for fullscreen, and `#n` deep links for jumping to a slide while rehearsing.

**Why.** The video needs framing slides around the live walkthrough, and the existing
`docs/architecture.png` could not serve as the architecture slide — see the correction below.

**Correction: the committed architecture diagram is out of date.** `docs/architecture.html` and the
`docs/architecture.png` rendered from it show **four** tools. Six have shipped since — they predate
`list_dependencies` and `post_verdict`. The README embeds that PNG, so the diagram in the repo
currently understates the project. The deck's architecture slide is a fresh inline SVG showing all
six, split into the three that touch local disk and the three that call the GitHub API.
`docs/architecture.html` / `.png` and the README embed are still stale and need the same treatment.

**Verification.** All four slides rendered headlessly through Edge at 1280×720 and inspected as
images, not just read as source. Two layout issues found that way and fixed: slides sat high with
dead space beneath them (fixed with `justify-content:center`, a no-op on the architecture slide
whose diagram is `flex:1`), and the AFFECTED/NOT AFFECTED pills were too low-chroma to survive video
compression (saturation raised). Every fact on the slides traced to the repo rather than written
from memory: fixture line numbers confirmed with `grep -n` (`crypto_utils.py:24` Blowfish,
`:30` AES), and the 4,212-file / 12.1s measurement taken from ROADMAP item 4. Not verified: how the
deck looks in a real recording, or in any browser other than Edge.

**Files.** `docs/deck.html`

---

### 2026-09-14 — All five roadmap items completed

**What changed.** PyPI → GitHub repo resolution with per-process caching and an import-name /
package-name alias map; a new `list_dependencies` tool plus a "Scan repo" UI mode backed by
`GET /api/dependencies`; two-pass cross-file instance tracking in `find_usages`; a new
`post_verdict` tool that dry-runs by default; and a scale measurement that produced a 5.5x
speedup. Six tools are now registered with the agent, up from four.

**Why.** The hardcoded eight-library map was the most visible fragility in the product — anyone
typing `fastapi` or `pandas` hit a wall. Cross-file tracking closed the gap on the shared-client
pattern that nearly every production codebase uses. `list_dependencies` moves the product from
"audit the one bump you already knew about" to "point it at a repo and see what drifted".

**Verification.** Every item verified against real data:
- Resolution: `fastapi`, `boto3`, `pandas`, `sqlalchemy`, `yaml`, `sklearn`, `rich`, `httpx`,
  `pytest` all resolve unaided. `bs4` correctly returns None (BeautifulSoup isn't on GitHub).
- Cross-file: `fixtures/crossfile_repo` — `service.py` and `pkg/nested.py` correctly attributed
  to urllib3 despite never naming it, including via a relative import.
- Scale: 4,212-file corpus. urllib3 67.4s → 12.1s, requests 14.1s → 3.8s, cryptography 11.6s
  (1,723 call sites). Results byte-identical before and after the optimisation.
- Full agent run against the cross-file repo produced correct per-call-site verdicts.
- Both original fixtures regression-clean throughout.

**Two bugs this work surfaced, both real:**
1. `get_changelog` fetched only the first page of GitHub Releases. For an actively-released
   library like FastAPI, the 100 most recent releases were all *newer* than the requested range,
   so it silently returned nothing. Now paginated with truncation reporting.
2. The first scale measurement took 67 seconds because both passes parsed and fully walked every
   file, including thousands never mentioning the library. A raw-text pre-filter before parsing is
   sound — an import statement must literally contain the module name — and cut it to 12s.

Also fixed: `max_files` truncated silently, presenting a partial scan as if complete. It now
reports how many files went unscanned.

**Files.** `agent/tools/changelog.py`, `agent/tools/dependencies.py` (new),
`agent/tools/verdict.py` (new), `agent/tools/usages.py`, `agent/tools/issues.py`,
`agent/tools/__init__.py`, `agent/agent.py`, `server.py`, `frontend/index.html`,
`fixtures/demo_repo/requirements.txt` (new), `fixtures/crossfile_repo/` (new),
`tests/test_tools.py`, `ROADMAP.md`

---

### 2026-09-11 — Project documentation

**What changed.** Wrote README.md (product, architecture, full setup instructions, known
limitations), ROADMAP.md (contributor guide with five open items), and an architecture diagram
rendered to `docs/architecture.png` from a hand-authored SVG source kept at
`docs/architecture.html`.

**Why.** Public repo, README, and architecture diagram are hard requirements for the hackathon
submission. The roadmap exists so contributors who weren't part of the initial build can pick
up open items without reverse-engineering the codebase.

**Verification.** Diagram rendered and visually inspected; two layout bugs found and fixed (a
dangling arrow on `get_file_context` pointing past its target, and callout text overflowing its
container). All file paths referenced in the README confirmed to exist.

**Files.** `README.md`, `ROADMAP.md`, `docs/architecture.html`, `docs/architecture.png`

---

### 2026-09-11 — Web UI with live reasoning trace

**What changed.** FastAPI server streaming agent events to the browser over Server-Sent Events,
plus a single-page front end showing the tool sequence as it happens alongside per-call-site
verdicts. Three preset scenario buttons. No build step, no external JS dependencies.

**Why.** The core claim is that the agent chooses a different tool sequence per bump. That was
only observable as terminal output, which is unusable for a demo and invisible to a judge.

**Verification.** Streaming event shapes captured from a real agent run before building against
them (`current_tool_use` for tool calls, `data` for text deltas, `message` for tool results).
End-to-end verified in a real browser via Playwright screenshots, not just by reading the
SSE stream.

**Files.** `server.py`, `frontend/index.html`, `requirements.txt`

---

### 2026-09-11 — Group call sites per source line

**What changed.** `find_usages` now returns one entry per source line with all matched symbols
collected, instead of one entry per resolved symbol.

**Why.** A single line often references several library symbols — `Cipher(algorithms.Blowfish(key),
modes.CBC(iv))` resolves to three — and the agent was faithfully producing three near-duplicate
verdict rows for what is one place in the code. Twelve rows for a six-line file.

**Verification.** Re-ran all three fixtures: each now returns 6 grouped call sites instead of
12/6/6 ungrouped, with symbols listed together (`Cipher, Blowfish, CBC`). Confirmed in the UI.

**Files.** `agent/tools/usages.py`

---

### 2026-09-11 — Verdict quality rules in the system prompt

**What changed.** Three rules added: verdicts must use one of three fixed labels and must not
contradict their own reasoning; "removed" in a changelog does not reliably mean a runtime crash;
don't cascade verdicts onto lines that merely consume an object created by an affected line.

**Why.** The agent was labelling call sites "Touches change" and then explaining why they were
fine, and was asserting `Blowfish` usage "will fail in the new version" — which is false (see the
correction entry below). AES was getting the same verdict as Blowfish despite being unrelated
to the change.

**Verification.** Re-ran cryptography and urllib3 scenarios. Blowfish now AFFECTED and AES
cleanly NOT AFFECTED; urllib3's `getheader` correctly AFFECTED with the other five call sites
NOT AFFECTED. The false "will fail" claim is gone. Cascade rule is only partly followed —
dependent lines still get their own rows, though with consistent verdicts.

**Files.** `agent/agent.py`

---

### 2026-09-11 — Changelog file fallback when a repo publishes no Releases

**What changed.** `get_changelog` now falls back to fetching the repo's own changelog file
(`CHANGELOG.rst`, `CHANGELOG.md`, and six other common paths) via the GitHub Contents API when
the Releases API returns nothing for the version range. The file is sliced to only the relevant
version sections using a deliberately conservative header detector.

**Why.** Publishing GitHub Releases is optional for maintainers. `pyca/cryptography` tags every
version but publishes **zero** Releases, so the tool had no evidence at all for it — the agent
correctly but uselessly answered "unclear" for every call site. A complete 131KB `CHANGELOG.rst`
was sitting in the repo root the whole time, containing the exact text needed.

**Verification.** cryptography 42.0.0→46.0.3 now returns 28 sections from `CHANGELOG.rst`, with
Blowfish text captured in the 43.0.0 and 46.0.0 sections. Regression-checked: urllib3 (20
releases) and requests (1 release) still use the Releases API as before. Header detection is
conservative on purpose — a line only counts as a version header if its first token parses as a
version AND it is a markdown heading, followed by an RST underline, or carries a date; otherwise
prose like "3.14 support added" becomes a false section boundary.

**Files.** `agent/tools/changelog.py`

---

### 2026-09-11 — Correction: urllib3 and cryptography deprecations that never happened

**What changed.** Nothing in code. Two previously-assumed facts were tested and found wrong.

**Why.** Both were about to be encoded into fixtures and verdict expectations.

**Verification.** Tested directly against installed libraries:
- **urllib3 2.2.1** — `HTTPResponse.getheader`/`getheaders` still exist (`hasattr` → `True`),
  despite the 2.0.0 changelog stating they would be removed in 2.1.0. 2.1.0's own release notes
  never mention them. Deprecated, not removed. An earlier claim that these were "removed in v2.0"
  was wrong.
- **cryptography 46.0.0** — `from cryptography.hazmat.primitives.ciphers.algorithms import
  Blowfish` still succeeds despite the changelog saying it was "removed from the cipher module".
  The returned class is `cryptography.hazmat.decrepit.ciphers.algorithms.Blowfish` — the old path
  is a compatibility re-export.

This is the origin of design invariant #3: a changelog saying "removed" frequently means the
documented home moved, not that callers crash.

**Files.** none

---

### 2026-09-10 — Strands agent wired to Azure OpenAI

**What changed.** Agent built with the four tools registered and a system prompt instructing it
to reason about evidence sufficiency rather than follow a fixed tool order. Model provider is
Azure OpenAI via a pre-built `openai.AsyncAzureOpenAI` client injected into Strands'
`OpenAIModel` through its `client=` parameter.

**Why.** No Anthropic key was available, and Strands is provider-agnostic so the hackathon's
SDK requirement is unaffected.

**Verification.** Read the installed `strands/models/openai.py` source rather than trusting the
docs: `OpenAIModel` always constructs a plain `openai.AsyncOpenAI` internally, which does not
send the `api-version` parameter Azure requires on every request. The documented
`client_args={"base_url": ...}` recipe for Azure is therefore unreliable. The `client=`
injection path sidesteps this entirely. Confirmed working end-to-end against a live deployment.

Note: `AZURE_DEPLOYMENT_NAME` must be the deployment name chosen in Azure, not the model name.

**Files.** `agent/agent.py`, `.env.example`, `tests/test_tools.py`

---

### 2026-09-10 — Fix: AST scanner missed usage through local variables

**What changed.** Added best-effort instance tracking to `find_usages`: when a variable is
assigned from a resolved library call, subsequent attribute and method access on that variable
resolves back to the library, transitively.

**Why.** The first version only matched the literal `library.foo()` spelling. It caught
`urllib3.PoolManager()` and `requests.Session()` — the two call sites that *don't* matter for a
bump — while missing `resp.getheader(...)` and `session.get(...)`, which were exactly the ones
that do. Nearly all real-world usage goes through an instance obtained from a factory call, so
this was close to a total miss on realistic code.

**Verification.** Re-ran against fixtures: `http = urllib3.PoolManager()` → `http.request(...)`
→ `resp.getheader(...)` now all resolve to urllib3, and `session.get(url, allow_redirects=True)`
resolves to requests.

Known limitation, documented in the module docstring: single-pass, order-dependent, no scope
awareness, no cross-file tracking. See ROADMAP item 3.

**Files.** `agent/tools/usages.py`

---

### 2026-09-10 — Four tools built

**What changed.** `find_usages` (stdlib `ast` scan for imports and call sites), `get_changelog`
(GitHub Releases API with semantic version-range filtering via `packaging`), `get_file_context`
(bounded line window around an already-identified location), `search_github_issues` (GitHub
issue/PR search sorted by comment count).

**Why.** The four evidence sources the agent needs. `find_usages` is the differentiator versus
Dependabot — it reads actual usage rather than comparing version numbers.

**Verification.** Each tested against real data, not mocks. Demo fixtures built around
verified-real breaking changes: urllib3 v1→v2's `getheader` deprecation and the requests
CVE-2023-32681 proxy-redirect fix, each paired with a deliberately unaffected sibling call site.

**Files.** `agent/tools/usages.py`, `changelog.py`, `context.py`, `issues.py`,
`fixtures/demo_repo/api_client.py`

---

### 2026-09-10 — Verification before scaffolding

**What changed.** No code. Confirmed the three assumptions the design rested on before building
on them.

**Why.** Changelog retrieval was identified up front as the fragile part.

**Verification.**
- **GitHub Releases API is usable** — structure and content confirmed against real repos.
- **Rate limits** — hit the unauthenticated wall live within two calls: 60 req/hr shared **per
  IP**, which on a shared network may already be exhausted. A PAT raises this to 5,000/hr, so a
  token is mandatory rather than optional. Confirmed against GitHub's documentation.
- **Demo libraries selected** on the basis of having real, documented breaking changes with
  genuinely usable changelogs, rather than picking convenient ones and hoping.

**Files.** none
