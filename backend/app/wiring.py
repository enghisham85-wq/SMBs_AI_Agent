"""Registers every graph, action spec, daily step and event handler. Called at startup and in tests."""

from __future__ import annotations

import importlib

# Each module exposes `register()`. Order matters only for readability.
MODULES: list[str] = [
    "app.harness.graph",
    "app.graphs.daily_run",
    "app.seed.feed",
    "app.agents.stock.wiring",
    "app.agents.accountant.wiring",
]


def register_all() -> None:
    for name in MODULES:
        importlib.import_module(name).register()
