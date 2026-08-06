"""RuTracker client backed by the official JSON API (no HTML, no Cloudflare).

Why the API:
  Scraping viewtopic.php stopped working — rutracker.org sits behind a
  Cloudflare challenge, so a plain requests.get() gets the "Just a moment..."
  interstitial instead of the topic. The tracker's own read-only API at
  api.rutracker.org is not behind that challenge and needs no login.

Endpoint used:
  GET /v1/get_tor_topic_data?by=topic_id&val=<id>[,<id>...]
  ->  {"result": {"<id>": {"info_hash": ..., "forum_id": ..., "size": ...,
                           "reg_time": <unix>, "tor_status": ...,
                           "seeders": ..., "topic_title": ...}}}
  A missing/unknown id comes back as null.

Update detection:
  reg_time is the moment the *current* .torrent file was registered. When the
  uploader adds new episodes they re-upload the torrent, so reg_time bumps.
  That is a stronger signal than the old post-edit-date heuristic. We also fold
  in the episode range parsed out of topic_title (e.g. "Серии: 1-7 из 10") and
  the size, so a silent re-pack still trips the check.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

_UA = "rutracker-show-tracker/2.0 (+https://github.com/)"

# The API accepts up to 100 ids per request.
MAX_IDS_PER_REQUEST = 100


class RutrackerError(Exception):
    pass


@dataclass
class TopicInfo:
    topic_id: str
    title: str
    reg_time: int = 0        # unix ts the current .torrent was registered
    episodes: str = ""       # e.g. "1-7 из 10", parsed from the title
    size: int = 0            # bytes
    seeders: int = 0
    status: int = 0          # tor_status (2 = "проверено", etc.)
    info_hash: str = ""
    forum_id: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def registered(self) -> str:
        """reg_time as a readable local-ish string, '' if unknown."""
        if not self.reg_time:
            return ""
        return datetime.fromtimestamp(self.reg_time, timezone.utc).strftime(
            "%d-%m-%Y %H:%M UTC"
        )

    @property
    def size_human(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
        return ""

    def signature(self) -> str:
        """Compared between checks to decide whether the release changed."""
        return "|".join([str(self.reg_time), self.episodes, str(self.size)])


def extract_topic_id(url_or_id: str) -> Optional[str]:
    """Accept a full topic URL or a bare numeric id, return the id string."""
    s = url_or_id.strip()
    if s.isdigit():
        return s
    m = re.search(r"[?&]t=(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/topic/(\d+)", s)
    if m:
        return m.group(1)
    return None


_EP_RE = re.compile(
    r"(?:Серии|Серия|Эпизоды|Эпизод|Episodes?|Series|Ep)\s*\.?\s*:?\s*"
    r"(\d+\s*-\s*\d+(?:\s*(?:из|of)\s*\d+)?)",
    re.IGNORECASE,
)


def parse_episodes(title: str) -> str:
    m = _EP_RE.search(title or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


class RutrackerClient:
    """Read-only client for the RuTracker JSON API."""

    def __init__(self,
                 api_base: str = "https://api.rutracker.org/v1",
                 site_base: str = "https://rutracker.org",
                 timeout: int = 30):
        self.api_base = api_base.rstrip("/")
        self.site_base = site_base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json",
        })

    # kept so bot.py can still build topic links
    @property
    def base(self) -> str:
        return self.site_base

    def topic_url(self, topic_id: str) -> str:
        return f"{self.site_base}/forum/viewtopic.php?t={topic_id}"

    # ------------------------------------------------------------------ request
    def _call(self, method: str, **params) -> dict:
        url = f"{self.api_base}/{method}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise RutrackerError(f"Network error calling {method}: {e}") from e

        if resp.status_code != 200:
            raise RutrackerError(
                f"{method} returned HTTP {resp.status_code}. "
                "The API mirror may be down — try RUTRACKER_API_BASE="
                "https://api.t-ru.org/v1"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            head = resp.text[:120].replace("\n", " ")
            raise RutrackerError(
                f"{method} did not return JSON (got: {head!r}). "
                "If this says 'Just a moment' you're hitting the website, "
                "not the API — check RUTRACKER_API_BASE."
            ) from e

        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            raise RutrackerError(
                f"API error {err.get('code')}: {err.get('text')}"
            )
        return payload.get("result") or {}

    # ---------------------------------------------------------------- public API
    def get_limit(self) -> int:
        """Max ids the API accepts per request (usually 100)."""
        res = self._call("get_limit")
        try:
            return int(res.get("limit", MAX_IDS_PER_REQUEST))
        except (TypeError, ValueError):
            return MAX_IDS_PER_REQUEST

    def fetch_topics(self, topic_ids: Iterable[str]) -> Dict[str, TopicInfo]:
        """Fetch many topics at once. Unknown ids are simply absent from the
        returned dict."""
        ids: List[str] = [str(t).strip() for t in topic_ids if str(t).strip()]
        out: Dict[str, TopicInfo] = {}
        for i in range(0, len(ids), MAX_IDS_PER_REQUEST):
            chunk = ids[i:i + MAX_IDS_PER_REQUEST]
            result = self._call("get_tor_topic_data",
                                by="topic_id", val=",".join(chunk))
            for tid, data in (result or {}).items():
                if not data:          # null == no such topic / no torrent
                    continue
                out[str(tid)] = self._to_info(str(tid), data)
        return out

    def fetch_topic(self, topic_id: str) -> TopicInfo:
        """Fetch a single topic. Raises if the id is unknown."""
        topics = self.fetch_topics([topic_id])
        info = topics.get(str(topic_id))
        if info is None:
            raise RutrackerError(
                f"Topic {topic_id} not found (or it has no torrent attached). "
                "Double-check the viewtopic.php?t=... id."
            )
        return info

    # ------------------------------------------------------------------- mapping
    @staticmethod
    def _to_info(topic_id: str, data: dict) -> TopicInfo:
        title = str(data.get("topic_title") or "").strip()
        return TopicInfo(
            topic_id=topic_id,
            title=title,
            reg_time=_int(data.get("reg_time")),
            episodes=parse_episodes(title),
            size=_int(data.get("size")),
            seeders=_int(data.get("seeders")),
            status=_int(data.get("tor_status")),
            info_hash=str(data.get("info_hash") or ""),
            forum_id=_int(data.get("forum_id")),
            raw=data,
        )


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def build_client_from_env() -> RutrackerClient:
    return RutrackerClient(
        api_base=os.getenv("RUTRACKER_API_BASE", "https://api.rutracker.org/v1"),
        site_base=os.getenv("RUTRACKER_BASE", "https://rutracker.org"),
    )


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = sys.argv[1:] or ["6866086"]
    c = build_client_from_env()
    print(f"API base: {c.api_base}")
    try:
        print(f"Per-request id limit: {c.get_limit()}")
    except RutrackerError as e:
        print(f"get_limit failed: {e}")

    try:
        topics = c.fetch_topics(extract_topic_id(a) or a for a in args)
    except RutrackerError as e:
        raise SystemExit(f"FAILED: {e}")

    if not topics:
        raise SystemExit("No topics returned — check the ids.")
    for tid, t in topics.items():
        print("-" * 70)
        print(f"id        {tid}")
        print(f"title     {t.title}")
        print(f"episodes  {t.episodes or '-'}")
        print(f"reg_time  {t.reg_time}  ({t.registered})")
        print(f"size      {t.size_human}")
        print(f"seeders   {t.seeders}")
        print(f"signature {t.signature()}")
