"""
Market discovery and token-ID resolution for Polymarket up/down markets.

Supports both 15-min and 5-min markets for BTC, ETH and other assets.

Key difference by market type
-------------------------------
5m  → slug timestamp is DETERMINISTIC (ts = now // 300 * 300).
      No scraping or Gamma API needed for discovery.
15m → timestamp must be discovered via Gamma API or HTML scraping.
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


def _is_active(ts: int, now: int, duration: int) -> bool:
    """True if ts (as start OR end) places the window around now."""
    return (ts <= now < ts + duration) or (ts - duration <= now < ts)


def _end_ts_from_slug_ts(ts: int, now: int, duration: int) -> int:
    """Infer end timestamp from slug timestamp and window duration."""
    if ts > now:          # ts is in the future → it IS the end time
        return ts
    return ts + duration  # ts is in the past → it's the start time


# ---------------------------------------------------------------------------
# Active-slug discovery
# ---------------------------------------------------------------------------

def find_active_slug(slug_prefix: str, market_duration: int) -> Optional[str]:
    """
    Return the slug of the currently active market.

    For 5-min markets the slug timestamp is always ts = now // 300 * 300
    so no external discovery is needed — just verify the market exists.

    For 15-min markets we fall through a cascade of Gamma API + HTML sources.
    """
    now = int(time.time())
    duration = market_duration

    # ── 5-min: deterministic slug ──────────────────────────────────────────
    if duration == 300:
        window_ts = (now // 300) * 300
        slug = f"{slug_prefix}-{window_ts}"
        # Quick verify via Gamma API (non-blocking on failure)
        try:
            r = httpx.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=5)
            if r.status_code == 200 and r.json():
                logger.info(f"[lookup] 5m slug confirmed via Gamma: {slug}")
                return slug
        except Exception as e:
            logger.debug(f"[lookup] Gamma verify failed for 5m slug ({e}), using deterministic value")
        logger.info(f"[lookup] 5m slug (deterministic): {slug}")
        return slug

    # ── 15-min: cascade discovery ──────────────────────────────────────────
    pattern = re.compile(rf"{re.escape(slug_prefix)}-(\d+)")

    # Attempt 1: Gamma API tag filter
    asset = slug_prefix.split("-")[0]
    try:
        r = httpx.get(f"{GAMMA_API}/markets",
                      params={"tag": asset, "active": "true", "limit": 50}, timeout=10)
        r.raise_for_status()
        for m in r.json():
            slug = m.get("slug", "")
            match = pattern.search(slug)
            if match and _is_active(int(match.group(1)), now, duration):
                logger.info(f"[lookup] found via Gamma tag={asset}: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma tag={asset} failed: {e}")

    # Attempt 2: Gamma API active list
    try:
        r = httpx.get(f"{GAMMA_API}/markets",
                      params={"active": "true", "limit": 100}, timeout=10)
        r.raise_for_status()
        for m in r.json():
            slug = m.get("slug", "")
            match = pattern.search(slug)
            if match and _is_active(int(match.group(1)), now, duration):
                logger.info(f"[lookup] found via Gamma active list: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma active list failed: {e}")

    # Attempt 3: Gamma API /events
    try:
        r = httpx.get(f"{GAMMA_API}/events",
                      params={"active": "true", "limit": 50}, timeout=10)
        r.raise_for_status()
        data = r.json()
        events = data if isinstance(data, list) else data.get("data", [])
        for ev in events:
            for candidate in [ev] + ev.get("markets", []):
                slug = candidate.get("slug", "")
                match = pattern.search(slug)
                if match and _is_active(int(match.group(1)), now, duration):
                    logger.info(f"[lookup] found via Gamma /events: {slug}")
                    return slug
    except Exception as e:
        logger.debug(f"[lookup] Gamma /events failed: {e}")

    # Attempt 4: HTML scraping /crypto/15M
    try:
        r = httpx.get("https://polymarket.com/crypto/15M",
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        for ts in sorted(set(int(x) for x in pattern.findall(r.text))):
            if _is_active(ts, now, duration):
                slug = f"{slug_prefix}-{ts}"
                logger.info(f"[lookup] found via HTML /crypto/15M: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] HTML /crypto/15M failed: {e}")

    # Attempt 5: HTML scraping /markets
    try:
        r = httpx.get("https://polymarket.com/markets",
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        for ts in sorted(set(int(x) for x in pattern.findall(r.text))):
            if _is_active(ts, now, duration):
                slug = f"{slug_prefix}-{ts}"
                logger.info(f"[lookup] found via HTML /markets: {slug}")
                return slug
    except Exception as e:
        logger.debug(f"[lookup] HTML /markets failed: {e}")

    logger.warning(f"[lookup] No active {slug_prefix} market found across all sources.")
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

    # Primary: Gamma API exact slug
    try:
        r = httpx.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        markets = r.json()
        if isinstance(markets, list) and markets:
            m        = markets[0]
            tokens   = m.get("clobTokenIds") or []
            outcomes = m.get("outcomes") or []
            if len(tokens) == 2:
                yi, ni = _resolve_token_order(outcomes)
                logger.info(f"[lookup] tokens via Gamma: yes={tokens[yi][:12]}…")
                return {"yes_token_id": tokens[yi], "no_token_id": tokens[ni],
                        "outcomes": outcomes}
            logger.warning(f"[lookup] Gamma returned {len(tokens)} tokens for {slug}")
    except Exception as e:
        logger.debug(f"[lookup] Gamma fetch_market_tokens failed: {e}")

    # Fallback: HTML scraping __NEXT_DATA__
    logger.debug(f"[lookup] Falling back to HTML for {slug}")
    url  = f"https://polymarket.com/event/{slug}"
    resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not m:
        raise RuntimeError(f"[lookup] __NEXT_DATA__ not found for {slug}")
    payload = json.loads(m.group(1))

    queries = (payload.get("props", {}).get("pageProps", {})
                      .get("dehydratedState", {}).get("queries", []))
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
            yi, ni = _resolve_token_order(outcomes)
            logger.info(f"[lookup] tokens via HTML: yes={tokens[yi][:12]}…")
            return {"yes_token_id": tokens[yi], "no_token_id": tokens[ni],
                    "outcomes": outcomes}

    raise RuntimeError(f"Slug '{slug}' not found via Gamma API or HTML scraping")


# ---------------------------------------------------------------------------
# End-timestamp helper
# ---------------------------------------------------------------------------

def slug_end_ts(slug: str, market_duration: int) -> Optional[int]:
    """Infer end timestamp from a slug, handling both timestamp conventions."""
    match = re.search(r"-(\d+)$", slug)
    if not match:
        return None
    ts  = int(match.group(1))
    now = int(time.time())
    return _end_ts_from_slug_ts(ts, now, market_duration)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s  %(message)s")

    mtype = sys.argv[1] if len(sys.argv) > 1 else "15m"
    asset = sys.argv[2] if len(sys.argv) > 2 else "btc"
    dur   = 300 if mtype == "5m" else 900
    prefix = f"{asset}-updown-{mtype}"

    slug = find_active_slug(prefix, dur)
    if not slug:
        print("No active market found"); sys.exit(1)

    info = fetch_market_tokens(slug)
    end  = slug_end_ts(slug, dur)
    print(f"\nSlug:      {slug}")
    print(f"End TS:    {end}  ({time.strftime('%H:%M:%S', time.localtime(end)) if end else '?'})")
    print(f"YES token: {info['yes_token_id']}")
    print(f"NO  token: {info['no_token_id']}")
    print(f"Outcomes:  {info['outcomes']}")