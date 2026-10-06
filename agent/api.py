"""HTTP front end for the agent loop. Holds the API key; never published to the host.

The browser-facing `ui` container proxies /api/* here over the Compose network.
Progress is streamed as Server-Sent Events so the UI can show each loop step live.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from typing import Any, Iterator, Literal

import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from loop import AgentError, run_agent
from main import SYSTEM_PROMPT
from tools import build_registry


app = FastAPI()


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10_000)


class ChatRequest(BaseModel):
    # The browser keeps the conversation and sends it with every request.
    # Only plain text is carried between turns; tool calls from earlier turns are not replayed.
    messages: list[Turn] = Field(min_length=1, max_length=50)


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=3)


@app.get("/health")
def health():
    return {"status": "ok"}


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


@app.post("/chat")
def chat(req: ChatRequest):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return JSONResponse(status_code=500, content={"error": "ANTHROPIC_API_KEY is not set on the server"})
    if req.messages[-1].role != "user":
        return JSONResponse(status_code=400, content={"error": "last message must be from the user"})

    *history, last = [m.model_dump() for m in req.messages]
    events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()

    def work() -> None:
        # The loop is synchronous, so it runs in a thread and pushes events to the queue.
        try:
            result = run_agent(
                get_client(), last["content"], build_registry(os.getenv("TOOLS_URL", "http://tools:8000")),
                model=os.getenv("MODEL", "claude-haiku-4-5-20251001"),
                system=SYSTEM_PROMPT,
                max_iterations=int(os.getenv("MAX_ITERATIONS", "10")),
                history=history, on_event=events.put,
            )
            events.put({"type": "answer", "text": result.answer, "finished": result.finished,
                        "iterations": result.iterations,
                        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        except AgentError as exc:
            events.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # never leave the stream hanging
            events.put({"type": "error", "message": f"unexpected error: {exc}"})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream() -> Iterator[str]:
        while (event := events.get()) is not None:
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
