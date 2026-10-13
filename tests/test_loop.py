"""Loop tests use a scripted fake chat model: no network, no API key."""
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from loop import AgentError, run_agent
from tools import ToolError


class FakeChatModel(BaseChatModel):
    """Replays scripted AIMessages (or raises scripted exceptions) and records what it was sent."""

    script: list
    requests: list = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):  # create_agent binds the tools; the fake ignores them
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def text(t):
    return AIMessage(content=t, usage_metadata=USAGE)


def tool_use(name, input, id="tu_1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": input, "id": id}], usage_metadata=USAGE)


def make_model(script):
    return FakeChatModel(script=list(script), requests=[])


def echo_tools(func=lambda text: text.upper()):
    def echo(text: str) -> str:
        """Echo text."""
        return func(text)

    return [StructuredTool.from_function(echo)]


def run(model, tools=None, **kw):
    events = kw.pop("events", None)
    return run_agent(model, "task", tools or echo_tools(), system="s",
                     on_event=(events.append if events is not None else (lambda e: None)), **kw)


def test_answer_without_tools():
    result = run(make_model([text("hello")]))
    assert result.answer == "hello" and result.finished and result.iterations == 1
    assert (result.input_tokens, result.output_tokens) == (10, 5)


def test_single_tool_call_feeds_result_back():
    model = make_model([tool_use("echo", {"text": "hi"}), text("done: HI")])
    events = []
    result = run(model, events=events)
    assert result.answer == "done: HI" and result.iterations == 2
    sent_second = model.requests[1]
    assert sent_second[-1].content == "HI" and sent_second[-1].tool_call_id == "tu_1"
    assert [e["type"] for e in events] == ["iteration", "tool_call", "tool_result", "iteration", "model_text"]
    assert events[2] == {"type": "tool_result", "name": "echo", "output": "HI", "is_error": False}


def test_multi_step_tool_calls():
    model = make_model([tool_use("echo", {"text": "a"}, "1"), tool_use("echo", {"text": "b"}, "2"), text("ok")])
    assert run(model).iterations == 3


def test_tool_error_is_returned_to_model():
    def boom(text):
        raise ToolError("nope")

    events = []
    model = make_model([tool_use("echo", {"text": "x"}), text("recovered")])
    result = run(model, echo_tools(boom), events=events)
    assert result.answer == "recovered"
    assert {"type": "tool_result", "name": "echo", "output": "Error: nope", "is_error": True} in events


def test_unexpected_tool_exception_does_not_crash():
    def boom(text):
        raise RuntimeError("bug")

    events = []
    result = run(make_model([tool_use("echo", {"text": "x"}), text("ok")]), echo_tools(boom), events=events)
    assert result.finished
    assert any(e["type"] == "tool_result" and e["is_error"] and "bug" in e["output"] for e in events)


def test_unknown_tool_and_bad_arguments_do_not_crash():
    events = []
    model = make_model([tool_use("missing", {}, "1"), tool_use("echo", {"wrong": 1}, "2"), text("ok")])
    result = run(model, events=events)
    assert result.finished
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2 and all(e["is_error"] for e in results)


def test_iteration_cap():
    model = make_model([tool_use("echo", {"text": "x"}, str(i)) for i in range(5)])
    result = run(model, max_iterations=3)
    assert not result.finished and result.iterations == 3
    assert len(model.script) == 2  # stopped asking the model


class ProviderError(Exception):
    """Stands in for any provider's own error type: the loop must not depend on one provider."""


@pytest.mark.parametrize("error", [ProviderError("rate limited"), ConnectionError("network down"),
                                   TypeError("no API key or authorization credentials were provided")])
def test_any_model_failure_raises_agent_error(error):
    with pytest.raises(AgentError) as info:
        run(make_model([error]))
    assert str(error) in str(info.value)


def test_provider_neutral_out_of_tokens_warning():
    from langchain_core.messages import AIMessage
    cut_off = [AIMessage(content="partial", usage_metadata=USAGE, response_metadata={"stop_reason": "max_tokens"}),
               AIMessage(content="partial", usage_metadata=USAGE, response_metadata={"finish_reason": "length"})]
    for message in cut_off:  # Anthropic-style and OpenAI-style signals
        assert "cut off by the token limit" in run(make_model([message])).answer


def test_history_is_prepended_and_events_emitted():
    model = make_model([tool_use("echo", {"text": "x"}), text("fin")])
    events = []
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    run(model, history=history, events=events)
    assert [m.content for m in model.requests[0]][-3:] == ["earlier", "reply", "task"]
    assert [e["type"] for e in events] == ["iteration", "tool_call", "tool_result", "iteration", "model_text"]
