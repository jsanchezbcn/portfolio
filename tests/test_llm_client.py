from __future__ import annotations

import pytest

from agents.llm_client import build_session_config
from agents import llm_client


def test_build_session_config_has_no_openai_provider(monkeypatch):
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "work")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "jose-antonio-sanchez_hpi")

    cfg = build_session_config("gpt-5-mini")

    assert "provider" not in cfg


def test_build_session_config_includes_system_message(monkeypatch):
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "personal")
    monkeypatch.setenv("GITHUB_COPILOT_PERSONAL", "jsanchezbcn")

    cfg = build_session_config("gpt-5-mini", system="You are a risk assistant")

    assert cfg["system_message"]["content"] == "You are a risk assistant"


@pytest.mark.asyncio
async def test_async_llm_chat_switches_to_profile_account_before_chat(monkeypatch):
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "personal")
    monkeypatch.setenv("GITHUB_COPILOT_PERSONAL", "jsanchezbcn")
    monkeypatch.delenv("GITHUB_COPILOT_WORK", raising=False)

    switched: list[str] = []
    calls: list[str] = []

    def fake_switch(user: str) -> None:
        switched.append(user)

    async def fake_copilot_chat(*, prompt, model, system, timeout):
        calls.append(model)
        return "ok"

    monkeypatch.setattr(llm_client, "_switch_gh_account", fake_switch)
    monkeypatch.setattr(llm_client, "_copilot_chat", fake_copilot_chat)

    text = await llm_client.async_llm_chat("ping", model="gpt-5-mini", timeout=1.0)

    assert text == "ok"
    assert switched == ["jsanchezbcn"]
    assert calls == ["gpt-5-mini"]


@pytest.mark.asyncio
async def test_async_llm_chat_ignores_token_like_active_account(monkeypatch):
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "work")
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_TOKEN", "gho_fake_token_like_value")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "jose-antonio-sanchez_hpi")

    switched: list[str] = []

    def fake_switch(user: str) -> None:
        switched.append(user)

    async def fake_copilot_chat(*, prompt, model, system, timeout):
        return "ok"

    monkeypatch.setattr(llm_client, "_switch_gh_account", fake_switch)
    monkeypatch.setattr(llm_client, "_copilot_chat", fake_copilot_chat)

    text = await llm_client.async_llm_chat("ping", model="gpt-5-mini", timeout=1.0)

    assert text == "ok"
    assert switched == ["jose-antonio-sanchez_hpi"]
