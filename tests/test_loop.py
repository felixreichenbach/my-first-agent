"""Loop tests use a fake model client that replays scripted responses."""
import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from loop import AgentError, run_agent
from tools import Tool, ToolError


def msg(content, stop_reason):
    return Message(id="m", type="message", role="assistant", model="fake",
                   content=content, stop_reason=stop_reason,
                   usage=Usage(input_tokens=10, output_tokens=5))


def text(t):
    return msg([TextBlock(type="text", text=t)], "end_turn")


def tool_use(name, input, id="tu_1"):
    return msg([ToolUseBlock(type="tool_use", id=id, name=name, input=input)], "tool_use")


class FakeClient:
    """Stands in for anthropic.Anthropic: returns scripted responses, records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs["messages"][:])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def echo_registry(func=lambda text: text.upper()):
    tool = Tool("echo", "echo", {"type": "object", "properties": {"text": {"type": "string"}}}, func)
    return {"echo": tool}


def run(client, registry=None, **kw):
    return run_agent(client, "task", registry or echo_registry(), model="fake", system="s", **kw)


def test_answer_without_tools():
    result = run(FakeClient([text("hello")]))
    assert result.answer == "hello" and result.finished and result.iterations == 1
    assert (result.input_tokens, result.output_tokens) == (10, 5)


def test_single_tool_call_feeds_result_back():
    client = FakeClient([tool_use("echo", {"text": "hi"}), text("done: HI")])
    result = run(client)
    assert result.answer == "done: HI" and result.iterations == 2
    last_user = client.requests[1][-1]
    assert last_user["role"] == "user"
    assert last_user["content"][0]["tool_use_id"] == "tu_1"
    assert last_user["content"][0]["content"] == "HI"
    assert last_user["content"][0]["is_error"] is False


def test_multi_step_tool_calls():
    client = FakeClient([tool_use("echo", {"text": "a"}, "1"), tool_use("echo", {"text": "b"}, "2"), text("ok")])
    assert run(client).iterations == 3


def test_tool_error_is_returned_to_model():
    def boom(text):
        raise ToolError("nope")

    client = FakeClient([tool_use("echo", {"text": "x"}), text("recovered")])
    result = run(client, echo_registry(boom))
    assert result.answer == "recovered"
    tool_result = client.requests[1][-1]["content"][0]
    assert tool_result["is_error"] is True and "nope" in tool_result["content"]


def test_unknown_tool_and_bad_arguments_do_not_crash():
    client = FakeClient([tool_use("missing", {}, "1"), tool_use("echo", {"wrong": 1}, "2"), text("ok")])
    result = run(client)
    assert result.finished
    assert client.requests[1][-1]["content"][0]["is_error"] is True
    assert client.requests[2][-1]["content"][0]["is_error"] is True


def test_iteration_cap():
    client = FakeClient([tool_use("echo", {"text": "x"}, str(i)) for i in range(5)])
    result = run(client, max_iterations=3)
    assert not result.finished and result.iterations == 3
    assert len(client.responses) == 2  # stopped asking the model


def test_api_failure_raises_agent_error():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    with pytest.raises(AgentError):
        run(FakeClient([err]))


def test_history_is_prepended_and_events_emitted():
    client = FakeClient([tool_use("echo", {"text": "x"}), text("fin")])
    events = []
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    run(client, history=history, on_event=events.append)
    assert [m["content"] for m in client.requests[0]] == ["earlier", "reply", "task"]
    assert [e["type"] for e in events] == ["iteration", "tool_call", "tool_result", "iteration", "model_text"]
    assert events[2] == {"type": "tool_result", "name": "echo", "output": "X", "is_error": False}
