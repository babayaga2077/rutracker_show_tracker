"""RuTracker client: guest-visible topic scraping through a Cloudflare bypass.

History of this file, so the design makes sense:
  1. Plain `requests` scraping — died when rutracker.org put the whole forum
     behind a Cloudflare JS challenge ("Just a moment...").
  2. The official JSON API at api.rutracker.org — clean, but the host has since
     been retired (it no longer resolves).
  3. Back to scraping, this time through curl_cffi + FlareSolverr. See
     cloudflare.py for how the challenge is cleared.

Update detection (all guest-visible, no account needed)
------------------------------------------------------
The topic's first post carries a creation time and, once the uploader adds new
episodes, an *edit* time:

    "02-Июн-26 18:54  (1 месяц 4 дня назад, ред. 06-Июл-26 19:09)"

and the title carries the episode range, e.g. "Серии: 1-7 из 10". We watch the
edit date, the episode range and the size together.

Caveat worth knowing: the edit date bumps for *any* edit, so an uploader fixing
a typo in the description will produce a false "new episodes" alert. The old
API's reg_time did not have this problem, but it is gone. Logging in exposes
the torrent's "Зарегистрирован" date, which is the accurate signal — so if
credentials are set we fold that in and the false alarms stop.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from cloudflare import ChallengeError, CloudflareSession, describe_transport

log = logging.getLogger(__name__)

# dd-Mon-yy HH:MM  (Mon is a Russian abbreviation like Июн, Июл, ...)
_DATE_RE = r"[0-3]?\d-[А-Яа-яA-Za-z]{3}-\d{2}(?:\s+\d{1,2}:\d{2})?"

_EP_RE = re.compile(
    r"(?:Серии|Серия|Эпизоды|Эпизод|Episodes?|Series|Ep)\s*\.?\s*:?\s*"
    r"(\d+\s*-\s*\d+(?:\s*(?:из|of)\s*\d+)?)",
    re.IGNORECASE,
)


class RutrackerError(Exception):
    pass


@dataclass
class TopicInfo:
    topic_id: str
    title: str
    created: str = ""       # first-post creation timestamp
    edited: str = ""        # first-post last-edit timestamp ("ред. ...")
    episodes: str = ""      # e.g. "1-7 из 10" parsed from the title
    registered: str = ""    # torrent "Зарегистрирован" date (needs login)
    size: str = ""

    @property
    def stamp(self) -> str:
        """Best available human-readable 'last changed' marker."""
        return self.registered or self.edited or self.created or "n/a"

    def signature(self) -> str:
        """Compared between checks to decide if new episodes appeared.

        Prefers the accurate registered date when we have it (login), otherwise
        falls back to the guest-visible edit date.
        """
        primary = self.registered or self.edited or self.created
        return "|".join([primary, self.episodes, self.size])


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


def parse_episodes(title: str) -> str:
    m = _EP_RE.search(title or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


class RutrackerClient:
    def __init__(self, base: str = "https://rutracker.org",
                 flaresolverr_url: str = ""):
        self.base = base.rstrip("/")
        self.session = CloudflareSession(flaresolverr_url=flaresolverr_url)

    def topic_url(self, topic_id: str) -> str:
        return f"{self.base}/forum/viewtopic.php?t={topic_id}"

    def transport_summary(self) -> str:
        return describe_transport(self.session)

    # -------------------------------------------------------------- fetch topic
    def fetch_topic(self, topic_id: str) -> TopicInfo:
        url = self.topic_url(topic_id)
        try:
            html = self.session.get(url)
        except ChallengeError as e:
            raise RutrackerError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise RutrackerError(f"Network error fetching topic {topic_id}: {e}") from e
        return self._parse(topic_id, html)

    # ------------------------------------------------------------------- parse
    @staticmethod
    def _parse(topic_id: str, html: str) -> TopicInfo:
        soup = BeautifulSoup(html, "html.parser")

        # ---- title
        title = ""
        h1 = soup.select_one("h1.maintitle, a.maintitle, #topic-title")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title:
            t = soup.find("title")
            if t:
                title = t.get_text(strip=True)
        title = re.sub(r"\s*::\s*RuTracker.*$", "", title).strip()

        # Guard against silently storing a challenge/error page as the title.
        if not title or title.lower().startswith(("just a moment", "attention required",
                                                  "access denied", "error")):
            raise RutrackerError(
                f"Could not parse topic {topic_id} — got a block/challenge page "
                "instead of the topic. Check that FlareSolverr is running."
            )

        text = soup.get_text("\n", strip=True)

        # ---- first-post creation + edit dates
        created = edited = ""
        pt = soup.select_one("p.post-time, .post_head .p-link, span.p-link")
        pt_text = pt.get_text(" ", strip=True) if pt else ""
        scope = pt_text or text

        m_edit = re.search(r"ред\.\s*(" + _DATE_RE + r")", scope)
        if m_edit:
            edited = _norm(m_edit.group(1))
        m_created = re.search(_DATE_RE, scope)
        if m_created:
            created = _norm(m_created.group(0))

        # ---- episode range from the title, e.g. "Серии: 1-7 из 10"
        episodes = parse_episodes(title)

        # ---- torrent registered date (only visible when logged in)
        registered = ""
        m_reg = re.search(r"Зарегистрирован\s*\[?\s*(" + _DATE_RE + r")", text)
        if m_reg:
            registered = _norm(m_reg.group(1))

        # ---- size
        size = ""
        ms = re.search(r"(?:Размер|Size)\s*[:\s]\s*([\d.,]+\s?[КМГKMGТ]?i?B)", text)
        if ms:
            size = ms.group(1).strip()

        return TopicInfo(topic_id=topic_id, title=title, created=created,
                         edited=edited, episodes=episodes,
                         registered=registered, size=size)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def build_client_from_env() -> RutrackerClient:
    return RutrackerClient(
        base=os.getenv("RUTRACKER_BASE", "https://rutracker.org"),
        flaresolverr_url=os.getenv("FLARESOLVERR_URL", ""),
    )


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    import sys

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = sys.argv[1:] or ["6866086"]
    c = build_client_from_env()
    print(f"Site:      {c.base}")
    print(f"Transport: {c.transport_summary()}")
    print()

    failures = 0
    for a in args:
        tid = extract_topic_id(a) or a
        try:
            t = c.fetch_topic(tid)
        except RutrackerError as e:
            failures += 1
            print(f"FAILED {tid}: {e}")
            continue
        print("-" * 70)
        print(f"id         {t.topic_id}")
        print(f"title      {t.title}")
        print(f"episodes   {t.episodes or '-'}")
        print(f"created    {t.created or '-'}")
        print(f"edited     {t.edited or '-'}")
        print(f"registered {t.registered or '- (needs login)'}")
        print(f"size       {t.size or '-'}")
        print(f"signature  {t.signature()}")
    sys.exit(1 if failures else 0)
