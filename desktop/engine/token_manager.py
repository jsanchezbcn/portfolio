from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

from desktop.config.preferences import load_preferences, save_preferences

# Backward-compatible constant name used in existing UI/tests.
# Values now point to github.com usernames for profile-based gh auth switching.
PROFILE_TOKEN_KEYS: dict[str, str] = {
    "personal": "GITHUB_COPILOT_PERSONAL",
    "work": "GITHUB_COPILOT_WORK",
}
LEGACY_PROFILE_TOKEN_KEYS: dict[str, str] = {
    "personal": "GITHUB_COPILOT_TOKEN_PERSONAL",
    "work": "GITHUB_COPILOT_TOKEN_WORK",
}
ACTIVE_PROFILE_ENV = "GITHUB_COPILOT_ACTIVE_PROFILE"
# Backward-compatible env var name kept for compatibility; stores active account username.
ACTIVE_TOKEN_ENV = "GITHUB_COPILOT_ACTIVE_TOKEN"


def _is_token_like(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith(("gho_", "ghp_", "github_pat_", "sk-"))


@dataclass(frozen=True)
class TokenState:
    profile: str
    token_env_var: str
    token_value: str

    @property
    def token_available(self) -> bool:
        return bool(self.token_value)


class TokenManager:
    def __init__(
        self,
        *,
        preferences_path: str | Path | None = None,
        environ: MutableMapping[str, str] | None = None,
    ):
        self._preferences_path = Path(preferences_path) if preferences_path is not None else None
        self._environ = environ if environ is not None else os.environ
        self._active_state = self.initialize()

    @property
    def active_profile(self) -> str:
        return self._active_state.profile

    @property
    def active_token(self) -> str:
        return self._active_state.token_value

    @property
    def active_token_env_var(self) -> str:
        return self._active_state.token_env_var

    def initialize(self) -> TokenState:
        prefs = load_preferences(self._preferences_path)
        profile = self._normalize_profile(str(prefs.get("copilot_profile", "personal")))
        return self.set_active_profile(profile, persist=False)

    def available_profiles(self) -> tuple[str, str]:
        return ("personal", "work")

    def set_active_profile(self, profile: str, *, persist: bool = True) -> TokenState:
        normalized = self._normalize_profile(profile)
        token_env_var = PROFILE_TOKEN_KEYS[normalized]
        primary_value = (self._environ.get(token_env_var) or "").strip()
        token_value = primary_value if primary_value and not _is_token_like(primary_value) else ""
        if not token_value:
            legacy_key = LEGACY_PROFILE_TOKEN_KEYS[normalized]
            legacy_value = (self._environ.get(legacy_key) or "").strip()
            token_value = legacy_value if legacy_value and not _is_token_like(legacy_value) else ""
        self._environ[ACTIVE_PROFILE_ENV] = normalized
        self._environ[ACTIVE_TOKEN_ENV] = token_value
        state = TokenState(profile=normalized, token_env_var=token_env_var, token_value=token_value)
        self._active_state = state
        if persist:
            save_preferences({"copilot_profile": normalized}, self._preferences_path)
        return state

    def state_for(self, profile: str) -> TokenState:
        normalized = self._normalize_profile(profile)
        token_env_var = PROFILE_TOKEN_KEYS[normalized]
        primary_value = (self._environ.get(token_env_var) or "").strip()
        token_value = primary_value if primary_value and not _is_token_like(primary_value) else ""
        if not token_value:
            legacy_key = LEGACY_PROFILE_TOKEN_KEYS[normalized]
            legacy_value = (self._environ.get(legacy_key) or "").strip()
            token_value = legacy_value if legacy_value and not _is_token_like(legacy_value) else ""
        return TokenState(profile=normalized, token_env_var=token_env_var, token_value=token_value)

    def has_configured_token(self, profile: str | None = None) -> bool:
        state = self._active_state if profile is None else self.state_for(profile)
        return state.token_available

    @staticmethod
    def active_state_from_env(environ: Mapping[str, str] | None = None) -> TokenState:
        env = environ if environ is not None else os.environ
        profile = str(env.get(ACTIVE_PROFILE_ENV) or "personal")
        normalized = TokenManager._normalize_profile(profile)
        token_env_var = PROFILE_TOKEN_KEYS[normalized]
        legacy_key = LEGACY_PROFILE_TOKEN_KEYS[normalized]
        raw_token_value = str(
            env.get(ACTIVE_TOKEN_ENV)
            or env.get(token_env_var)
            or env.get(legacy_key)
            or ""
        ).strip()
        token_value = raw_token_value if raw_token_value and not _is_token_like(raw_token_value) else ""
        return TokenState(profile=normalized, token_env_var=token_env_var, token_value=token_value)

    @staticmethod
    def _normalize_profile(profile: str) -> str:
        normalized = profile.strip().lower()
        if normalized not in PROFILE_TOKEN_KEYS:
            return "personal"
        return normalized
