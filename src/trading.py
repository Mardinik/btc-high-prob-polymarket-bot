import functools
import logging
from typing import Optional
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
    OrderType,
    PostOrdersArgs,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Settings

logger = logging.getLogger(__name__)

_cached_client = None

# Caché simple para neg_risk (por token_id)
_neg_risk_cache = {}


def get_neg_risk_for_token(client, token_id):
    """Obtiene el valor neg_risk para un token, con caché."""
    if token_id in _neg_risk_cache:
        return _neg_risk_cache[token_id]
    try:
        neg_risk = client.get_neg_risk(token_id)
        _neg_risk_cache[token_id] = neg_risk
        return neg_risk
    except Exception as e:
        logger.debug(f"Error getting neg_risk for {token_id}: {e}")
        _neg_risk_cache[token_id] = False
        return False


def get_client(settings: Settings) -> ClobClient:
    global _cached_client
    
    if _cached_client is not None:
        return _cached_client
    
    if not settings.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for trading")
    
    host = "https://clob.polymarket.com"
    
    # Usar funder exactamente como viene (recomendado con checksum)
    funder = settings.funder.strip() if settings.funder else None
    
    _cached_client = ClobClient(
        host, 
        key=settings.private_key.strip(), 
        chain_id=137, 
        signature_type=settings.signature_type, 
        funder=funder
    )
    
    # Derivar credenciales API
    logger.info("Deriving User API credentials from private key...")
    derived_creds = _cached_client.create_or_derive_api_creds()
    _cached_client.set_api_creds(derived_creds)
    
    logger.info("✅ API credentials configured")
    logger.info(f"   API Key: {derived_creds.api_key}")
    logger.info(f"   Wallet: {_cached_client.get_address()}")
    logger.info(f"   Funder: {funder}")
    
    return _cached_client


def get_balance(settings: Settings) -> float:
    """Get USDC balance from Polymarket account."""
    try:
        client = get_client(settings)
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=settings.signature_type
        )
        result = client.get_balance_allowance(params)
        
        if isinstance(result, dict):
            balance_raw = result.get("balance", "0")
            balance_wei = float(balance_raw)
            balance_usdc = balance_wei / 1_000_000
            return balance_usdc
        
        logger.warning(f"Unexpected response getting balance: {result}")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        return 0.0


def place_order(settings: Settings, *, side: str, token_id: str, price: float, size: float, tif: str = "GTC") -> dict:
    if price <= 0:
        raise ValueError("price must be > 0")
    if size <= 0:
        raise ValueError("size must be > 0")
    if not token_id:
        raise ValueError("token_id is required")

    side_up = side.upper()
    if side_up not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    client = get_client(settings)
    
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side_up == "BUY" else SELL
        )
        
        # Determinar neg_risk dinámicamente
        neg_risk = get_neg_risk_for_token(client, token_id)
        options = PartialCreateOrderOptions(neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        
        tif_up = (tif or "GTC").upper()
        order_type = getattr(OrderType, tif_up, OrderType.GTC)
        return client.post_order(signed_order, order_type)
    except Exception as exc:
        raise RuntimeError(f"place_order failed: {exc}") from exc


def place_orders_fast(settings: Settings, orders: list[dict], *, order_type: str = "GTC") -> list[dict]:
    """
    Place multiple orders as fast as possible.
    Pre-signs all orders first, then submits them together.
    """
    client = get_client(settings)

    tif_up = (order_type or "GTC").upper()
    ot = getattr(OrderType, tif_up, OrderType.GTC)

    # Pre-sign orders, determining neg_risk per token
    signed_orders = []
    for order_params in orders:
        side_up = order_params["side"].upper()
        token_id = order_params["token_id"]
        order_args = OrderArgs(
            token_id=token_id,
            price=order_params["price"],
            size=order_params["size"],
            side=BUY if side_up == "BUY" else SELL,
        )
        neg_risk = get_neg_risk_for_token(client, token_id)
        options = PartialCreateOrderOptions(neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        signed_orders.append(signed_order)

    # Post all orders in a single request when possible
    try:
        args = [PostOrdersArgs(order=o, orderType=ot) for o in signed_orders]
        result = client.post_orders(args)
        if isinstance(result, list):
            return result
        return [result]
    except Exception:
        # Fallback to sequential posting
        results: list[dict] = []
        for signed_order in signed_orders:
            try:
                results.append(client.post_order(signed_order, ot))
            except Exception as exc:
                results.append({"error": str(exc)})
        return results


def extract_order_id(result: dict) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        val = result.get(key)
        if val:
            return str(val)
    for key in ("order", "data", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            oid = extract_order_id(nested)
            if oid:
                return oid
    return None


def get_order(settings: Settings, order_id: str) -> dict:
    client = get_client(settings)
    return client.get_order(order_id)


def cancel_orders(settings: Settings, order_ids: list[str]) -> Optional[dict]:
    if not order_ids:
        return None
    client = get_client(settings)
    return client.cancel_orders(order_ids)


def _coerce_float(val) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def summarize_order_state(order_data: dict, *, requested_size: Optional[float] = None) -> dict:
    if not isinstance(order_data, dict):
        return {"status": None, "filled_size": None, "requested_size": requested_size, "raw": order_data}

    status = order_data.get("status") or order_data.get("state") or order_data.get("order_status")
    status_str = str(status).lower() if status is not None else None

    filled_size = None
    for key in ("filled_size", "filledSize", "size_filled", "sizeFilled", "matched_size", "matchedSize"):
        if key in order_data:
            filled_size = _coerce_float(order_data.get(key))
            break

    remaining_size = None
    for key in ("remaining_size", "remainingSize", "size_remaining", "sizeRemaining"):
        if key in order_data:
            remaining_size = _coerce_float(order_data.get(key))
            break

    original_size = None
    for key in ("original_size", "originalSize", "size", "order_size", "orderSize"):
        if key in order_data:
            original_size = _coerce_float(order_data.get(key))
            break

    if filled_size is None and remaining_size is not None and original_size is not None:
        filled_size = max(0.0, original_size - remaining_size)

    return {
        "status": status_str,
        "filled_size": filled_size,
        "remaining_size": remaining_size,
        "original_size": original_size,
        "requested_size": requested_size,
        "raw": order_data,
    }


def wait_for_terminal_order(
    settings: Settings,
    order_id: str,
    *,
    requested_size: Optional[float] = None,
    timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 0.25,
) -> dict:
    terminal_statuses = {"filled", "canceled", "cancelled", "rejected", "expired"}
    start = time.monotonic()
    last_summary: Optional[dict] = None

    while (time.monotonic() - start) < timeout_seconds:
        try:
            od = get_order(settings, order_id)
            last_summary = summarize_order_state(od, requested_size=requested_size)
        except Exception as exc:
            last_summary = {"status": "error", "error": str(exc), "filled_size": None, "requested_size": requested_size}

        status = (last_summary.get("status") or "").lower() if isinstance(last_summary, dict) else ""
        filled = last_summary.get("filled_size") if isinstance(last_summary, dict) else None

        if requested_size is not None and filled is not None and filled + 1e-9 >= float(requested_size):
            last_summary["terminal"] = True
            last_summary["filled"] = True
            return last_summary

        if status in terminal_statuses:
            last_summary["terminal"] = True
            last_summary["filled"] = (status == "filled")
            return last_summary

        time.sleep(poll_interval_seconds)

    if last_summary is None:
        last_summary = {"status": None, "filled_size": None, "requested_size": requested_size}
    last_summary["terminal"] = False
    last_summary.setdefault("filled", False)
    return last_summary


def get_positions(settings: Settings, token_ids: list[str] = None) -> dict:
    try:
        client = get_client(settings)
        positions = client.get_positions()
        result = {}
        for pos in positions:
            token_id = pos.get("asset", {}).get("token_id") or pos.get("token_id")
            if token_id:
                if token_ids is None or token_id in token_ids:
                    size = float(pos.get("size", 0))
                    avg_price = float(pos.get("avg_price", 0))
                    result[token_id] = {
                        "size": size,
                        "avg_price": avg_price,
                        "raw": pos
                    }
        return result
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return {}