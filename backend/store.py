"""Tiny JSON persistence for the app's personal layer.

Holds the watchlist and per-ticker research state (notes, digests, generated
research note, applied assumptions) in one file under the project's `data/`
directory, so research survives app restarts. Single-process, lock-guarded —
deliberately simple; this is a personal tool, not a multi-tenant service.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STORE_PATH = _DATA_DIR / "copilot_store.json"
_lock = threading.Lock()

_MAX_DIGESTS_PER_TICKER = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("watchlist", [])
            data.setdefault("research", {})
            return data
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable store: preserve it for recovery instead of
        # letting the next save silently wipe the user's research.
        try:
            os.replace(_STORE_PATH, str(_STORE_PATH) + ".corrupt")
        except OSError:
            pass
    return {"watchlist": [], "research": {}}


def _save(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = str(_STORE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, _STORE_PATH)


# --- watchlist -------------------------------------------------------------- #
def get_watchlist() -> list[dict]:
    with _lock:
        return _load()["watchlist"]


def upsert_watchlist(snapshot: dict) -> list[dict]:
    """Add or refresh one ticker's snapshot {ticker, name, currency, price,
    blended_target, recommendation}."""
    ticker = str(snapshot.get("ticker", "")).upper().strip()
    if not ticker:
        return get_watchlist()
    entry = {
        "ticker": ticker,
        "name": snapshot.get("name"),
        "currency": snapshot.get("currency"),
        "price": snapshot.get("price"),
        "blended_target": snapshot.get("blended_target"),
        "recommendation": snapshot.get("recommendation"),
        "updated_at": _now(),
    }
    with _lock:
        data = _load()
        data["watchlist"] = [w for w in data["watchlist"] if w.get("ticker") != ticker]
        data["watchlist"].insert(0, entry)
        _save(data)
        return data["watchlist"]


def remove_watchlist(ticker: str) -> list[dict]:
    ticker = ticker.upper().strip()
    with _lock:
        data = _load()
        data["watchlist"] = [w for w in data["watchlist"] if w.get("ticker") != ticker]
        _save(data)
        return data["watchlist"]


# --- per-ticker research state ---------------------------------------------- #
def get_research(ticker: str) -> dict:
    with _lock:
        return _load()["research"].get(ticker.upper().strip(), {})


def save_research(ticker: str, state: dict) -> dict:
    """Persist {notes, digests, note, assumptions} for a ticker (partial ok)."""
    ticker = ticker.upper().strip()
    with _lock:
        data = _load()
        cur = data["research"].get(ticker, {})
        for key in ("notes", "digests", "note", "assumptions"):
            if key in state and state[key] is not None:
                cur[key] = state[key]
        digests = cur.get("digests")
        if isinstance(digests, list) and len(digests) > _MAX_DIGESTS_PER_TICKER:
            cur["digests"] = digests[-_MAX_DIGESTS_PER_TICKER:]
        cur["updated_at"] = _now()
        data["research"][ticker] = cur
        _save(data)
        return cur
