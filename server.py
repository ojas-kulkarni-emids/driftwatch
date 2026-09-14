"""Local web server for the dependency-bump agent.

Streams the agent's live reasoning trace to the browser over Server-Sent
Events, so you can watch which tools it chooses and in what order -- the
tool sequence differs per bump, and that's the point of the whole design,
so it needs to be visible rather than buried in terminal output.

Run:  uvicorn server:app --reload --port 8000
"""

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402

from agent.agent import build_agent  # noqa: E402
from agent.tools.dependencies import list_dependencies  # noqa: E402

app = FastAPI(title="driftwatch")
FRONTEND = Path(__file__).parent / "frontend" / "index.html"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _summarize_tool_result(tool_result: dict) -> str:
    """One-line human summary of a tool result, for the trace panel."""
    for block in tool_result.get("content") or []:
        payload = block.get("json")
        if not isinstance(payload, dict):
            continue
        if "total_call_sites" in payload:
            return (
                f"{payload['total_call_sites']} call sites across "
                f"{payload['files_using_library']} file(s), {payload['files_scanned']} scanned"
            )
        if "source" in payload:
            if payload["source"] == "github_releases":
                return f"{payload.get('release_count', 0)} GitHub release(s)"
            if payload["source"] == "changelog_file":
                return f"{payload.get('section_count', 0)} section(s) from {payload.get('changelog_file')}"
            return "no changelog evidence found"
        if "total_count" in payload:
            return f"{payload['total_count']} matching issue(s)/PR(s)"
        if "code" in payload:
            return f"lines {payload.get('start_line')}-{payload.get('end_line')} of {Path(payload.get('file_path', '')).name}"
    for block in tool_result.get("content") or []:
        if "text" in block:
            return str(block["text"])[:180]
    return "done"


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(FRONTEND.read_text(encoding="utf-8"))


@app.get("/api/dependencies")
def dependencies(repo_path: str) -> dict:
    """Declared dependencies and their drift. Called directly rather than through
    the agent -- this is a deterministic lookup with no reasoning to stream."""
    result = list_dependencies(repo_path)
    if result.get("status") == "error":
        return {"error": result["content"][0].get("text", "unknown error")}
    return result["content"][0]["json"]


@app.get("/api/analyze")
async def analyze(
    repo_path: str,
    library: str,
    from_version: str,
    to_version: str,
    github_repo: str = "",
) -> StreamingResponse:
    async def generate():
        try:
            agent = build_agent()
        except KeyError as e:
            yield _sse("error", {"message": f"Missing environment variable: {e}. Check your .env file."})
            return
        except Exception as e:
            yield _sse("error", {"message": f"Could not start agent: {e}"})
            return

        prompt = (
            f"Audit this dependency bump against the codebase at {repo_path!r}: "
            f"library = {library!r}, from_version = {from_version!r}, to_version = {to_version!r}."
        )
        if github_repo:
            prompt += f" Use github_repo={github_repo!r} for get_changelog/search_github_issues."

        announced: set[str] = set()
        try:
            async for event in agent.stream_async(prompt):
                if not isinstance(event, dict):
                    continue

                tool_use = event.get("current_tool_use")
                if isinstance(tool_use, dict):
                    tool_id = tool_use.get("toolUseId")
                    name = tool_use.get("name")
                    if tool_id and name and tool_id not in announced:
                        announced.add(tool_id)
                        yield _sse("tool_start", {"id": tool_id, "name": name})

                if isinstance(event.get("data"), str):
                    yield _sse("text", {"chunk": event["data"]})

                message = event.get("message")
                if isinstance(message, dict):
                    for block in message.get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        # A completed tool call: its arguments are visible here.
                        if "toolUse" in block:
                            tu = block["toolUse"]
                            yield _sse("tool_args", {"id": tu.get("toolUseId"), "input": tu.get("input")})
                        if "toolResult" in block:
                            tr = block["toolResult"]
                            yield _sse(
                                "tool_done",
                                {
                                    "id": tr.get("toolUseId"),
                                    "status": tr.get("status", "success"),
                                    "summary": _summarize_tool_result(tr),
                                },
                            )

            yield _sse("done", {})
        except Exception as e:  # surface the real failure in the UI rather than a dead spinner
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
