"""Ephemeral Git authentication.

Credentials are injected only into Git subprocess configuration and are never
persisted in .git/config or exposed to the coding agent's shell environment.
"""
from __future__ import annotations

import base64
import os
from typing import Mapping

from minion.config import Settings


def git_environment(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    if settings.github_token:
        raw = f"x-access-token:{settings.github_token}".encode()
        encoded = base64.b64encode(raw).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
    return env


def safe_child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(base or os.environ)
    blocked_exact = {"GITHUB_TOKEN", "OPENAI_API_KEY", "MINION_API_TOKEN"}
    return {
        key: value
        for key, value in source.items()
        if key not in blocked_exact and not key.startswith("GIT_CONFIG_")
    }
