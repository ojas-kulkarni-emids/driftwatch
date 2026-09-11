"""get_file_context -- lets the agent pull more surrounding code when a
find_usages snippet alone is ambiguous, instead of the codebase being
front-loaded into the prompt. This is what makes the tool sequence
genuinely agentic rather than a fixed two-step pipeline.
"""

import os

from strands import tool


@tool
def get_file_context(file_path: str, line: int, context: int = 8) -> dict:
    """Read the lines surrounding a specific line in a file for more context.

    Use this when a find_usages call site's snippet isn't enough to judge
    whether a change actually applies -- e.g. the call spans a helper
    function whose definition is a few lines above.

    Args:
        file_path: Path to the file, exactly as returned by find_usages.
        line: The 1-indexed line number to center the context window on.
        context: How many lines to include above and below. Defaults to 8.
    """
    if not os.path.isfile(file_path):
        return {"status": "error", "content": [{"text": f"File not found: {file_path}"}]}

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"status": "error", "content": [{"text": f"Could not read {file_path}: {e}"}]}

    start = max(1, line - context)
    end = min(len(lines), line + context)
    numbered = [f"{n}: {lines[n - 1]}" for n in range(start, end + 1)]

    return {
        "status": "success",
        "content": [{"json": {"file_path": file_path, "start_line": start, "end_line": end, "code": "\n".join(numbered)}}],
    }
