"""Cloudflare-aware HTTP transport for rutracker.org.

Background
----------
rutracker.org serves an interstitial JS challenge ("Just a moment...") to
non-browser clients. Two layers have to be satisfied:

  1. TLS/HTTP2 fingerprint. Plain `requests` has a giveaway handshake, so it is
     challenged on every request no matter what cookies it carries. `curl_cffi`
     impersonates a real Chrome handshake and fixes this half.
  2. The JS challenge itself. Passing it produces a `cf_clearance` cookie. That
     cannot be computed offline — it needs a real browser, which is what
     FlareSolverr provides.

Strategy
--------
FlareSolverr is slow (a solve is ~10-30s of headless Chrome) so we do not want
it in the hot path. Instead:

    * solve once via FlareSolverr -> stash cf_clearance + the exact User-Agent
    * make all normal requests with curl_cffi carrying that cookie (fast)
    * when a response comes back challenged, re-solve and retry once

`cf_clearance` is bound to the User-Agent, the IP, *and* the TLS fingerprint,
which is why we must reuse the UA FlareSolverr reports and must send it through
curl_cffi rather than plain requests. Mismatch any of the three and the cookie
is silently rejected.

The clearance cookie is cached on disk so a bot restart does not trigger a
fresh solve.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

CLEARANCE_FILE = Path(__file__).with_name("cf_clearance.json")

# Chrome build curl_cffi impersonates. Kept in one place because the UA we
# fall back to must be plausible for this fingerprint.
_IMPERSONATE = "chrome124"
_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Markers that mean "this is the challenge page, not the content".
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "cf_chl_opt",
    "checking your browser",
    "enable javascript and cookies",
)


class ChallengeError(Exception):
    """Raised when the Cloudflare challenge could not be cleared."""


def looks_challenged(status: int, body: str) -> bool:
    """True if this response is a Cloudflare interstitial rather than content."""
    if status in (403, 503):
        return True
    head = (body or "")[:4000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS)


class CloudflareSession:
    """Fetches pages from a Cloudflare-protected host.

    Falls back gracefully: if FlareSolverr is not configured we still try
    curl_cffi on its own, which is enough on days when Cloudflare is only doing
    passive fingerprinting.
    """

    def __init__(self,
                 flaresolverr_url: str = "",
                 solve_timeout: int = 60,
                 request_timeout: int = 30):
        self.flaresolverr_url = flaresolverr_url.rstrip("/")
        self.solve_timeout = solve_timeout
        self.request_timeout = request_timeout
        self._cookies: dict[str, str] = {}
        self._user_agent: str = _FALLBACK_UA
        self._load_clearance()

        try:
            from curl_cffi import requests as cffi
            self._cffi = cffi
        except ImportError:
            self._cffi = None
            log.warning(
                "curl_cffi is not installed — falling back to plain requests, "
                "which Cloudflare will almost certainly challenge. "
                "Install it with: pip install curl_cffi"
            )

    @property
    def has_solver(self) -> bool:
        return bool(self.flaresolverr_url)

    # ------------------------------------------------------------- persistence
    def _load_clearance(self) -> None:
        if not CLEARANCE_FILE.exists():
            return
        try:
            data = json.loads(CLEARANCE_FILE.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies") or {}
            self._user_agent = data.get("user_agent") or _FALLBACK_UA
            age = time.time() - data.get("saved_at", 0)
            log.info("Loaded cached Cloudflare clearance (%.0f min old)", age / 60)
        except (OSError, ValueError) as e:
            log.warning("Could not read %s: %s", CLEARANCE_FILE.name, e)

    def _save_clearance(self) -> None:
        try:
            CLEARANCE_FILE.write_text(
                json.dumps({
                    "cookies": self._cookies,
                    "user_agent": self._user_agent,
                    "saved_at": time.time(),
                }, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("Could not write %s: %s", CLEARANCE_FILE.name, e)

    # ------------------------------------------------------------------ solving
    def solve(self, url: str) -> None:
        """Ask FlareSolverr to clear the challenge and keep the cookies."""
        if not self.has_solver:
            raise ChallengeError(
                "Hit a Cloudflare challenge and no solver is configured. "
                "Start FlareSolverr and set FLARESOLVERR_URL "
                "(e.g. http://localhost:8191/v1)."
            )
        log.info("Solving Cloudflare challenge via FlareSolverr...")
        try:
            resp = requests.post(
                self.flaresolverr_url,
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": self.solve_timeout * 1000,
                },
                timeout=self.solve_timeout + 15,
            )
        except requests.RequestException as e:
            raise ChallengeError(
                f"Could not reach FlareSolverr at {self.flaresolverr_url}: {e}"
            ) from e

        try:
            payload = resp.json()
        except ValueError as e:
            raise ChallengeError(
                f"FlareSolverr returned non-JSON (HTTP {resp.status_code})"
            ) from e

        if payload.get("status") != "ok":
            raise ChallengeError(
                f"FlareSolverr failed: {payload.get('message', 'unknown error')}"
            )

        solution = payload.get("solution") or {}
        cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])
                   if "name" in c and "value" in c}
        if not cookies:
            raise ChallengeError("FlareSolverr returned no cookies")

        self._cookies = cookies
        # Must match exactly or the clearance cookie is rejected.
        self._user_agent = solution.get("userAgent") or self._user_agent
        self._save_clearance()
        log.info("Challenge cleared (%d cookies, cf_clearance=%s)",
                 len(cookies), "yes" if "cf_clearance" in cookies else "no")

    # ----------------------------------------------------------------- fetching
    def _raw_get(self, url: str) -> tuple[int, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        if self._cffi is not None:
            r = self._cffi.get(url, headers=headers, cookies=self._cookies,
                               impersonate=_IMPERSONATE,
                               timeout=self.request_timeout)
        else:
            r = requests.get(url, headers=headers, cookies=self._cookies,
                             timeout=self.request_timeout)
        # rutracker serves cp1251; curl_cffi and requests both guess badly.
        try:
            body = r.content.decode("cp1251", errors="replace")
        except Exception:  # noqa: BLE001
            body = r.text
        return r.status_code, body

    def get(self, url: str) -> str:
        """GET a page, transparently clearing the challenge if we hit one."""
        status, body = self._raw_get(url)
        if not looks_challenged(status, body):
            return body

        log.info("Challenged on %s (HTTP %s) — refreshing clearance", url, status)
        self.solve(url)
        status, body = self._raw_get(url)
        if looks_challenged(status, body):
            raise ChallengeError(
                f"Still challenged after solving (HTTP {status}). Cloudflare may "
                "have tightened up, or FlareSolverr's IP differs from the bot's "
                "— they must egress from the same address."
            )
        return body


def describe_transport(session: CloudflareSession) -> str:
    """Short human-readable summary, used by the self-test and /status."""
    parts = [
        f"curl_cffi: {'yes' if session._cffi else 'NO (install it)'}",
        f"FlareSolverr: {session.flaresolverr_url or 'not configured'}",
        f"cf_clearance cached: {'yes' if 'cf_clearance' in session._cookies else 'no'}",
    ]
    return " | ".join(parts)
