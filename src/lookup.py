"""
Fetch token IDs for the active BTC 15-min market on Polymarket.

Primary source : Gamma REST API  (https://gamma-api.polymarket.com)
Fallback source: Polymarket HTML scraping

Timestamp convention
--------------------
Polymarket slugs follow the pattern btc-updown-15m-{ts}.
The timestamp may be the START or END of the 15-minute window.
Both conventions are checked so discovery works regardless.
"""

import json
import logging
import re
import time
from typing import Optional, Dict, Tuple

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

_UP_KEYWORDS = {"up", "yes", "higher", "above", "pump"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_token_order(outcomes: list) -> Tuple[int, int]:
    for i, label in enumerate(outcomes):
        if str(label).strip().lower() in _UP_KEYWORDS:
            return i, 1 - i
    return 0, 1


def _is_active(ts: int, now: int) -> bool:
    """True if this timestamp (as start OR end) places the market window around now."""
    start_conv = ts <= now < ts + 900
    end_conv   = ts - 900 <= now < ts
    return start_conv or end_conv


def _end_ts(ts: int, now: int) -> int:
    """Infer actual end timestamp from slug timestamp."""
    if ts > now:        # ts is in the future → it IS the end time
        return ts
    return ts + 900     # ts is in the past → it's the start time


# ---------------------------------------------------------------------------
# Active-slug discovery
# ---------------------------------------------------------------------------

def find_active_slug() -> Optional[str]:
    """Return the slug of the currently active BTC 15-min market, or None."""
    now = int(time.time())

    # --- Attempt 1: Gamma API tag=btc ---
    try:
        r = httpx.get(
            f"{GAMMA_API}/markets",
            params={"tag": "btc", "active": "true", "limit": 50},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json():
            slug = m.get("slug", "")
            match = re.search(r"btc-updown-15m-(\d+)", slug)
            if match and _is_active(int(match.group(1)), now):
                logger.info(f"[lookup] found via Gamma tag=btc: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma tag=btc failed: {e}")

    # --- Attempt 2: Gamma API — broader active list ---
    try:
        r = httpx.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "limit": 100},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json():
            slug = m.get("slug", "")
            match = re.search(r"btc-updown-15m-(\d+)", slug)
            if match and _is_active(int(match.group(1)), now):
                logger.info(f"[lookup] found via Gamma active list: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma active list failed: {e}")

    # --- Attempt 3: Gamma API — /events endpoint ---
    try:
        r = httpx.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "limit": 50},
            timeout=10,
        )
        r.raise_for_status()
        data   = r.json()
        events = data if isinstance(data, list) else data.get("data", [])
        for ev in events:
            slug = ev.get("slug", "")
            match = re.search(r"btc-updown-15m-(\d+)", slug)
            if match and _is_active(int(match.group(1)), now):
                logger.info(f"[lookup] found via Gamma /events: {slug}")
                return slug
            for mk in ev.get("markets", []):
                slug = mk.get("slug", "")
                match = re.search(r"btc-updown-15m-(\d+)", slug)
                if match and _is_active(int(match.group(1)), now):
                    logger.info(f"[lookup] found via Gamma /events nested: {slug}")
                    return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma /events failed: {e}")

    # --- Attempt 4: HTML scraping — /crypto/15M ---
    try:
        r = httpx.get(
            "https://polymarket.com/crypto/15M",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        timestamps = sorted(set(int(x) for x in re.findall(r"btc-updown-15m-(\d+)", r.text)))
        logger.debug(f"[lookup] HTML /crypto/15M timestamps: {timestamps}")
        for ts in timestamps:
            if _is_active(ts, now):
                slug = f"btc-updown-15m-{ts}"
                logger.info(f"[lookup] found via HTML /crypto/15M: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] HTML /crypto/15M failed: {e}")

    # --- Attempt 5: HTML scraping — /markets ---
    try:
        r = httpx.get(
            "https://polymarket.com/markets",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        for ts in sorted(set(int(x) for x in re.findall(r"btc-updown-15m-(\d+)", r.text))):
            if _is_active(ts, now):
                slug = f"btc-updown-15m-{ts}"
                logger.info(f"[lookup] found via HTML /markets: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] HTML /markets failed: {e}")

    logger.warning("[lookup] No active BTC 15-min market found across all sources.")
    return None


# ---------------------------------------------------------------------------
# Token-ID resolution
# ---------------------------------------------------------------------------

def fetch_market_tokens(slug: str) -> Dict[str, str]:
    """
    Return yes_token_id and no_token_id for the given slug.
    Raises RuntimeError if the market cannot be found.
    """
    slug = slug.split("?")[0]

    # --- Primary: Gamma API exact slug ---
    try:
        r = httpx.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=10,
        )
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, list) and markets:
            m        = markets[0]
            tokens   = m.get("clobTokenIds") or []
            outcomes = m.get("outcomes") or []
            if len(tokens) == 2:
                yes_idx, no_idx = _resolve_token_order(outcomes)
                logger.info(f"[lookup] tokens via Gamma: yes={tokens[yes_idx][:12]}…")
                return {
                    "yes_token_id": tokens[yes_idx],
                    "no_token_id":  tokens[no_idx],
                    "outcomes":     outcomes,
                }
            logger.warning(f"[lookup] Gamma returned {len(tokens)} tokens for {slug}")
    except Exception as e:
        logger.debug(f"[lookup] Gamma fetch_market_tokens failed: {e}")

    # --- Fallback: HTML scraping (__NEXT_DATA__) ---
    logger.debug(f"[lookup] Falling back to HTML scraping for {slug}")
    url  = f"https://polymarket.com/event/{slug}"
    resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not m:
        raise RuntimeError(f"[lookup] __NEXT_DATA__ not found for {slug}")
    payload = json.loads(m.group(1))

    queries = (payload.get("props", {})
                      .get("pageProps", {})
                      .get("dehydratedState", {})
                      .get("queries", []))

    for q in queries:
        data = q.get("state", {}).get("data")
        if not isinstance(data, dict) or "markets" not in data:
            continue
        for mk in data["markets"]:
            if mk.get("slug") != slug:
                continue
            tokens   = mk.get("clobTokenIds") or []
            outcomes = mk.get("outcomes") or []
            if len(tokens) != 2:
                raise RuntimeError(f"HTML fallback: expected 2 tokens, got {len(tokens)}")
            yes_idx, no_idx = _resolve_token_order(outcomes)
            logger.info(f"[lookup] tokens via HTML: yes={tokens[yes_idx][:12]}…")
            return {
                "yes_token_id": tokens[yes_idx],
                "no_token_id":  tokens[no_idx],
                "outcomes":     outcomes,
            }

    raise RuntimeError(f"Slug '{slug}' not found via Gamma API or HTML scraping")


# ---------------------------------------------------------------------------
# End-timestamp helper
# ---------------------------------------------------------------------------

def slug_end_ts(slug: str) -> Optional[int]:
    """Infer the end timestamp from a slug, handling both timestamp conventions."""
    match = re.search(r"btc-updown-15m-(\d+)", slug)
    if not match:
        return None
    ts  = int(match.group(1))
    now = int(time.time())
    return _end_ts(ts, now)


# ---------------------------------------------------------------------------
# CLI helper (run directly to test)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    slug = find_active_slug()
    if not slug:
        print("No active market found")
        sys.exit(1)

    info = fetch_market_tokens(slug)
    end  = slug_end_ts(slug)
    print(f"\nSlug:         {slug}")
    print(f"End TS:       {end}  ({time.strftime('%H:%M:%S', time.localtime(end)) if end else '?'})")
    print(f"YES token:    {info['yes_token_id']}")
    print(f"NO  token:    {info['no_token_id']}")
    print(f"Outcomes:     {info['outcomes']}")