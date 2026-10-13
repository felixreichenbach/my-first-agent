"""CLI entry point: read config from the environment, run the loop, print the answer."""
from __future__ import annotations

import os
import sys

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from loop import AgentError, run_agent
from tools import build_tools

SYSTEM_PROMPT = (
    "You are a concise assistant with tools. Use a tool whenever it gives a more "
    "reliable answer than guessing (arithmetic, web pages, saved notes). "
    "When you have what you need, reply with the final answer and no further tool calls."
)


def get_model() -> BaseChatModel:
    """Build the chat model from MODEL, for any provider LangChain supports.

    "claude-haiku-4-5-20251001" and "anthropic:claude-haiku-4-5-20251001" both work. For another
    provider install its package (e.g. langchain-openai), set MODEL=openai:<model> and that
    provider's API key.
    """
    name = os.getenv("MODEL", "claude-haiku-4-5-20251001")
    try:
        return init_chat_model(name, max_tokens=2048, max_retries=3)  # bounded retries with backoff
    except Exception as exc:
        raise AgentError(f"could not create the model {name!r}: {exc}") from exc


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
            get_model(),
            task,
            build_tools(os.getenv("TOOLS_URL", "http://tools:8000")),
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
