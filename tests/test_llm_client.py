from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from agents.llm_client import build_session_config
from agents import llm_client


def test_build_session_config_has_no_openai_provider(monkeypatch):
    monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "work")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "jose-antonio-sanchez_hpi")

    cfg = build_session_config("gpt-5-mini")

    assert "provider" not in cfg


def test_build_session_config_includes_aistudio_provider():
    cfg = build_session_config("aistudio:gemini-3.1-flash-lite-preview")

    assert cfg["provider"] == "aistudio"
    assert cfg["model"] == "gemini-3.1-flash-lite-preview"


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


@pytest.mark.asyncio
async def test_async_llm_chat_routes_aistudio_models(monkeypatch):
    calls: list[str] = []

    async def fake_aistudio_chat(*, prompt, model, system, timeout):
        calls.append(model)
        return "gemini ok"

    monkeypatch.setattr(llm_client, "_aistudio_chat", fake_aistudio_chat)

    text = await llm_client.async_llm_chat("ping", model="aistudio:gemini-3.1-flash-lite-preview", timeout=1.0)

    assert text == "gemini ok"
    assert calls == ["gemini-3.1-flash-lite-preview"]


@pytest.mark.asyncio
async def test_async_list_models_keeps_hardcoded_aistudio_entry(monkeypatch):
    class FakeClient:
        async def start(self):
            return None

        async def stop(self):
            return None

        async def list_models(self):
            return [
                SimpleNamespace(
                    id="gpt-5-mini",
                    name="GPT-5 mini",
                    policy=SimpleNamespace(state="enabled"),
                    billing=SimpleNamespace(multiplier=0.0),
                )
            ]

    monkeypatch.setitem(sys.modules, "copilot", SimpleNamespace(CopilotClient=lambda _cfg: FakeClient()))

    models = await llm_client.async_list_models()

    assert any(model["id"] == "gpt-5-mini" for model in models)
    assert any(model["id"] == "aistudio:gemini-3.1-flash-lite-preview" for model in models)
