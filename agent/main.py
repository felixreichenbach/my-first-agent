"""CLI entry point: read config from the environment, run the loop, print the answer."""
from __future__ import annotations

import os
import sys

import anthropic

from loop import AgentError, run_agent
from tools import build_registry

SYSTEM_PROMPT = (
    "You are a concise assistant with tools. Use a tool whenever it gives a more "
    "reliable answer than guessing (arithmetic, web pages, saved notes). "
    "When you have what you need, reply with the final answer and no further tool calls."
)


def print_event(event: dict) -> None:
    """Render loop events as a readable trace on stderr (stdout is for the answer)."""
    kind = event["type"]
    if kind == "iteration":
        print(f"\n--- iteration {event['n']} ---", file=sys.stderr)
    elif kind == "model_text":
        print(f"model: {event['text']}", file=sys.stderr)
    elif kind == "tool_call":
        print(f"tool call: {event['name']}({event['input']})", file=sys.stderr)
    elif kind == "tool_result":
        flag = " (error)" if event["is_error"] else ""
        print(f"tool result{flag}: {event['output'][:200]}", file=sys.stderr)


def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set (copy .env.example to .env)", file=sys.stderr)
        return 1

    task = " ".join(sys.argv[1:]).strip()
    if not task:
        if not sys.stdin.isatty():
            print("usage: docker compose run --rm agent-cli \"your task\"", file=sys.stderr)
            return 1
        task = input("Task: ").strip()
        if not task:
            return 1

    try:
        max_iterations = int(os.getenv("MAX_ITERATIONS", "10"))
    except ValueError:
        print("error: MAX_ITERATIONS must be an integer", file=sys.stderr)
        return 1

    try:
        result = run_agent(
            anthropic.Anthropic(max_retries=3),  # bounded retries with backoff
            task,
            build_registry(os.getenv("TOOLS_URL", "http://tools:8000")),
            model=os.getenv("MODEL", "claude-haiku-4-5-20251001"),
            system=SYSTEM_PROMPT,
            max_iterations=max_iterations,
            on_event=print_event,
        )
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n{result.answer}")
    print(f"\n[{result.iterations} iteration(s), {result.input_tokens} input / "
          f"{result.output_tokens} output tokens]", file=sys.stderr)
    return 0 if result.finished else 2


if __name__ == "__main__":
    sys.exit(main())
