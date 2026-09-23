"""Hermes Monitor has no agent-side capabilities.

The plugin ships a Desktop status-bar UI (``desktop/plugin.js``) and read-only
dashboard routes (``dashboard/plugin_api.py``). This module exists so the agent
plugin loader can import the package without registering anything.
"""


def register(ctx) -> None:  # noqa: ARG001 - intentionally registers nothing
    return None
