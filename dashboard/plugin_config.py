"""Plugin settings: the pinned candidate list and the subscription's flat cost."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "cost_arbitrage_config.json"

#: Seeded with the models measured in production plus cheap open alternatives
#: spanning the interesting price range.
DEFAULT_CONFIG: dict[str, Any] = {
    "subscription_usd_per_month": 23.0,
    "pinned": [
        {"provider": "openai", "model": "gpt-5.6-terra"},
        {"provider": "openai", "model": "gpt-5.5"},
        {"provider": "anthropic", "model": "claude-sonnet-5"},
        {"provider": "anthropic", "model": "claude-haiku-4-5"},
        {"provider": "openrouter", "model": "z-ai/glm-5"},
        {"provider": "openrouter", "model": "moonshotai/kimi-k2.5"},
        {"provider": "openrouter", "model": "minimax/minimax-m2.7"},
    ],
}


def config_path() -> Path:
    """``$HERMES_HOME/cost_arbitrage_config.json``."""
    try:
        from hermes_constants import get_hermes_home

        base = Path(get_hermes_home())
    except Exception:
        home = (os.environ.get("HERMES_HOME") or "").strip()
        base = Path(home) if home else Path.home() / ".hermes"
    return base / CONFIG_FILENAME


def _clean_pinned(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return [dict(entry) for entry in DEFAULT_CONFIG["pinned"]]
    cleaned = [
        {"provider": str(entry["provider"]), "model": str(entry["model"])}
        for entry in raw
        if isinstance(entry, dict) and entry.get("provider") and entry.get("model")
    ]
    return cleaned


def _normalize(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    try:
        subscription = float(data.get("subscription_usd_per_month", DEFAULT_CONFIG["subscription_usd_per_month"]))
    except (TypeError, ValueError):
        subscription = float(DEFAULT_CONFIG["subscription_usd_per_month"])
    pinned = _clean_pinned(data["pinned"]) if "pinned" in data else [dict(e) for e in DEFAULT_CONFIG["pinned"]]
    if not pinned:
        pinned = [dict(entry) for entry in DEFAULT_CONFIG["pinned"]]
    return {"subscription_usd_per_month": subscription, "pinned": pinned}


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load settings, falling back to defaults on any failure."""
    target = Path(path) if path is not None else config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return _normalize(json.load(handle))
    except Exception:
        return _normalize(None)


def save_config(path: Path | str | None, data: dict[str, Any]) -> dict[str, Any]:
    """Normalize, persist and return the settings.

    Writes are atomic: the payload lands on a temporary file in the same
    directory as ``target`` and is moved into place with ``os.replace``, so a
    process crash or a write failure partway through can never leave the
    real config file truncated. If the write fails, the temporary file is
    removed and the exception propagates.
    """
    target = Path(path) if path is not None else config_path()
    normalized = _normalize(data)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return normalized
