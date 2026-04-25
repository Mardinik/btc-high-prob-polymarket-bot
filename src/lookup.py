"""Fetch token IDs for the active BTC 15-min market on Polymarket."""

import json
import re
import time
from typing import Optional, Dict

import httpx


def find_active_slug() -> Optional[str]:
    """Return the slug of the currently active BTC 15-min market, or None."""
    try:
        r = httpx.get(
            "https://polymarket.com/crypto/15M",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        now = int(time.time())
        for ts in sorted(int(x) for x in re.findall(r"btc-updown-15m-(\d+)", r.text)):
            if ts <= now < ts + 900:
                return f"btc-updown-15m-{ts}"
    except Exception:
        pass
    return None


def fetch_market_tokens(slug: str) -> Dict[str, str]:
    """
    Return yes_token_id and no_token_id for the given slug.
    Raises RuntimeError if the market cannot be found.
    """
    slug = slug.split("?")[0]
    url  = f"https://polymarket.com/event/{slug}"
    resp = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not m:
        raise RuntimeError("__NEXT_DATA__ not found")
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
                raise RuntimeError(f"Expected 2 tokens, got {len(tokens)}")
            return {
                "yes_token_id": tokens[0],
                "no_token_id":  tokens[1],
                "outcomes":     outcomes,
            }

    raise RuntimeError(f"Slug '{slug}' not found in page data")


if __name__ == "__main__":
    import sys
    slug = find_active_slug()
    if not slug:
        print("No active market found")
        sys.exit(1)
    info = fetch_market_tokens(slug)
    print(f"Slug: {slug}")
    print(json.dumps(info, indent=2))