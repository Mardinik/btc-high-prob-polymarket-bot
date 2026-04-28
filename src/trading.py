"""Order placement and balance helpers for Polymarket CLOB V2."""

import logging
from typing import Optional

from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from .config import Settings

logger = logging.getLogger(__name__)

_client: Optional[ClobClient] = None


def get_client(settings: Settings) -> ClobClient:
    global _client
    if _client is not None:
        return _client

    if not settings.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required")

    funder = settings.funder.strip() or None

    # Step 1 — L1 client to derive API credentials
    l1 = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=settings.private_key.strip(),
        funder=funder,
        signature_type=settings.signature_type,
    )
    creds = l1.create_or_derive_api_key()

    # Step 2 — Full client with L1 + L2 auth
    _client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=settings.private_key.strip(),
        funder=funder,
        signature_type=settings.signature_type,
        creds=creds,
    )

    logger.info(f"✅ Client ready | wallet: {_client.get_address()} | funder: {funder}")
    return _client


def get_balance(settings: Settings) -> float:
    """Return pUSD balance in dollars (6-decimal ERC-20, same as USDC)."""
    client = get_client(settings)
    try:
        result = client.get_balance()
        if isinstance(result, dict):
            return float(result.get("balance", 0)) / 1_000_000
        return float(result) / 1_000_000
    except Exception as e:
        logger.warning(f"get_balance failed ({e}), falling back to get_balance_allowance")
        try:
            from py_clob_client_v2 import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = client.get_balance_allowance(params)
            if isinstance(result, dict):
                return float(result.get("balance", 0)) / 1_000_000
        except Exception as e2:
            logger.error(f"Balance fetch failed completely: {e2}")
    return 0.0


def _get_tick_size(client: ClobClient, token_id: str) -> str:
    """Fetch the minimum tick size for a token. Defaults to '0.01'."""
    try:
        info = client.get_market_info(token_id)
        return str(info.get("minimum_tick_size") or info.get("tick_size") or "0.01")
    except Exception:
        return "0.01"


def place_buy_gtc(settings: Settings, token_id: str, price: float, size: float) -> dict:
    """
    Place a GTC limit BUY order at `price` via CLOB V2.

    GTC semantics:
      - Fills immediately if liquidity exists at that price.
      - Rests in the book until filled OR the market closes (auto-cancel).
      - No capital lost on unfilled orders.

    Returns the raw API response dict.
    Raises RuntimeError on any API failure.
    """
    if price <= 0 or size <= 0 or not token_id:
        raise ValueError(f"Invalid order params: token={token_id} price={price} size={size}")

    client    = get_client(settings)
    tick_size = _get_tick_size(client, token_id)

    try:
        resp = client.create_and_post_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=price,
                side=Side.BUY,
                size=size,
            ),
            options=PartialCreateOrderOptions(tick_size=tick_size),
            order_type=OrderType.GTC,
        )
        logger.debug(f"BUY GTC response: {resp}")
        return resp if isinstance(resp, dict) else {"raw": resp}
    except Exception as exc:
        raise RuntimeError(f"place_buy_gtc failed: {exc}") from exc


def place_sell_gtc(settings: Settings, token_id: str, price: float, size: float) -> dict:
    """
    Place a GTC limit SELL order at `price` (stop-loss exit).

    Returns the raw API response dict.
    Raises RuntimeError on any API failure.
    """
    if price <= 0 or size <= 0 or not token_id:
        raise ValueError(f"Invalid order params: token={token_id} price={price} size={size}")

    client    = get_client(settings)
    tick_size = _get_tick_size(client, token_id)

    try:
        resp = client.create_and_post_order(
            order_args=OrderArgs(
                token_id=token_id,
                price=price,
                side=Side.SELL,
                size=size,
            ),
            options=PartialCreateOrderOptions(tick_size=tick_size),
            order_type=OrderType.GTC,
        )
        logger.debug(f"SELL GTC response: {resp}")
        return resp if isinstance(resp, dict) else {"raw": resp}
    except Exception as exc:
        raise RuntimeError(f"place_sell_gtc failed: {exc}") from exc