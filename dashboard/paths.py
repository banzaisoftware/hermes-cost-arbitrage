"""Resolve ``$HERMES_HOME`` — the single copy of this chain.

Every module that needs a path under the Hermes home directory
(``store.py``, ``plugin_config.py``, ``plugin_api.py``) calls
:func:`hermes_home` rather than carrying its own copy of the fallback
chain. ``/opt/data`` in the target deployment.
"""
from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    """Resolve the Hermes home directory.

    Three tiers, in order:

    1. ``hermes_constants.get_hermes_home()`` when the host is importable.
    2. The ``HERMES_HOME`` environment variable.
    3. ``~/.hermes``.
    """
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        home = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(home) if home else Path.home() / ".hermes"
