"""The agent loop. This file is the point of the whole project.

    while not done:
        response = model(messages, tools)
        if the model asked for tools: run them, append the results, continue
        else: the model is finished -- return its answer
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import anthropic
from opentelemetry import trace

from telemetry import capture_content
from tools import Tool, ToolError, truncate

tracer = trace.get_tracer("my-first-agent")


class AgentError(Exception):
    """The run failed in a way the model cannot recover from (e.g. the API is down)."""


@dataclass
class RunResult:
    answer: str
    finished: bool          # False if we stopped because we hit the iteration cap
    iterations: int
    input_tokens: int
    output_tokens: int


def run_agent(
    client: anthropic.Anthropic,
    task: str,
    registry: dict[str, Tool],
    *,
    model: str,
    system: str,
    max_iterations: int = 10,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], None] = lambda event: None,
) -> RunResult:
    """Run the loop. `history` is earlier chat turns (plain user/assistant text), if any.

    Progress is reported through `on_event` so the CLI can print it and the web
    API can stream it; the loop itself does no I/O.
    """
    messages: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": task}]
    tool_defs = [t.definition() for t in registry.values()]
    in_tok = out_tok = 0

    with tracer.start_as_current_span("agent.run") as run_span:
        run_span.set_attribute("agent.model", model)
        if capture_content():
            run_span.set_attribute("agent.task", task)

        for iteration in range(1, max_iterations + 1):
            with tracer.start_as_current_span("agent.iteration") as it_span:
                it_span.set_attribute("agent.iteration", iteration)
                on_event({"type": "iteration", "n": iteration})

                # 1. Ask the model what to do next. The SDK retries transient
                #    errors (429/5xx/network) with backoff; we only see the final failure.
                with tracer.start_as_current_span("model.call") as span:
                    try:
                        response = client.messages.create(
                            model=model, max_tokens=2048, system=system,
                            messages=messages, tools=tool_defs,
                        )
                    except anthropic.APIError as exc:
                        span.record_exception(exc)
                        raise AgentError(f"model API call failed: {exc}") from exc
                    span.set_attribute("agent.stop_reason", str(response.stop_reason))
                    span.set_attribute("agent.input_tokens", response.usage.input_tokens)
                    span.set_attribute("agent.output_tokens", response.usage.output_tokens)
                in_tok += response.usage.input_tokens
                out_tok += response.usage.output_tokens

                # Remember what the model said: the API is stateless, so we resend the whole history.
                messages.append({"role": "assistant", "content": response.content})

                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        on_event({"type": "model_text", "text": block.text.strip()})

                # 2. No tool requests -> the model is done.
                tool_calls = [b for b in response.content if b.type == "tool_use"]
                if not tool_calls:
                    answer = "".join(b.text for b in response.content if b.type == "text")
                    if response.stop_reason == "max_tokens":
                        answer += "\n[warning: answer was cut off by the token limit]"
                    return RunResult(answer, True, iteration, in_tok, out_tok)

                # 3. Run every requested tool and send all results back in ONE user message.
                results = [_run_tool(call, registry, on_event) for call in tool_calls]
                messages.append({"role": "user", "content": results})

        # Safety net: never loop forever (and never spend unbounded money).
        run_span.set_attribute("agent.hit_iteration_cap", True)
        return RunResult(
            f"Stopped after {max_iterations} iterations without a final answer.",
            False, max_iterations, in_tok, out_tok,
        )


def _run_tool(call: Any, registry: dict[str, Tool], on_event: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Execute one tool call. Errors become results the model can read, not crashes."""
    with tracer.start_as_current_span("tool.call") as span:
        span.set_attribute("agent.tool", call.name)
        on_event({"type": "tool_call", "name": call.name, "input": call.input})
        if capture_content():
            span.set_attribute("agent.tool_input", str(call.input))

        is_error = False
        tool = registry.get(call.name)
        try:
            if tool is None:
                raise ToolError(f"unknown tool {call.name!r}; available: {', '.join(registry)}")
            output = truncate(str(tool.func(**call.input)))
        except ToolError as exc:
            is_error, output = True, f"Error: {exc}"
        except TypeError as exc:  # model sent arguments that don't match the schema
            is_error, output = True, f"Error: bad arguments for {call.name}: {exc}"
        except Exception as exc:  # a bug in a tool must not kill the run
            is_error, output = True, f"Error: tool {call.name} failed: {exc}"

        span.set_attribute("agent.tool_error", is_error)
        if capture_content():
            span.set_attribute("agent.tool_output", output)
        on_event({"type": "tool_result", "name": call.name, "output": output, "is_error": is_error})

        return {"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": is_error}
