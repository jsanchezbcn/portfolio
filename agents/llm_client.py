"""agents/llm_client.py — Unified LLM chat helper.

Priority order for LLM backend:
1. GitHub Copilot SDK — authenticates via ``copilot`` CLI (installed via GitHub
   Copilot for CLI).  No API key required; auth comes from ``gh auth login``
   stored in keyring.  Optionally enhanced with BYOK when ``OPENAI_API_KEY``
   is also set (skips GitHub subscription quota).
2. Direct ``openai.AsyncOpenAI`` fallback — only used when the SDK fails AND
   ``OPENAI_API_KEY`` is available.

Authentication requirements:
- Install GitHub Copilot CLI: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli
- Authenticate once: ``gh auth login`` (stored in keyring, no env var needed)
- Verify: ``copilot --version``

Usage::

    from agents.llm_client import async_llm_chat

    result = await async_llm_chat(
        prompt="Summarise the sentiment of these headlines…",
        model="gpt-4.1",
        system="You are a financial analyst.",
        timeout=30.0,
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Public helper                                                        #
# ------------------------------------------------------------------ #


async def async_llm_chat(
    prompt: str,
    *,
    model: str = "gpt-4.1",
    system: str = "",
    timeout: float = 45.0,
) -> str:
    """Send *prompt* to an LLM and return the assistant's text reply.

    Args:
        prompt:  User message text.
        model:   Model identifier (e.g. ``"gpt-4.1"``).
        system:  Optional system / developer message prepended to the session.
        timeout: Maximum seconds to wait for a response (default 45).

    Returns:
        The assistant's reply text, or ``""`` on failure.
    """
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()

    # --- 1. Try Copilot SDK via gh auth keyring (no API key needed) ---
    # This uses the 'copilot' CLI which picks up 'gh auth login' credentials.
    try:
        return await asyncio.wait_for(
            _copilot_chat(
                prompt=prompt,
                model=model,
                system=system,
                openai_key="",  # no BYOK — use GitHub Copilot auth via keyring
                timeout=timeout,
            ),
            timeout=timeout + 20,
        )
    except asyncio.TimeoutError:
        logger.warning("Copilot SDK (gh auth) timed out after %.0fs", timeout + 20)
    except Exception as exc:
        logger.info("Copilot SDK (gh auth) failed (%s: %s); trying fallbacks…", type(exc).__name__, exc)

    # --- 2. Try Copilot SDK + BYOK (requires valid OPENAI_API_KEY) ---
    if openai_key:
        try:
            return await asyncio.wait_for(
                _copilot_chat(
                    prompt=prompt,
                    model=model,
                    system=system,
                    openai_key=openai_key,
                    timeout=timeout,
                ),
                timeout=timeout + 20,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.info("Copilot SDK BYOK failed (%s); trying direct OpenAI…", exc)

        # --- 3. Direct OpenAI (no CLI dependency) ---
        try:
            return await _openai_direct_chat(
                prompt=prompt,
                model=model,
                system=system,
                timeout=timeout,
                api_key=openai_key,
            )
        except Exception as exc:
            logger.error("Direct OpenAI fallback failed: %s", exc)

    logger.error(
        "No LLM response available. Ensure 'copilot' CLI is installed "
        "and authenticated via 'gh auth login', or set a valid OPENAI_API_KEY."
    )
    return ""


# ------------------------------------------------------------------ #
# Public helper: list available models                                 #
# ------------------------------------------------------------------ #

_HARDCODED_MODELS: list[dict] = [
    {"id": "gpt-4.1",          "name": "GPT-4.1",          "is_free": True},
    {"id": "gpt-4o",           "name": "GPT-4o",            "is_free": True},
    {"id": "gpt-4o-mini",      "name": "GPT-4o mini",       "is_free": True},
    {"id": "claude-sonnet-4.5","name": "Claude Sonnet 4.5", "is_free": False},
    {"id": "o3-mini",          "name": "o3-mini",           "is_free": False},
    {"id": "o1",               "name": "o1",                "is_free": False},
    {"id": "gpt-4.5-preview",  "name": "GPT-4.5 Preview",   "is_free": False},
]

_FREE_MODEL_IDS: frozenset[str] = frozenset(
    m["id"] for m in _HARDCODED_MODELS if m["is_free"]
)


async def async_list_models() -> list[dict]:
    """Return available LLM models discovered via the Copilot SDK.

    Each entry is ``{"id": str, "name": str, "is_free": bool}``.
    Falls back to :data:`_HARDCODED_MODELS` if the SDK call fails.
    Free models (gpt-4.1, gpt-4o, gpt-4o-mini) are always listed first.
    """
    try:
        from copilot import CopilotClient  # type: ignore[import]

        client = CopilotClient({"log_level": "error"})
        await client.start()
        try:
            raw_models = await asyncio.wait_for(client.list_models(), timeout=10)
        finally:
            try:
                await client.stop()
            except Exception:
                pass

        result: list[dict] = []
        for m in raw_models:
            policy_state = getattr(getattr(m, "policy", None), "state", "enabled")
            if policy_state == "disabled":
                continue
            billing = getattr(m, "billing", None)
            multiplier: float = getattr(billing, "multiplier", 1.0) if billing else 1.0
            # Models with multiplier == 0 or whose id is in the known-free set are free
            is_free = (multiplier == 0) or (m.id in _FREE_MODEL_IDS)
            result.append({"id": m.id, "name": getattr(m, "name", m.id), "is_free": is_free})

        if result:
            # Sort: free first, then alphabetical by name
            result.sort(key=lambda x: (not x["is_free"], x["name"].lower()))
            logger.debug("async_list_models: %d models from SDK", len(result))
            return result

    except Exception as exc:
        logger.info("async_list_models: SDK call failed (%s); using hardcoded list", exc)

    return list(_HARDCODED_MODELS)


# ------------------------------------------------------------------ #
# Internal: Copilot SDK backend                                        #
# ------------------------------------------------------------------ #


async def _copilot_chat(
    prompt: str,
    *,
    model: str,
    system: str,
    openai_key: str,
    timeout: float,
) -> str:
    """Call the Copilot SDK, wiring BYOK when OPENAI_API_KEY is available."""
    from copilot import CopilotClient  # type: ignore[import]

    session_cfg: dict = {"model": model}

    if openai_key:
        # BYOK — routes through OpenAI directly, skips GitHub subscription quota
        session_cfg["provider"] = {
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": openai_key,
        }
        logger.debug("LLM: Copilot SDK + BYOK (OpenAI key)")
    else:
        # Standard — uses GitHub Copilot auth from 'gh auth login' keyring
        logger.debug("LLM: Copilot SDK via GitHub Copilot CLI (gh auth keyring)")

    if system:
        session_cfg["system_message"] = {"content": system, "role": "system"}

    # Disable infinite sessions for simple one-shot calls
    session_cfg["infinite_sessions"] = {"enabled": False}

    client = CopilotClient({"log_level": "error"})
    await client.start()
    session = None
    try:
        session = await client.create_session(session_cfg)
        response = await asyncio.wait_for(
            session.send_and_wait({"prompt": prompt}),
            timeout=timeout,
        )
        return (response.data.content if response and response.data else "") or ""
    finally:
        if session is not None:
            try:
                await session.destroy()
            except Exception:
                pass
        try:
            await client.stop()
        except Exception:
            pass


# ------------------------------------------------------------------ #
# Internal: direct OpenAI fallback                                     #
# ------------------------------------------------------------------ #


async def _openai_direct_chat(
    prompt: str,
    *,
    model: str,
    system: str,
    timeout: float,
    api_key: str,
) -> str:
    """Call OpenAI directly via openai.AsyncOpenAI (no Copilot CLI needed)."""
    import openai  # type: ignore[import]

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    aclient = openai.AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        aclient.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.2,
        ),
        timeout=timeout,
    )
    if response.choices:
        return response.choices[0].message.content or ""
    return ""


# ------------------------------------------------------------------ #
# Session config helper (for agents that manage sessions themselves)   #
# ------------------------------------------------------------------ #


def build_session_config(model: str, *, system: str = "") -> dict:
    """Return a ``create_session`` config dict with BYOK wired when available.

    Prefer :func:`async_llm_chat` for simple one-shot calls; use this only
    when you need to manage the session lifecycle yourself.
    """
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    cfg: dict = {"model": model, "infinite_sessions": {"enabled": False}}
    if openai_key:
        cfg["provider"] = {
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": openai_key,
        }
    if system:
        cfg["system_message"] = {"content": system, "role": "system"}
    return cfg
