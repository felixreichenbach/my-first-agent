import pytest
from fastapi.testclient import TestClient

import app as tools_app
from tools import ToolError, calculator, truncate


@pytest.mark.parametrize("expr,expected", [("17 * 23", "391"), ("(2 + 3) ** 2", "25"), ("-4 + 10 / 4", "-1.5")])
def test_calculator(expr, expected):
    assert calculator(expr) == expected


@pytest.mark.parametrize("expr", ["__import__('os').system('id')", "1 +", "2 ** 99999", "1/0", "abs(1)", "True + 1"])
def test_calculator_rejects_unsafe_or_invalid(expr):
    with pytest.raises(ToolError):
        calculator(expr)


def test_truncate():
    assert truncate("abc", 10) == "abc"
    assert "truncated" in truncate("a" * 20, 10)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_app, "WORKSPACE", tmp_path)
    return TestClient(tools_app.app)


def test_notes_roundtrip(client):
    assert client.post("/write_note", json={"name": "todo", "content": "milk"}).status_code == 200
    assert client.post("/read_note", json={"name": "todo"}).json()["result"] == "milk"


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "", "x" * 65])
def test_note_name_traversal_rejected(client, name):
    r = client.post("/read_note", json={"name": name})
    assert r.status_code == 400 and "invalid note name" in r.json()["error"]


def test_missing_note(client):
    assert client.post("/read_note", json={"name": "nope"}).status_code == 400


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "http://localhost:8000/health"])
def test_fetch_url_blocks_unsafe_targets(client, url):
    r = client.post("/fetch_url", json={"url": url})
    assert r.status_code == 400
