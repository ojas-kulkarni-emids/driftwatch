"""The dependency-bump agent: one Strands Agent, four tools, and a system
prompt that tells it to reason about sufficiency rather than follow a fixed
order. The whole pitch depends on the tool SEQUENCE differing between a bump
with a clear changelog and one that needs more digging -- if every run calls
the same tools in the same order, this is a pipeline wearing an agent costume.

Model: Azure OpenAI. Strands' OpenAIModel always builds a plain
openai.AsyncOpenAI internally (verified by reading the installed source),
which won't correctly send Azure's required api-version on every call. So
we build the real openai.AsyncAzureOpenAI client ourselves and hand it in
via OpenAIModel's `client=` escape hatch instead of `client_args=`.
"""

import os

from openai import AsyncAzureOpenAI
from strands import Agent
from strands.models.openai import OpenAIModel

from .tools import find_usages, get_changelog, get_file_context, search_github_issues

SYSTEM_PROMPT = """You audit a single dependency version bump against a codebase's ACTUAL usage of \
that library -- not just version numbers or CVE databases. You have four tools. Decide their order \
and how many to call yourself, per bump -- do not follow the same fixed sequence every time.

Always start with find_usages to see what the codebase actually calls. Then decide:
- If get_changelog's release notes clearly explain what changed and you can directly compare that \
  to the call sites find_usages found, you can reach a verdict now. Do not call anything else just \
  to pad out the process.
- If a call site's snippet is ambiguous on its own (e.g. it wraps a helper, or the arguments aren't \
  fully visible), call get_file_context on that exact file/line before guessing.
- If the changelog is vague, placeholder text, or doesn't map cleanly onto what you see in the \
  call sites, call search_github_issues with specific terms describing the behavior in question -- \
  don't guess when you can check whether real users hit this.

For your final answer, produce a verdict PER CALL SITE find_usages returned, not one verdict for \
the whole bump. For each: state the file and line, the verdict, your reasoning, and a certainty \
level -- "clear from changelog", "clear after more context", "inferred from reported issues", or \
"unclear -- needs a human".

The verdict must be exactly one of these three labels, and it must not contradict your own \
reasoning. If you are about to write "affected" and then explain why it is fine, the verdict was \
AFFECTED when it should have been NOT AFFECTED:
- AFFECTED -- this change touches this call site.
- NOT AFFECTED -- this change does not touch this call site. Use this freely. Most call sites in \
  most bumps are genuinely untouched, and saying so is the useful signal.
- UNCLEAR -- you cannot tell from the evidence you gathered.

Three rules that matter more than they look:

1. "Removed" in a changelog does NOT reliably mean the caller's code will crash. Libraries very \
often keep a compatibility re-export at the old import path, or move a symbol while leaving an \
alias behind -- a changelog saying "removed X, it now lives in Y" frequently means "the documented \
home moved" rather than "your import raises ImportError". Distinguish "the documented API moved / \
is now discouraged" from "this will break at runtime", say which one you mean, and do not assert a \
hard runtime failure unless the changelog explicitly says the old path raises an error. Being wrong \
in the alarming direction destroys trust in every other verdict you give.

2. Do not cascade, and do not hedge on downstream lines. Give a verdict row ONLY to call sites \
where a symbol from the library itself is what changed. A line that merely uses an object created \
by an earlier line (e.g. `encryptor = cipher.encryptor()` after `cipher = Cipher(...)`, or \
`encryptor.update(...)` after that) is NOT its own finding: do not give it its own verdict row and \
do not mark it UNCLEAR. If those lines matter, add one short "downstream of line N" note under the \
root finding. UNCLEAR is for when you genuinely lack evidence about a changed symbol -- never for \
"this line depends on another line I already judged". A reader should see one row per real finding, \
not a row per line of the function.

3. Never say a bump is unconditionally "safe". Some call sites can be untouched while others are \
affected in the same bump. If nothing in the codebase uses the library at all, say that plainly and \
stop -- don't fabricate a hypothetical risk.

Be specific and short. Quote the actual code you're reasoning about, not a paraphrase of it.
"""


def build_agent() -> Agent:
    azure_client = AsyncAzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_BASE_URL"],
        api_version=os.getenv("AZURE_API_VERSION", "2024-08-01-preview"),
    )
    model = OpenAIModel(
        client=azure_client,
        model_id=os.environ["AZURE_DEPLOYMENT_NAME"],  # the Azure *deployment* name, not a model name like "gpt-4o"
        params={"temperature": 0.2, "max_tokens": 2000},
    )
    return Agent(
        model=model,
        tools=[find_usages, get_changelog, get_file_context, search_github_issues],
        system_prompt=SYSTEM_PROMPT,
    )


def analyze_bump(agent: Agent, repo_path: str, library: str, from_version: str, to_version: str, github_repo: str = "") -> str:
    """One bump, one pass: ask the agent to audit it against the real codebase."""
    prompt = (
        f"Audit this dependency bump against the codebase at {repo_path!r}: "
        f"library = {library!r}, from_version = {from_version!r}, to_version = {to_version!r}."
    )
    if github_repo:
        prompt += f" Use github_repo={github_repo!r} for get_changelog/search_github_issues."
    return str(agent(prompt))
