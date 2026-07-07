"""RuTracker client: guest-friendly topic scraping (login optional).

Update detection without login:
  The topic's first post shows a creation time and, when the uploader adds new
  episodes, an *edit* time — e.g.:
      "02-Июн-26 18:54  (1 месяц 4 дня назад, ред. 06-Июл-26 19:09)"
  and the title carries an episode range, e.g. "Серии: 1-7 из 10".
  Both are visible to guests, so we watch:  edit-date  +  episode-range.

If RuTracker credentials are provided we also try to read the torrent's
"Зарегистрирован" (registered) date as an extra signal, but it's optional.
"""

from __future__ import annotations

import logging
import os
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

COOKIE_FILE = Path(__file__).with_name("rutracker_cookies.pkl")
_ENCODING = "cp1251"  # rutracker serves cp1251-encoded pages

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# dd-Mon-yy HH:MM  (Mon is a Russian abbreviation like Июн, Июл, ...)
_DATE_RE = r"[0-3]?\d-[А-Яа-яA-Za-z]{3}-\d{2}(?:\s+\d{1,2}:\d{2})?"


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

    def signature(self) -> str:
        """String compared between checks to decide if new episodes appeared.

        Prefers guest-visible signals (edit date + episode range). Falls back
        to creation date, and folds in the registered date/size when available.
        """
        primary = self.edited or self.created
        return "|".join([primary, self.episodes, self.registered, self.size])


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


class RutrackerClient:
    def __init__(self, username: str = "", password: str = "",
                 base: str = "https://rutracker.org"):
        self.username = username
        self.password = password
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        self._load_cookies()

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    # ------------------------------------------------------------------ cookies
    def _load_cookies(self) -> None:
        if COOKIE_FILE.exists():
            try:
                with open(COOKIE_FILE, "rb") as fh:
                    self.session.cookies.update(pickle.load(fh))
            except Exception as e:  # noqa: BLE001
                log.warning("Could not load cookies: %s", e)

    def _save_cookies(self) -> None:
        try:
            with open(COOKIE_FILE, "wb") as fh:
                pickle.dump(self.session.cookies, fh)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not save cookies: %s", e)

    # ------------------------------------------------------------------- login
    def login(self, force: bool = False) -> None:
        if not self.has_credentials:
            return
        if not force and self.session.cookies.get("bb_session"):
            return
        url = f"{self.base}/forum/login.php"
        data = {
            "login_username": self.username,
            "login_password": self.password,
            "login": "Вход",
        }
        try:
            resp = self.session.post(url, data=data, timeout=30)
        except requests.RequestException as e:
            raise RutrackerError(f"Network error during login: {e}") from e
        resp.encoding = _ENCODING
        if not self.session.cookies.get("bb_session"):
            raise RutrackerError(
                "Login failed — check credentials or a captcha/blocked mirror. "
                "Tracking still works without login using the post edit date."
            )
        self._save_cookies()
        log.info("Logged in to RuTracker as %s", self.username)

    # -------------------------------------------------------------- fetch topic
    def _get(self, topic_id: str) -> str:
        url = f"{self.base}/forum/viewtopic.php?t={topic_id}"
        resp = self.session.get(url, timeout=30)
        resp.encoding = _ENCODING
        return resp.text

    def fetch_topic(self, topic_id: str) -> TopicInfo:
        """Fetch a topic as a guest; use login only if credentials are set."""
        if self.has_credentials:
            try:
                self.login()
            except RutrackerError as e:
                log.warning("%s — continuing as guest", e)
        html = self._get(topic_id)
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

        text = soup.get_text("\n", strip=True)

        # ---- first-post creation + edit dates
        # Prefer the dedicated post-time element if present.
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
        episodes = ""
        m_ep = re.search(
            r"(?:Серии|Серия|Эпизоды|Episodes?)\s*:?\s*([\d]+\s*-\s*[\d]+(?:\s*из\s*[\d]+)?)",
            title, re.IGNORECASE,
        )
        if m_ep:
            episodes = re.sub(r"\s+", " ", m_ep.group(1)).strip()

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

        if not title:
            raise RutrackerError(
                f"Could not parse topic {topic_id} — wrong id, or the mirror "
                "returned a block/captcha page. Try another RUTRACKER_BASE."
            )

        return TopicInfo(topic_id=topic_id, title=title, created=created,
                         edited=edited, episodes=episodes,
                         registered=registered, size=size)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def build_client_from_env() -> RutrackerClient:
    return RutrackerClient(
        username=os.getenv("RUTRACKER_USERNAME", ""),
        password=os.getenv("RUTRACKER_PASSWORD", ""),
        base=os.getenv("RUTRACKER_BASE", "https://rutracker.org"),
    )
