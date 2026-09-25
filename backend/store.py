"""Tiny JSON persistence for the app's personal layer.

Holds the watchlist and per-ticker research state (notes, digests, generated
research note, applied assumptions) in one file under the project's `data/`
directory, so research survives app restarts. Single-process, lock-guarded —
deliberately simple; this is a personal tool, not a multi-tenant service.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STORE_PATH = _DATA_DIR / "copilot_store.json"
_lock = threading.Lock()

_MAX_DIGESTS_PER_TICKER = 25


class StoreError(RuntimeError):
    """The store file exists but can't be read or written right now (e.g. a
    sync client or antivirus holds a lock). Surfaced as HTTP 503; the file is
    left untouched."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid(data) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("watchlist", []), list)
        and all(isinstance(w, dict) for w in data.get("watchlist", []))
        and isinstance(data.get("research", {}), dict)
        and all(isinstance(r, dict) for r in data.get("research", {}).values())
    )


def _quarantine() -> None:
    """Move an unusable store aside under a name that never overwrites an
    earlier backup, so the next save can't silently wipe the user's data."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{_STORE_PATH}.corrupt-{stamp}"
    dest, n = base, 1
    while os.path.exists(dest):
        dest, n = f"{base}-{n}", n + 1
    try:
        os.replace(_STORE_PATH, dest)
    except OSError as exc:
        raise StoreError(f"Research store is unreadable and could not be moved aside: {exc}") from exc


def _load() -> dict:
    try:
        with open(_STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"watchlist": [], "research": {}}
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Corrupt store: preserve it for recovery, start fresh.
        _quarantine()
        return {"watchlist": [], "research": {}}
    except OSError as exc:
        # Possibly transient (lock, permissions): don't touch a store that may
        # be perfectly valid.
        raise StoreError(f"Research store is temporarily unreadable: {exc}") from exc
    if not _valid(data):
        _quarantine()
        return {"watchlist": [], "research": {}}
    data.setdefault("watchlist", [])
    data.setdefault("research", {})
    return data


def _save(data: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_DATA_DIR), prefix=".copilot_store.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _STORE_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise StoreError(f"Could not save the research store: {exc}") from exc


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
