"""AI exploration route. Server-sent events so the frontend shows the agent work.

The agent is read-only by construction (see ai/tools.py and engine.py). This
route adds nothing to that guarantee and takes nothing away — it just relays the
event stream to the browser.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from lake.api.ai.agent import explore
from lake.api.schemas import AskRequest

router = APIRouter()


@router.post("/ask")
def post_ask(request: AskRequest) -> StreamingResponse:
    """Ask the AI a question about the data. Streams SSE events.

    Each event is `data: <json>\\n\\n`, where the JSON has a `type`:
    `text`, `tool_call`, `tool_result`, `done`, or `error`.
    """

    def sse() -> Iterator[bytes]:
        try:
            for event in explore(request.question):
                yield f"data: {json.dumps(event, default=str)}\n\n".encode()
        except Exception as exc:  # a generator failure must still close the stream
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n".encode()
        yield b'data: {"type": "stream_end"}\n\n'

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
