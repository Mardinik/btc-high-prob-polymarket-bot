"""Order placement and balance helpers for Polymarket CLOB."""

import logging
import time
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Settings

logger = logging.getLogger(__name__)

_client: Optional[ClobClient] = None
_neg_risk_cache: dict = {}


def get_client(settings: Settings) -> ClobClient:
    global _client
    if _client is not None:
        return _client

    if not settings.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required")

    funder = settings.funder.strip() or None
    _client = ClobClient(
        "https://clob.polymarket.com",
        key=settings.private_key.strip(),
        chain_id=137,
        signature_type=settings.signature_type,
        funder=funder,
    )
    creds = _client.create_or_derive_api_creds()
    _client.set_api_creds(creds)

    logger.info(f"✅ Client ready | wallet: {_client.get_address()} | funder: {funder}")
    return _client


def get_balance(settings: Settings) -> float:
    """Return USDC balance in dollars (6-decimal raw → float)."""
    client = get_client(settings)
    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=settings.signature_type,
    )
    result = client.get_balance_allowance(params)
    if isinstance(result, dict):
        return float(result.get("balance", 0)) / 1_000_000
    return 0.0


def _neg_risk(client: ClobClient, token_id: str) -> bool:
    if token_id not in _neg_risk_cache:
        try:
            _neg_risk_cache[token_id] = client.get_neg_risk(token_id)
        except Exception:
            _neg_risk_cache[token_id] = False
    return _neg_risk_cache[token_id]


def place_buy_gtc(settings: Settings, token_id: str, price: float, size: float) -> dict:
    """
    Place a GTC limit BUY order at `price`.

    GTC semantics on Polymarket:
      - Fills immediately if there is liquidity at that price level.
      - Rests in the order book until filled OR the market closes (auto-cancel).
      - No capital is lost on unfilled orders.

    Returns the raw API response dict.
    Raises RuntimeError on any API failure.
    """
    if price <= 0 or size <= 0 or not token_id:
        raise ValueError(f"Invalid order params: token={token_id} price={price} size={size}")

    client    = get_client(settings)
    order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
    options    = PartialCreateOrderOptions(neg_risk=_neg_risk(client, token_id))
    signed     = client.create_order(order_args, options)

    try:
        resp = client.post_order(signed, OrderType.GTC)
        logger.debug(f"BUY GTC response: {resp}")
        return resp
    except Exception as exc:
        raise RuntimeError(f"place_buy_gtc failed: {exc}") from exc


def place_sell_gtc(settings: Settings, token_id: str, price: float, size: float) -> dict:
    """
    Place a GTC limit SELL order at `price` (used for stop-loss exit).

    Returns the raw API response dict.
    Raises RuntimeError on any API failure.
    """
    if price <= 0 or size <= 0 or not token_id:
        raise ValueError(f"Invalid order params: token={token_id} price={price} size={size}")

    client    = get_client(settings)
    order_args = OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
    options    = PartialCreateOrderOptions(neg_risk=_neg_risk(client, token_id))
    signed     = client.create_order(order_args, options)

    try:
        resp = client.post_order(signed, OrderType.GTC)
        logger.debug(f"SELL GTC response: {resp}")
        return resp
    except Exception as exc:
        raise RuntimeError(f"place_sell_gtc failed: {exc}") from exc