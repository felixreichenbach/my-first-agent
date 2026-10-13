"""The agent loop, LangChain edition.

`create_agent` builds the loop for us (it is a LangGraph graph under the hood):

    model node -> (did the model ask for tools?) -> tools node -> model node -> ... -> done

What we add on top: an iteration cap, tool errors returned to the model, and a stream of events
for the CLI and the web UI. Compare with the hand-written loop on the `main` branch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolErrorMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from tools import ToolError


class AgentError(Exception):
    """The run failed in a way the model cannot recover from (e.g. the API is down)."""


@dataclass
class RunResult:
    answer: str
    finished: bool          # False if we stopped because we hit the iteration cap
    iterations: int
    input_tokens: int
    output_tokens: int


def _tool_error(exc: Exception, request: Any) -> str:
    """Return a tool failure to the model as text, so it can recover instead of crashing the run."""
    if isinstance(exc, ToolError):
        return f"Error: {exc}"
    return f"Error: tool {request.tool_call['name']} failed: {exc}"


def run_agent(
    model: BaseChatModel,
    task: str,
    tools: list[BaseTool],
    *,
    system: str,
    max_iterations: int = 10,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], None] = lambda event: None,
) -> RunResult:
    """Run the agent. `history` is earlier chat turns (plain user/assistant text), if any.

    Progress is reported through `on_event` so the CLI can print it and the web API can stream it.
    """
    agent = create_agent(
        model, tools, system_prompt=system,
        middleware=[
            # One model call per iteration: this is our iteration cap.
            ModelCallLimitMiddleware(run_limit=max_iterations, exit_behavior="error"),
            ToolErrorMiddleware(on_error=_tool_error),
        ],
    )
    messages = [
        *(AIMessage(m["content"]) if m["role"] == "assistant" else HumanMessage(m["content"])
          for m in (history or [])),
        HumanMessage(task),
    ]

    iterations = in_tok = out_tok = 0
    answer = ""
    try:
        # stream_mode="updates" yields one chunk per graph node as it finishes.
        for chunk in agent.stream({"messages": messages}, stream_mode="updates"):
            for node, update in chunk.items():
                for msg in (update or {}).get("messages", []):
                    if node == "model" and isinstance(msg, AIMessage):
                        iterations += 1
                        on_event({"type": "iteration", "n": iterations})
                        usage = msg.usage_metadata or {}
                        in_tok += usage.get("input_tokens", 0)
                        out_tok += usage.get("output_tokens", 0)
                        if msg.text.strip():
                            on_event({"type": "model_text", "text": msg.text.strip()})
                        for call in msg.tool_calls:
                            on_event({"type": "tool_call", "name": call["name"], "input": call["args"]})
                        if not msg.tool_calls:
                            answer = msg.text
                            meta = msg.response_metadata  # provider-specific key for "ran out of tokens"
                            if meta.get("stop_reason") == "max_tokens" or meta.get("finish_reason") == "length":
                                answer += "\n[warning: answer was cut off by the token limit]"
                    elif node == "tools" and isinstance(msg, ToolMessage):
                        on_event({"type": "tool_result", "name": msg.name, "output": str(msg.content),
                                  "is_error": msg.status == "error"})
    except ModelCallLimitExceededError:
        # Safety net: never loop forever (and never spend unbounded money).
        return RunResult(f"Stopped after {max_iterations} iterations without a final answer.",
                         False, iterations, in_tok, out_tok)
    except Exception as exc:
        # Provider-agnostic on purpose: every provider raises its own error types (a bad key, a rate
        # limit, a network error). Tool failures never get here: ToolErrorMiddleware handles them.
        raise AgentError(f"agent run failed: {exc}") from exc

    return RunResult(answer, True, iterations, in_tok, out_tok)
