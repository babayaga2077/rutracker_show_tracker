"""Tiny JSON-backed store for tracked topics and their last-seen state.

Structure (data.json):
{
  "subs": {
    "<topic_id>": {
      "title": "...",
      "last_signature": "updated|size",
      "last_updated": "raw date string",
      "chats": [123, 456]        # telegram chat ids subscribed
    }
  }
}
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, List

DATA_FILE = Path(__file__).with_name("data.json")
_lock = threading.Lock()


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"subs": {}}


def _save(data: dict) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def add_subscription(topic_id: str, chat_id: int, title: str,
                     signature: str, updated: str) -> bool:
    """Returns True if this chat was newly subscribed."""
    with _lock:
        data = _load()
        sub = data["subs"].get(topic_id)
        if sub is None:
            sub = {"title": title, "last_signature": signature,
                   "last_updated": updated, "chats": []}
            data["subs"][topic_id] = sub
        else:
            sub["title"] = title or sub.get("title", "")
        newly = chat_id not in sub["chats"]
        if newly:
            sub["chats"].append(chat_id)
        _save(data)
        return newly


def remove_subscription(topic_id: str, chat_id: int) -> bool:
    """Remove a chat from a topic. Returns True if it was subscribed."""
    with _lock:
        data = _load()
        sub = data["subs"].get(topic_id)
        if not sub or chat_id not in sub["chats"]:
            return False
        sub["chats"].remove(chat_id)
        if not sub["chats"]:
            del data["subs"][topic_id]
        _save(data)
        return True


def list_subscriptions(chat_id: int) -> List[dict]:
    data = _load()
    out = []
    for tid, sub in data["subs"].items():
        if chat_id in sub["chats"]:
            out.append({"topic_id": tid, "title": sub.get("title", ""),
                        "last_updated": sub.get("last_updated", "")})
    return out


def all_topics() -> Dict[str, dict]:
    return _load()["subs"]


def update_state(topic_id: str, signature: str, updated: str,
                 title: str = "") -> None:
    with _lock:
        data = _load()
        sub = data["subs"].get(topic_id)
        if sub is None:
            return
        sub["last_signature"] = signature
        sub["last_updated"] = updated
        if title:
            sub["title"] = title
        _save(data)
