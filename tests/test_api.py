import json

import pytest
from fastapi.testclient import TestClient

import api
from test_loop import make_model, text, tool_use


def parse_sse(body: str):
    events = []
    for block in body.strip().split("\n\n"):
        data = next(line for line in block.split("\n") if line.startswith("data: "))
        events.append(json.loads(data[6:]))
    return events


@pytest.fixture
def client(monkeypatch):
    return TestClient(api.app)


def chat(client, messages):
    return client.post("/chat", json={"messages": messages})


def test_streams_loop_events_then_answer(client, monkeypatch):
    fake = make_model([tool_use("calculator", {"expression": "2+2"}), text("it is 4")])
    monkeypatch.setattr(api, "get_model", lambda: fake)
    r = chat(client, [{"role": "user", "content": "2+2?"}])
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [e["type"] for e in events] == ["iteration", "tool_call", "tool_result", "iteration", "model_text", "answer"]
    assert events[2]["output"] == "4"
    assert events[-1]["text"] == "it is 4" and events[-1]["finished"] is True


def test_history_is_passed_to_the_model(client, monkeypatch):
    fake = make_model([text("ok")])
    monkeypatch.setattr(api, "get_model", lambda: fake)
    chat(client, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                  {"role": "user", "content": "c"}])
    assert [m.content for m in fake.requests[0]][-3:] == ["a", "b", "c"]


def test_model_failure_becomes_error_event(client, monkeypatch):
    monkeypatch.setattr(api, "get_model", lambda: make_model([ConnectionError("network down")]))
    events = parse_sse(chat(client, [{"role": "user", "content": "hi"}]).text)
    assert events[-1]["type"] == "error"


@pytest.mark.parametrize("messages", [
    [], [{"role": "system", "content": "x"}], [{"role": "user", "content": ""}],
    [{"role": "user", "content": "x" * 10_001}],
])
def test_invalid_requests_rejected(client, messages):
    assert chat(client, messages).status_code == 422


def test_last_message_must_be_from_user(client):
    r = chat(client, [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert r.status_code == 400


def test_model_creation_failure_becomes_error_event(client, monkeypatch):
    def broken():
        raise api.AgentError("could not create the model 'x': missing provider package")

    monkeypatch.setattr(api, "get_model", broken)
    events = parse_sse(chat(client, [{"role": "user", "content": "a"}]).text)
    assert events[-1]["type"] == "error" and "could not create the model" in events[-1]["message"]
