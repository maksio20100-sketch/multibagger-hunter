"""Talk to the SEC EDGAR APIs.

This file knows how to *download* data from the SEC. It does not know
anything about revenue, margins or Streamlit - that keeps it small and easy
to test.

The SEC's rules (https://www.sec.gov/os/webmaster-faq#developers):
  * Every request must carry a User-Agent header that identifies who is
    asking, including a contact email. Requests without one get blocked.
  * No more than 10 requests per second. We stay far below that.
"""

import os
import re
import threading
import time
from typing import Any, Dict, Optional

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# At most ~6 requests per second (SEC allows 10). Being polite is cheap.
MIN_SECONDS_BETWEEN_REQUESTS = 0.15
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 30


class SecError(Exception):
    """Raised for anything that goes wrong while talking to the SEC."""


class MissingUserAgentError(SecError):
    """Raised when no User-Agent (contact details) has been configured."""


# ---------------------------------------------------------------------------
# User-Agent: who we are. Read from configuration, NEVER written in the code,
# because this repository is public.
# ---------------------------------------------------------------------------
def _read_configured_user_agent() -> Optional[str]:
    """Look for the User-Agent in the environment, then in Streamlit secrets."""
    from_env = os.environ.get("SEC_USER_AGENT")
    if from_env:
        return from_env
    try:
        import streamlit as st

        return st.secrets.get("SEC_USER_AGENT")
    except Exception:  # no secrets file, or not running inside Streamlit
        return None


def get_user_agent(explicit: Optional[str] = None) -> str:
    """Return the User-Agent string, looking in this order:

    1. the `explicit` argument (used by tests),
    2. the SEC_USER_AGENT environment variable,
    3. Streamlit's secrets (`.streamlit/secrets.toml` locally, or the
       "Secrets" box in the Streamlit Community Cloud app settings).
    """
    candidate = (explicit or _read_configured_user_agent() or "").strip()
    # A valid value looks like: "AppName you@example.com". Require an
    # email-like part so we do not send an anonymous request.
    if not re.search(r"\S+@\S+\.\S+", candidate):
        raise MissingUserAgentError(
            "SEC_USER_AGENT is not set (or has no contact email). "
            'Set it to something like "MultibaggerHunter yourname@example.com".'
        )
    return candidate


# ---------------------------------------------------------------------------
# Polite downloading: rate limit + retries
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_last_request_time = 0.0


def _wait_for_turn() -> None:
    """Sleep if needed so requests are spaced out. Shared by all threads."""
    global _last_request_time
    with _rate_lock:
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def _get(url: str, user_agent: Optional[str] = None, accept: str = "*/*") -> requests.Response:
    """Download `url` (politely) and return the response, retrying on overload."""
    headers = {"User-Agent": get_user_agent(user_agent), "Accept": accept}
    last_problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _wait_for_turn()
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_problem = f"network error: {exc}"
        else:
            if response.status_code == 200:
                return response
            if response.status_code == 404:
                raise SecError(f"SEC has no data at {url} (HTTP 404).")
            last_problem = f"HTTP {response.status_code}"
            if response.status_code == 403:
                # Usually means the User-Agent was rejected or we were
                # rate-limited. Retrying quickly only makes it worse.
                raise SecError(
                    "SEC refused the request (HTTP 403). Check that SEC_USER_AGENT "
                    "contains a real contact email, then wait a few minutes and retry."
                )
        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)  # back off: 2s, then 4s
    raise SecError(f"Could not fetch {url} after {MAX_ATTEMPTS} attempts ({last_problem}).")


def fetch_json(url: str, user_agent: Optional[str] = None) -> Any:
    """Download `url` and return the parsed JSON."""
    response = _get(url, user_agent, accept="application/json")
    try:
        return response.json()
    except ValueError as exc:
        raise SecError(f"SEC returned invalid JSON for {url}") from exc


def fetch_text(url: str, user_agent: Optional[str] = None) -> str:
    """Download `url` as text (used to read filing documents for verification)."""
    return _get(url, user_agent).text


# ---------------------------------------------------------------------------
# The two SEC endpoints we use
# ---------------------------------------------------------------------------
def fetch_ticker_map(user_agent: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Download SEC's ticker list and return {TICKER: {"cik": int, "title": str}}.

    A company with several share classes (e.g. GOOGL and GOOG) appears once
    per ticker, all pointing at the same CIK (the SEC's company ID number).
    """
    raw = fetch_json(TICKERS_URL, user_agent)
    mapping: Dict[str, Dict[str, Any]] = {}
    for entry in raw.values():
        mapping[str(entry["ticker"]).upper()] = {
            "cik": int(entry["cik_str"]),
            "title": entry["title"],
        }
    return mapping


def fetch_company_facts(cik: int, user_agent: Optional[str] = None) -> Dict[str, Any]:
    """Download every XBRL fact the company ever filed (one big JSON)."""
    url = COMPANYFACTS_URL.format(cik=f"{int(cik):010d}")  # CIKs are zero-padded to 10 digits
    return fetch_json(url, user_agent)
