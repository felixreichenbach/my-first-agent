"""get_model builds a model for any provider LangChain supports, and fails with a clear AgentError."""
import pytest

import main
from loop import AgentError


def test_default_model_is_anthropic_without_a_provider_prefix(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert type(main.get_model()).__name__ == "ChatAnthropic"


@pytest.mark.parametrize("name", ["claude-haiku-4-5-20251001", "anthropic:claude-haiku-4-5-20251001"])
def test_model_name_with_or_without_provider_prefix(monkeypatch, name):
    monkeypatch.setenv("MODEL", name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert type(main.get_model()).__name__ == "ChatAnthropic"


@pytest.mark.parametrize("name,hint", [
    ("nonsense-model", "infer model provider"),
    ("openai:gpt-5.5", "langchain-openai"),    # provider named, package not installed
])
def test_unusable_model_gives_a_clear_error(monkeypatch, name, hint):
    monkeypatch.setenv("MODEL", name)
    with pytest.raises(AgentError) as info:
        main.get_model()
    assert name in str(info.value) and hint in str(info.value)
