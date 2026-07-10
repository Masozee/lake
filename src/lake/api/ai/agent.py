"""The AI exploration loop.

Runs an Anthropic tool-use conversation where the only tools are the read-only
ones in `tools.py`. The agent can look at tables, profile columns, and run SELECT
queries; it cannot change anything, because nothing it can call is able to.

Emits events as it goes (thinking, tool call, tool result, answer) so the
frontend can show the exploration unfolding rather than a spinner.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lake.api.ai.tools import TOOL_DEFINITIONS, dispatch
from lake.api.catalog import schema_digest
from lake.core.logging import get_logger
from lake.settings import get_settings

log = get_logger(__name__)

MODEL = "claude-fable-5"
MAX_TOKENS = 4096
#: A hard cap on tool round-trips. A well-behaved agent finishes in two or three;
#: this stops a pathological loop from running the bill up.
MAX_TURNS = 8

SYSTEM_PROMPT = """You are a data analyst exploring a read-only data lake.

You have tools to list tables, describe and profile them, and run DuckDB SELECT
queries. Use them to answer the user's question with real data — do not guess.

Workflow: list the tables, describe the relevant one, profile columns when you
need to know valid filter values, then write aggregating SQL. Prefer GROUP BY and
aggregates over pulling raw rows; results are capped.

You cannot modify anything. There is no tool to insert, update, or delete, and the
database is physically read-only. If asked to change data, explain that this is a
read-only interface.

When you have the answer, state it plainly and show the key numbers. If a query is
rejected, read the error and try a different approach.

Current schema:
{schema}
"""


class AgentUnavailable(RuntimeError):
    """No API key configured. The rest of the API works without AI."""


def _client():
    import anthropic

    settings = get_settings()
    key = getattr(settings, "anthropic_api_key", None)
    if not key:
        raise AgentUnavailable(
            "AI exploration needs an Anthropic API key. Set LAKE_ANTHROPIC_API_KEY."
        )
    return anthropic.Anthropic(api_key=key)


def _system_prompt() -> str:
    try:
        schema = schema_digest()
    except Exception as exc:  # replica not built yet, etc.
        schema = f"(schema unavailable: {exc})"
    return SYSTEM_PROMPT.format(schema=schema)


def explore(question: str) -> Iterator[dict[str, Any]]:
    """Drive the tool-use loop, yielding events.

    Event shapes (each a dict with a `type`):
        {"type": "text",        "text": "..."}            model prose
        {"type": "tool_call",   "tool": "...", "input": {...}}
        {"type": "tool_result", "tool": "...", "result": {...}}
        {"type": "done"}
        {"type": "error",       "error": "..."}
    """
    try:
        client = _client()
    except AgentUnavailable as exc:
        yield {"type": "error", "error": str(exc)}
        return

    system = _system_prompt()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for _turn in range(MAX_TURNS):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        except Exception as exc:
            log.exception("agent.api_error")
            yield {"type": "error", "error": f"model call failed: {type(exc).__name__}"}
            return

        assistant_content: list[dict[str, Any]] = []
        tool_uses: list[Any] = []

        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                yield {"type": "text", "text": block.text}
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
                tool_uses.append(block)
                yield {"type": "tool_call", "tool": block.name, "input": block.input}

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason != "tool_use":
            yield {"type": "done"}
            return

        # Run every requested tool and feed the results back.
        tool_results = []
        for tool_use in tool_uses:
            result = dispatch(tool_use.name, tool_use.input or {})
            yield {"type": "tool_result", "tool": tool_use.name, "result": result}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": _stringify(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "error": f"stopped after {MAX_TURNS} tool turns without an answer"}


def _stringify(result: dict[str, Any]) -> str:
    import json

    text = json.dumps(result, default=str, separators=(",", ":"))
    # Keep a single tool result from flooding the context window. The model has
    # already been told results are capped; this is a hard backstop.
    if len(text) > 16_000:
        text = text[:16_000] + '…","note":"result truncated"}'
    return text
