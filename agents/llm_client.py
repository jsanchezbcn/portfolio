"""agents/llm_client.py — Unified LLM chat helper.

🚨 IMPORTANT: This project uses GITHUB COPILOT SDK ONLY.
We do NOT use OpenAI API or OPENAI_API_KEY in this module.

Authentication flow:
1. Resolve active profile (personal/work).
2. Resolve configured github.com username for that profile.
3. Switch `gh` active account to that username on github.com.
4. Call Copilot SDK using the gh keyring-backed session.

GitHub Copilot CLI setup:
- Install: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli
- Authenticate: ``gh auth login`` (stores credentials in system keyring)
- Verify: ``copilot --version``

Usage::

    from agents.llm_client import async_llm_chat

    result = await async_llm_chat(
        prompt="Summarise the sentiment of these headlines…",
        model="gpt-4o",
        system="You are a financial analyst.",
        timeout=30.0,
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any, cast

logger = logging.getLogger(__name__)

_PROFILE_ACCOUNT_KEYS: dict[str, str] = {
    "personal": "GITHUB_COPILOT_PERSONAL",
    "work": "GITHUB_COPILOT_WORK",
}
_ACTIVE_PROFILE_ENV = "GITHUB_COPILOT_ACTIVE_PROFILE"
_ACTIVE_ACCOUNT_ENV = "GITHUB_COPILOT_ACTIVE_TOKEN"


def _is_token_like(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith(("gho_", "ghp_", "github_pat_", "sk-"))


def _active_profile_name() -> str:
    profile = (os.getenv(_ACTIVE_PROFILE_ENV) or "personal").strip().lower()
    return profile if profile in _PROFILE_ACCOUNT_KEYS else "personal"


def _active_profile_account() -> str:
    active = (os.getenv(_ACTIVE_ACCOUNT_ENV) or "").strip()
    if active and not _is_token_like(active):
        return active
    account_key = _PROFILE_ACCOUNT_KEYS[_active_profile_name()]
    account = (os.getenv(account_key) or "").strip()
    return "" if _is_token_like(account) else account


def _ordered_profile_accounts() -> list[tuple[str, str]]:
    """Return unique non-empty profile usernames, active profile first."""
    active_profile = _active_profile_name()
    ordered_profiles = [active_profile] + [p for p in _PROFILE_ACCOUNT_KEYS if p != active_profile]
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    explicit_active = (os.getenv(_ACTIVE_ACCOUNT_ENV) or "").strip()
    if explicit_active and not _is_token_like(explicit_active):
        seen.add(explicit_active)
        result.append((f"{active_profile}:active", explicit_active))
    elif explicit_active:
        logger.warning(
            "Ignoring token-like value in %s; expected github.com username, got token prefix.",
            _ACTIVE_ACCOUNT_ENV,
        )
    for profile in ordered_profiles:
        account_key = _PROFILE_ACCOUNT_KEYS[profile]
        account = (os.getenv(account_key) or "").strip()
        if account and _is_token_like(account):
            logger.warning(
                "Ignoring token-like value in %s; configure github.com username for profile '%s'.",
                account_key,
                profile,
            )
            continue
        if account and account not in seen:
            seen.add(account)
            result.append((profile, account))
    return result


def _switch_gh_account(username: str) -> None:
    """Switch gh active account to `username` on github.com."""
    if not username:
        return
    proc = subprocess.run(
        ["gh", "auth", "switch", "--hostname", "github.com", "--user", username],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise RuntimeError(f"gh auth switch failed for '{username}': {detail}")

# ------------------------------------------------------------------ #
# Public helper                                                        #
# ------------------------------------------------------------------ #


async def async_llm_chat(
    prompt: str,
    *,
    model: str = "gpt-5-mini",
    system: str = "",
    timeout: float = 90.0,
) -> str:
    """Send *prompt* to an LLM and return the assistant's text reply.

    Args:
        prompt:  User message text.
        model:   Model identifier (e.g. ``"gpt-4.1"``).
        system:  Optional system / developer message prepended to the session.
        timeout: Maximum seconds to wait for a response (default 90).

    Returns:
        The assistant's reply text, or ``""`` on failure.
    """
    profile_accounts = _ordered_profile_accounts()
    fallback_model = (os.getenv("LLM_FAST_MODEL") or "gpt-5-mini").strip()

    # --- 1. Try configured profile github.com account(s), active profile first ---
    for label, username in profile_accounts:
        try:
            _switch_gh_account(username)
            return await asyncio.wait_for(
                _copilot_chat(
                    prompt=prompt,
                    model=model,
                    system=system,
                    timeout=timeout,
                ),
                timeout=timeout + 20,
            )
        except asyncio.TimeoutError:
            logger.warning("Copilot SDK profile account (%s) timed out after %.0fs", label, timeout + 20)
        except Exception as exc:
            logger.info("Copilot SDK profile account (%s) failed (%s)", label, exc)

    # --- 2. Try Copilot SDK with currently active gh account ---
    # If no profile account is configured, rely on gh default active github.com account.
    use_gh_auth_fallback = not profile_accounts
    if use_gh_auth_fallback:
        logger.warning(
            "No profile github.com account found in .env — falling back to current 'gh auth' keyring account. "
            "⚠️  Copilot SDK requires github.com authentication, NOT GitHub Enterprise. "
            "Run 'gh auth status' to verify you're logged into github.com."
        )
        try:
            return await asyncio.wait_for(
                _copilot_chat(
                    prompt=prompt,
                    model=model,
                    system=system,
                    timeout=timeout,
                ),
                timeout=timeout + 20,
            )
        except asyncio.TimeoutError:
            logger.warning("Copilot SDK (gh auth) timed out after %.0fs", timeout + 20)
        except Exception as exc:
            logger.info("Copilot SDK (gh auth) failed (%s: %s); trying fallbacks…", type(exc).__name__, exc)

    # --- 3. Final retry with fast model using active gh account ---
    try:
        retry_model = fallback_model or model
        return await asyncio.wait_for(
            _copilot_chat(
                prompt=prompt,
                model=retry_model,
                system=system,
                timeout=min(timeout, 35.0),
            ),
            timeout=min(timeout, 35.0) + 15,
        )
    except Exception as exc:
        logger.info("Final Copilot retry failed (%s: %s)", type(exc).__name__, exc)

    logger.error(
        "No LLM response available. Ensure the selected profile maps to a valid github.com username "
        "(GITHUB_COPILOT_PERSONAL / GITHUB_COPILOT_WORK) and gh is authenticated via 'gh auth login'."
    )
    return ""


# ------------------------------------------------------------------ #
# Public helper: list available models                                 #
# ------------------------------------------------------------------ #

_HARDCODED_MODELS: list[dict] = [
    {"id": "gpt-5-mini",        "name": "GPT-5 mini",         "is_free": True,  "cost_multiplier": 0.0},
    {"id": "gpt-4.1",           "name": "GPT-4.1",            "is_free": True,  "cost_multiplier": 0.0},
    {"id": "gpt-4o",            "name": "GPT-4o",             "is_free": True,  "cost_multiplier": 0.0},
    {"id": "gpt-4o-mini",       "name": "GPT-4o mini",        "is_free": True,  "cost_multiplier": 0.0},
    {"id": "gpt-5",             "name": "GPT-5",              "is_free": False, "cost_multiplier": 1.0},
    {"id": "o3",                "name": "o3",                 "is_free": False, "cost_multiplier": 3.0},
    {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5",  "is_free": False, "cost_multiplier": 0.33},
    {"id": "o3-mini",           "name": "o3-mini",            "is_free": False, "cost_multiplier": 0.25},
    {"id": "o1",                "name": "o1",                 "is_free": False, "cost_multiplier": 3.0},
    {"id": "gpt-4.5-preview",   "name": "GPT-4.5 Preview",    "is_free": False, "cost_multiplier": 1.0},
]

_FREE_MODEL_IDS: frozenset[str] = frozenset(
    m["id"] for m in _HARDCODED_MODELS if m["is_free"]
)


async def async_list_models() -> list[dict]:
    """Return available LLM models discovered via the Copilot SDK.

    Each entry is ``{"id": str, "name": str, "is_free": bool, "cost_multiplier": float | None}``.
    Falls back to :data:`_HARDCODED_MODELS` if the SDK call fails.
    Free models are listed first, then lower-cost models.
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
            result.append({
                "id": m.id,
                "name": getattr(m, "name", m.id),
                "is_free": is_free,
                "cost_multiplier": float(multiplier) if multiplier is not None else None,
            })

        if result:
            # Sort: free first, then lower-cost models, then alphabetical by name.
            def _cost(model: dict) -> float:
                raw = model.get("cost_multiplier")
                return float(raw) if isinstance(raw, (int, float)) else 999.0

            result.sort(
                key=lambda x: (
                    not x["is_free"],
                    _cost(x),
                    x["name"].lower(),
                )
            )
            logger.debug("async_list_models: %d models from SDK", len(result))
            return result

    except Exception as exc:
        logger.info("async_list_models: SDK call failed (%s); using hardcoded list", exc)

    return list(_HARDCODED_MODELS)


def get_hardcoded_models() -> list[dict]:
    """Return the fallback model catalog used before live discovery completes."""
    return list(_HARDCODED_MODELS)


# ------------------------------------------------------------------ #
# Internal: Copilot SDK backend                                        #
# ------------------------------------------------------------------ #


async def _copilot_chat(
    prompt: str,
    *,
    model: str,
    system: str,
    timeout: float,
) -> str:
    """Call the Copilot SDK using gh keyring-backed github.com authentication."""
    from copilot import CopilotClient  # type: ignore[import]

    session_cfg: dict[str, Any] = {"model": model}
    logger.debug("LLM: Copilot SDK via GitHub Copilot CLI (gh auth keyring)")

    if system:
        session_cfg["system_message"] = {"content": system, "role": "system"}

    # Disable infinite sessions for simple one-shot calls
    session_cfg["infinite_sessions"] = {"enabled": False}

    client = CopilotClient({"log_level": "error"})
    await client.start()
    session = None
    try:
        session = await client.create_session(cast(Any, session_cfg))
        response = await asyncio.wait_for(
            session.send_and_wait({"prompt": prompt}),
            timeout=timeout,
        )
        # Guard against empty response from Copilot SDK
        content = (response.data.content if response and response.data else "") or ""
        if not content or not content.strip():
            logger.warning(
                "Copilot SDK returned empty response. Check: copilot CLI installed, "
                "and 'gh auth login' authenticated on github.com."
            )
        return content
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
# Session config helper (for agents that manage sessions themselves)   #
# ------------------------------------------------------------------ #


def build_session_config(model: str, *, system: str = "") -> dict:
    """Return a ``create_session`` config dict for Copilot SDK gh-auth sessions.

    Prefer :func:`async_llm_chat` for simple one-shot calls; use this only
    when you need to manage the session lifecycle yourself.
    """
    cfg: dict = {"model": model, "infinite_sessions": {"enabled": False}}
    if system:
        cfg["system_message"] = {"content": system, "role": "system"}
    return cfg
