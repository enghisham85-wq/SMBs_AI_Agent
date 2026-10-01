"""Resolve the one project key used by this Python process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional
from boosthis.start_announce import warn_project_key_refused


class ProjectKeySource(str, Enum):
    ENV_RUNTIME = "env-runtime"
    CODE = "code"
    CONFIG_RUNTIME = "config-runtime"
    ENV_SHARED = "env-shared"
    ENV_LEGACY = "env-legacy"
    NONE = "none"


@dataclass(frozen=True)
class ResolvedProjectKey:
    key: Optional[str]
    source: ProjectKeySource
    overrode_shared: bool
    display: Optional[str]
    no_key_chosen: bool = False
    no_key_absent: bool = False


def _clean(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _accepted(value: object, source: str) -> Optional[str]:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    return None if warn_project_key_refused(cleaned, source) else cleaned


def mask_project_key(value: object) -> Optional[str]:
    key = _clean(value)
    if key is None:
        return None
    return f"…{key[-4:]}" if len(key) >= 8 else "set"


def resolve_project_key(
    explicit: Optional[str] = None,
    configured: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ResolvedProjectKey:
    env = os.environ if environ is None else environ
    runtime = _accepted(env.get("BOOSTHIS_PROJECT_KEY_PY"), "BOOSTHIS_PROJECT_KEY_PY") or _accepted(
        env.get("BOOSTHIS_PROJECT_KEY_PYTHON"), "BOOSTHIS_PROJECT_KEY_PYTHON"
    )
    code = _accepted(explicit, "the key passed in code")
    config = _accepted(configured, "~/.boosthis/config.json projectKey")
    shared = _accepted(env.get("BOOSTHIS_PROJECT_KEY"), "BOOSTHIS_PROJECT_KEY")
    legacy = _accepted(env.get("BOOSTHIS_INVITE_KEY"), "BOOSTHIS_INVITE_KEY") or _accepted(
        env.get("BOOSTEN_INVITE_KEY"), "BOOSTEN_INVITE_KEY"
    )
    if runtime:
        replaced = code or config or shared or legacy
        return ResolvedProjectKey(
            runtime,
            ProjectKeySource.ENV_RUNTIME,
            bool(replaced and replaced != runtime),
            mask_project_key(runtime),
        )
    choices = (
        (code, ProjectKeySource.CODE),
        (config, ProjectKeySource.CONFIG_RUNTIME),
        (shared, ProjectKeySource.ENV_SHARED),
        (legacy, ProjectKeySource.ENV_LEGACY),
    )
    for key, source in choices:
        if key:
            return ResolvedProjectKey(key, source, False, mask_project_key(key))
    switch = str(env.get("BOOSTHIS_NO_PROJECT_KEY", "")).strip().lower()
    absent = not any(_clean(v) for v in (env.get("BOOSTHIS_PROJECT_KEY_PY"), env.get("BOOSTHIS_PROJECT_KEY_PYTHON"), explicit, configured, env.get("BOOSTHIS_PROJECT_KEY"), env.get("BOOSTHIS_INVITE_KEY"), env.get("BOOSTEN_INVITE_KEY")))
    return ResolvedProjectKey(None, ProjectKeySource.NONE, False, None, switch in {"1", "true", "yes", "on"}, absent)


def describe_project_key_source(source: ProjectKeySource) -> str:
    return {
        ProjectKeySource.ENV_RUNTIME: "Python key from the environment",
        ProjectKeySource.CODE: "key passed in code",
        ProjectKeySource.CONFIG_RUNTIME: "Python key from ~/.boosthis/config.json",
        ProjectKeySource.ENV_SHARED: "shared key from BOOSTHIS_PROJECT_KEY",
        ProjectKeySource.ENV_LEGACY: "shared key from BOOSTHIS_INVITE_KEY",
        ProjectKeySource.NONE: "no project key configured",
    }[source]