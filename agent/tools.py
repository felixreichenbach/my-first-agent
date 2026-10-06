"""Tool definitions and the registry the loop uses.

A tool is: a name, a description the model reads, a JSON-schema for its input,
and a Python function that does the work. To add a tool, write one function and
add one entry to `build_registry` -- the loop does not change.
"""
from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable

import httpx

MAX_TOOL_OUTPUT = 10_000  # bytes/characters returned to the model per tool call


class ToolError(Exception):
    """Raised by a tool; the message is shown to the model so it can recover."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: Callable[..., str]

    def definition(self) -> dict[str, Any]:
        """The shape the Anthropic API expects in the `tools` parameter."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Keep tool output small: big outputs cost tokens and can drown the task."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated: {len(text) - limit} more characters]"


# --- calculator: a pure local tool -------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval(node: ast.AST) -> float:
    # Walk the syntax tree and allow only numbers and arithmetic operators.
    # We never call eval() on text that came from a model.
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolError("exponent too large")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    raise ToolError("only numbers and + - * / // % ** ( ) are allowed")


def calculator(expression: str) -> str:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        return str(_eval(tree.body))
    except ZeroDivisionError:
        raise ToolError("division by zero")
    except (SyntaxError, ValueError, OverflowError, RecursionError):
        raise ToolError(f"could not evaluate expression: {expression!r}")


# --- remote tools: thin clients for the tools service -------------------------

def _remote(tools_url: str, endpoint: str, payload: dict[str, Any]) -> str:
    try:
        resp = httpx.post(f"{tools_url}/{endpoint}", json=payload, timeout=30)
    except httpx.HTTPError as exc:
        raise ToolError(f"tools service unreachable: {exc}")
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200:
        raise ToolError(data.get("error") or data.get("detail") or f"HTTP {resp.status_code}")
    return str(data.get("result", ""))


def build_registry(tools_url: str) -> dict[str, Tool]:
    tools = [
        Tool(
            "calculator",
            "Evaluate an arithmetic expression, e.g. '17 * 23' or '(2 + 3) ** 2'. "
            "Use this instead of doing maths yourself.",
            {"type": "object",
             "properties": {"expression": {"type": "string"}},
             "required": ["expression"]},
            calculator,
        ),
        Tool(
            "fetch_url",
            "Fetch a public web page over http(s) and return its text content.",
            {"type": "object",
             "properties": {"url": {"type": "string", "description": "Full http(s) URL"}},
             "required": ["url"]},
            lambda url: _remote(tools_url, "fetch_url", {"url": url}),
        ),
        Tool(
            "write_note",
            "Save text to a named note that persists between runs. Overwrites an existing note.",
            {"type": "object",
             "properties": {"name": {"type": "string", "description": "letters, digits, - and _ only"},
                            "content": {"type": "string"}},
             "required": ["name", "content"]},
            lambda name, content: _remote(tools_url, "write_note", {"name": name, "content": content}),
        ),
        Tool(
            "read_note",
            "Read a previously saved note by name.",
            {"type": "object",
             "properties": {"name": {"type": "string"}},
             "required": ["name"]},
            lambda name: _remote(tools_url, "read_note", {"name": name}),
        ),
    ]
    return {t.name: t for t in tools}
