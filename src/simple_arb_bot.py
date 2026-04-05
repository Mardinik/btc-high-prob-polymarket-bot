"""
Simple arbitrage bot for Bitcoin 15min markets.
Directional mode with two signal strategies: confluence (high threshold) or indicator-based (scoring).
"""

import asyncio
import logging
import re
import time
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional, List, Dict

import httpx

from .config import load_settings
from .lookup import fetch_market_from_slug
from .trading import (
    get_client,
    place_order,
    place_orders_fast,
    extract_order_id,
    wait_for_terminal_order,
    cancel_orders,
)
from .wss_market import MarketWssClient


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)


class Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def strip_ansi(text: str) -> str:
    """Elimina los códigos ANSI de un texto."""
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def find_current_btc_15min_market() -> str:
    """Find the currently active BTC 15min market on Polymarket."""
    logger.info("Searching for current BTC 15min market...")
    try:
        page_url = "https://polymarket.com/crypto/15M"
        resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()

        pattern = r'btc-updown-15m-(\d+)'
        matches = re.findall(pattern, resp.text)
        if not matches:
            raise RuntimeError("No active BTC 15min market found")

        now_ts = int(datetime.now().timestamp())
        all_ts = sorted((int(ts) for ts in matches))

        for ts in all_ts:
            if ts <= now_ts < ts + 900:
                slug = f"btc-updown-15m-{ts}"
                logger.info(f"Active market: {slug}")
                return slug

        future_ts = [ts for ts in all_ts if ts > now_ts]
        if future_ts:
            next_ts = min(future_ts)
            slug = f"btc-updown-15m-{next_ts}"
            logger.info(f"Next market: {slug} (starts in {next_ts - now_ts}s)")
            return slug

        raise RuntimeError("No suitable market found")

    except Exception as e:
        logger.error(f"Error searching market: {e}")
        raise


class SimpleArbitrageBot:
    def __init__(self, settings):
        self.settings = settings
        self.client = get_client(settings)

        self._initialize_market()

        self.opportunities_found = 0
        self.trades_executed = 0
        self.wins = 0
        self.losses = 0
        self.total_invested = 0.0
        self.total_shares_bought = 0
        self.open_positions: List[Dict] = []
        self.cached_balance = None

        self.sim_balance = self.settings.sim_balance if self.settings.sim_balance > 0 else 100.0
        self.sim_start_balance = self.sim_balance
        self._last_execution_ts = 0.0
        self._last_loss_ts = 0.0
        self.last_action = "None"
        self.last_trade_pnl = None

        # Historial de trades cerrados (máximo 5)
        self.trade_history: List[Dict] = []

    def _add_to_history(self, trade: Dict):
        """Añade un trade al historial, manteniendo solo los últimos 5."""
        self.trade_history.append(trade)
        if len(self.trade_history) > 5:
            self.trade_history.pop(0)

    def _initialize_market(self, forced_slug: Optional[str] = None):
        if forced_slug:
            market_slug = forced_slug
            logger.info(f"Using market from assistant: {market_slug}")
        else:
            try:
                market_slug = find_current_btc_15min_market()
            except Exception:
                if self.settings.market_slug:
                    market_slug = self.settings.market_slug
                    logger.info(f"Using configured market: {market_slug}")
                else:
                    raise RuntimeError("Could not find market")

        logger.info(f"Fetching market info: {market_slug}")
        market_info = fetch_market_from_slug(market_slug)

        self.market_id = market_info["market_id"]
        self.yes_token_id = market_info["yes_token_id"]
        self.no_token_id = market_info["no_token_id"]
        self.market_slug = market_slug

        match = re.search(r'btc-updown-15m-(\d+)', market_slug)
        market_start = int(match.group(1)) if match else None
        self.market_end_timestamp = market_start + 900 if market_start else None

        logger.info(f"Market ID: {self.market_id}")
        logger.info(f"UP Token: {self.yes_token_id}")
        logger.info(f"DOWN Token: {self.no_token_id}")

    def load_assistant_state(self):
        try:
            path = self.settings.assistant_state_file
            if not os.path.exists(path):
                logger.info(f"State file not found at {path}")
                return None
            with open(path, 'r') as f:
                state = json.load(f)

            timestamp = datetime.fromisoformat(state['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            if age > 5:
                logger.warning(f"State too old ({age:.1f}s)")
                return None

            assistant_slug = state.get('marketSlug')
            if assistant_slug and assistant_slug != self.market_slug:
                logger.info(f"Assistant switched market: {assistant_slug}")
                self._initialize_market(forced_slug=assistant_slug)

            return state
        except Exception as e:
            logger.warning(f"Could not load state: {e}")
            return None

    def get_time_remaining(self) -> str:
        if not self.market_end_timestamp:
            return "Unknown"
        now = int(datetime.now().timestamp())
        remaining = self.market_end_timestamp - now
        if remaining <= 0:
            return "CLOSED"
        minutes = remaining // 60
        seconds = remaining % 60
        return f"{minutes:02d}:{seconds:02d}"

    def get_time_remaining_minutes(self) -> float:
        if not self.market_end_timestamp:
            return 15.0
        now = int(datetime.now().timestamp())
        remaining = self.market_end_timestamp - now
        return max(0.0, remaining / 60.0)

    def get_balance(self) -> float:
        if self.settings.dry_run:
            return self.sim_balance
        from .trading import get_balance
        return get_balance(self.settings)

    def get_order_book(self, token_id: str) -> dict:
        try:
            book = self.client.get_order_book(token_id=token_id)
            bids = book.bids if hasattr(book, 'bids') and book.bids else []
            asks = book.asks if hasattr(book, 'asks') and book.asks else []

            bid_levels = self._levels_to_tuples(bids)
            ask_levels = self._levels_to_tuples(asks)

            best_bid = max((p for p, _ in bid_levels), default=None)
            best_ask = min((p for p, _ in ask_levels), default=None)

            bid_size = 0.0
            if best_bid is not None:
                for p, s in bid_levels:
                    if p == best_bid:
                        bid_size = s
                        break

            ask_size = 0.0
            if best_ask is not None:
                for p, s in ask_levels:
                    if p == best_ask:
                        ask_size = s
                        break

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "bids": bid_levels,
                "asks": ask_levels,
            }
        except Exception as e:
            logger.debug(f"Error fetching book for {token_id}: {e}")
            return {"best_bid": None, "best_ask": None, "bid_size": 0, "ask_size": 0, "bids": [], "asks": []}

    def _levels_to_tuples(self, levels):
        tuples = []
        for level in levels or []:
            try:
                price = float(level.price)
                size = float(level.size)
                if size > 0:
                    tuples.append((price, size))
            except:
                continue
        return tuples

    def _compute_buy_fill(self, asks, target_size):
        if target_size <= 0 or not asks:
            return None
        sorted_asks = sorted(asks, key=lambda x: x[0])
        filled = 0.0
        cost = 0.0
        worst = None
        best = sorted_asks[0][0]
        for price, size in sorted_asks:
            if filled >= target_size:
                break
            take = min(size, target_size - filled)
            cost += take * price
            filled += take
            worst = price
        if filled + 1e-9 < target_size:
            return None
        return {
            "filled": filled,
            "vwap": cost / filled,
            "worst": worst,
            "best": best,
            "cost": cost,
        }

    async def _fetch_order_books_parallel(self):
        try:
            up_task = asyncio.to_thread(self.get_order_book, self.yes_token_id)
            down_task = asyncio.to_thread(self.get_order_book, self.no_token_id)
            return await asyncio.gather(up_task, down_task)
        except:
            return self.get_order_book(self.yes_token_id), self.get_order_book(self.no_token_id)

    def _technical_score(self, state, direction: str) -> float:
        """Puntuación técnica (0-10) basada en indicadores (usada en modo confluence)."""
        score = 0.0
        rsi = state.get('rsi')
        macd_hist = state.get('macdHist')
        heiken_color = state.get('heikenColor')
        heiken_count = state.get('heikenCount', 0)
        vwap_slope = state.get('vwapSlope')
        delta1m = state.get('delta1m')
        delta3m = state.get('delta3m')

        if direction == 'up':
            if rsi is not None:
                if rsi > 70:
                    score -= 1
                elif rsi > 60 and rsi < 70:
                    score += 1
                elif rsi > 50:
                    score += 2
                elif rsi < 30:
                    score += 2
            if macd_hist is not None and macd_hist > 0:
                score += 2
            if heiken_color == "green":
                score += min(heiken_count, 3)
            if vwap_slope is not None and vwap_slope > 0:
                score += 3
            if delta1m is not None and delta1m > 0:
                score += 2
            if delta3m is not None and delta3m > 0:
                score += 2
        else:  # down
            if rsi is not None:
                if rsi < 30:
                    score -= 1
                elif rsi < 40 and rsi > 30:
                    score += 1
                elif rsi < 50:
                    score += 2
                elif rsi > 70:
                    score += 2
            if macd_hist is not None and macd_hist < 0:
                score += 2
            if heiken_color == "red":
                score += min(heiken_count, 3)
            if vwap_slope is not None and vwap_slope < 0:
                score += 3
            if delta1m is not None and delta1m < 0:
                score += 2
            if delta3m is not None and delta3m < 0:
                score += 2

        return min(10, max(0, score))

    def _indicator_score(self, state, direction: str) -> float:
        """
        Calcula una puntuación basada en indicadores individuales (RSI, MACD, VWAP slope, deltas, Heiken Ashi).
        Cada indicador favorable suma su peso, con posibles bonificaciones extra.
        """
        score = 0.0
        rsi = state.get('rsi')
        macd_hist = state.get('macdHist')
        macd_hist_delta = state.get('macdHistDelta')
        heiken_color = state.get('heikenColor')
        heiken_count = state.get('heikenCount', 0)
        vwap_slope = state.get('vwapSlope')
        delta1m = state.get('delta1m')
        delta3m = state.get('delta3m')

        # --- RSI ---
        if rsi is not None:
            if direction == 'up':
                if rsi >= self.settings.rsi_up_threshold:
                    score += self.settings.weight_rsi
                elif rsi <= 30:
                    score += self.settings.weight_rsi * 1.5   # oversold extra
                if rsi >= 70:
                    score += self.settings.rsi_extreme_bonus
            else:  # down
                if rsi <= self.settings.rsi_down_threshold:
                    score += self.settings.weight_rsi
                elif rsi >= 70:
                    score += self.settings.weight_rsi * 1.5
                if rsi <= 30:
                    score += self.settings.rsi_extreme_bonus

        # --- MACD ---
        if macd_hist is not None:
            if direction == 'up' and macd_hist > 0:
                score += self.settings.weight_macd
                if macd_hist_delta is not None and macd_hist_delta > 0:
                    score += self.settings.macd_expanding_bonus
            elif direction == 'down' and macd_hist < 0:
                score += self.settings.weight_macd
                if macd_hist_delta is not None and macd_hist_delta < 0:
                    score += self.settings.macd_expanding_bonus

        # --- VWAP slope ---
        if vwap_slope is not None:
            if direction == 'up' and vwap_slope > 0:
                score += self.settings.weight_vwap_slope
            elif direction == 'down' and vwap_slope < 0:
                score += self.settings.weight_vwap_slope

        # --- Deltas (1m y 3m) ---
        if delta1m is not None:
            if direction == 'up' and delta1m > 0:
                score += self.settings.weight_delta
            elif direction == 'down' and delta1m < 0:
                score += self.settings.weight_delta
        if delta3m is not None:
            if direction == 'up' and delta3m > 0:
                score += self.settings.weight_delta
            elif direction == 'down' and delta3m < 0:
                score += self.settings.weight_delta

        # --- Heiken Ashi ---
        if direction == 'up' and heiken_color == "green":
            base = self.settings.weight_heiken
            bonus = min(heiken_count - 1, self.settings.max_heiken_bonus) * self.settings.heiken_consecutive_bonus
            score += base + bonus
        elif direction == 'down' and heiken_color == "red":
            base = self.settings.weight_heiken
            bonus = min(heiken_count - 1, self.settings.max_heiken_bonus) * self.settings.heiken_consecutive_bonus
            score += base + bonus

        return score

    def _record_trade(self, side: str, entry_price: float, exit_price: float, size: float,
                      profit_pct: float, action_type: str):
        """Registra un trade cerrado en el historial."""
        profit_usd = (exit_price - entry_price) * size
        trade = {
            'side': side,
            'entry': entry_price,
            'exit': exit_price,
            'size': size,
            'profit_pct': profit_pct,
            'profit_usd': profit_usd,
            'action': action_type,
            'time': datetime.now().strftime("%H:%M:%S")
        }
        self._add_to_history(trade)
        # También actualizamos el last_trade_pnl para la línea de última acción
        self.last_trade_pnl = profit_pct
        # Log del nuevo balance después del cierre
        if self.settings.dry_run:
            logger.info(f"Balance after trade: ${self.sim_balance:.2f}")

    def _manage_position(self, pos: Dict) -> bool:
        """Gestiona una posición individual. Retorna True si se cerró."""
        side = pos['side']
        token_id = self.yes_token_id if side == "UP" else self.no_token_id
        avg_price = pos['avg_price']
        pos_size = pos['size']

        book = self.get_order_book(token_id)
        current_price = book.get("best_bid")
        if current_price is None:
            logger.warning(f"No best bid for exit for {side}")
            return False

        profit_pct = (current_price - avg_price) / avg_price * 100
        remaining_min = self.get_time_remaining_minutes()
        logger.info(f"Position {side}: entry=${avg_price:.4f}, current=${current_price:.4f}, P&L={profit_pct:.2f}%, time left={remaining_min:.1f}min")

        # --- Trailing stop ---
        if profit_pct > 0:
            if pos.get('trailing_peak') is None or profit_pct > pos['trailing_peak']:
                pos['trailing_peak'] = profit_pct
            elif (pos['trailing_peak'] - profit_pct) >= self.settings.trailing_pct:
                logger.info(f"Trailing stop triggered: peak {pos['trailing_peak']:.2f}% -> current {profit_pct:.2f}%")
                action = "TRAIL"
                self.last_action = f"TRAIL {side} @ {current_price:.4f}"
                if not self.settings.dry_run:
                    place_order(self.settings, side="SELL", token_id=token_id,
                                price=current_price, size=pos_size, tif="FAK")
                else:
                    self.sim_balance += current_price * pos_size
                self.wins += 1
                self._record_trade(side, avg_price, current_price, pos_size, profit_pct, action)
                return True
        else:
            pos['trailing_peak'] = None

        # --- Timeout en positivo ---
        if profit_pct > 0:
            if pos.get('first_positive_ts') is None:
                pos['first_positive_ts'] = time.time()
            time_positive = time.time() - pos['first_positive_ts']
            if time_positive >= self.settings.take_profit_timeout_seconds:
                logger.info(f"Position positive for {time_positive:.1f}s, taking profit")
                action = "TIMEOUT"
                self.last_action = f"TP_TIMEOUT {side} @ {current_price:.4f}"
                if not self.settings.dry_run:
                    place_order(self.settings, side="SELL", token_id=token_id,
                                price=current_price, size=pos_size, tif="FAK")
                else:
                    self.sim_balance += current_price * pos_size
                self.wins += 1
                self._record_trade(side, avg_price, current_price, pos_size, profit_pct, action)
                return True
        else:
            pos['first_positive_ts'] = None

        # --- Take profit y stop loss fijos ---
        target_price = avg_price * (1 + self.settings.profit_target_pct / 100)
        stop_price = avg_price * (1 - self.settings.stop_loss_pct / 100)
        target_price = min(target_price, 0.999)
        stop_price = max(stop_price, 0.001)

        # --- Salida anticipada si el mercado está por cerrar ---
        if remaining_min < self.settings.exit_before_close_minutes and profit_pct > 0:
            logger.info(f"Early exit: market closing soon, taking profit {profit_pct:.2f}% at {current_price:.4f}")
            action = "EARLY"
            self.last_action = f"EARLY {side} @ {current_price:.4f}"
            if not self.settings.dry_run:
                place_order(self.settings, side="SELL", token_id=token_id,
                            price=current_price, size=pos_size, tif="FAK")
            else:
                self.sim_balance += current_price * pos_size
            self.wins += 1
            self._record_trade(side, avg_price, current_price, pos_size, profit_pct, action)
            return True

        if profit_pct >= self.settings.profit_target_pct or current_price >= target_price:
            logger.info(f"Profit target reached ({profit_pct:.2f}%). Selling {pos_size} shares.")
            action = "TP"
            self.last_action = f"TP {side} @ {current_price:.4f}"
            if not self.settings.dry_run:
                place_order(self.settings, side="SELL", token_id=token_id,
                            price=current_price, size=pos_size, tif="FAK")
            else:
                self.sim_balance += current_price * pos_size
            self.wins += 1
            self._record_trade(side, avg_price, current_price, pos_size, profit_pct, action)
            return True
        elif profit_pct <= -self.settings.stop_loss_pct or current_price <= stop_price:
            logger.info(f"Stop loss triggered ({profit_pct:.2f}%). Selling {pos_size} shares.")
            action = "SL"
            self.last_action = f"SL {side} @ {current_price:.4f}"
            if not self.settings.dry_run:
                place_order(self.settings, side="SELL", token_id=token_id,
                            price=current_price, size=pos_size, tif="FAK")
            else:
                self.sim_balance += current_price * pos_size
            self.losses += 1
            self._last_loss_ts = time.time()
            self._record_trade(side, avg_price, current_price, pos_size, profit_pct, action)
            return True
        else:
            self.last_action = f"HOLD {side} ({profit_pct:+.2f}%)"
            return False

    async def run_once_async(self) -> bool:
        time_remaining_str = self.get_time_remaining()
        if time_remaining_str == "CLOSED":
            return False

        up_book, down_book = await self._fetch_order_books_parallel()

        if self.settings.trade_mode.lower() == "directional":
            state = self.load_assistant_state()
            if not state:
                logger.info("No valid assistant state")
                return False

            # ---- Gestión de posiciones abiertas ----
            closed_any = False
            remaining_positions = []
            for pos in self.open_positions:
                if self._manage_position(pos):
                    closed_any = True
                else:
                    remaining_positions.append(pos)
            self.open_positions = remaining_positions
            if closed_any:
                pass

            # ---- Evaluación de nuevas entradas ----
            if len(self.open_positions) >= self.settings.max_positions:
                logger.info(f"Max positions reached ({self.settings.max_positions}), skipping new entry")
                return False

            time_left_min = state.get('timeLeftMin', 15)
            if time_left_min < self.settings.min_time_left_minutes:
                logger.info(f"Too close to close ({time_left_min:.1f} min), skipping")
                return False

            now = time.time()
            if self._last_loss_ts and (now - self._last_loss_ts) < self.settings.cooldown_after_loss_seconds:
                logger.info(f"Cooldown after loss, skipping ({self.settings.cooldown_after_loss_seconds}s)")
                return False

            # --- Elegir método de señal según configuración ---
            if self.settings.signal_mode == "indicator_based":
                # --- Señal basada en indicadores individuales (puntuación) ---
                score_up = self._indicator_score(state, 'up')
                score_down = self._indicator_score(state, 'down')
                logger.info(f"Indicator scores: UP={score_up:.2f} DOWN={score_down:.2f} (min={self.settings.indicator_min_score})")

                # Evaluamos el lado con mayor puntuación
                if score_up >= self.settings.indicator_min_score and score_up > score_down:
                    price = up_book.get("best_ask")
                    if price is not None and price <= self.settings.max_entry_price:
                        # Nuevo filtro: evitar comprar muy barato en los últimos minutos
                        if time_left_min < self.settings.min_price_apply_minutes and price < self.settings.min_price_at_end:
                            logger.info(f"Price {price:.4f} too low in final minutes ({time_left_min:.1f} min left), skipping")
                            return False
                        logger.info(f"INDICATOR SIGNAL: UP (score={score_up:.2f})")
                        await self._execute_entry("UP", state)
                        return True
                    else:
                        logger.info(f"UP price {price if price is not None else 'N/A'} > {self.settings.max_entry_price}, skipping")
                elif score_down >= self.settings.indicator_min_score and score_down > score_up:
                    price = down_book.get("best_ask")
                    if price is not None and price <= self.settings.max_entry_price:
                        if time_left_min < self.settings.min_price_apply_minutes and price < self.settings.min_price_at_end:
                            logger.info(f"Price {price:.4f} too low in final minutes ({time_left_min:.1f} min left), skipping")
                            return False
                        logger.info(f"INDICATOR SIGNAL: DOWN (score={score_down:.2f})")
                        await self._execute_entry("DOWN", state)
                        return True
                    else:
                        logger.info(f"DOWN price {price if price is not None else 'N/A'} > {self.settings.max_entry_price}, skipping")
                else:
                    logger.info("No indicator signal")
                return False

            else:  # modo "confluence" (por defecto)
                # --- Señal de confluencia técnica (TA prob + diff + tech score) ---
                ta_prob_up = state.get('taProbabilityUp', 0)
                ta_prob_down = state.get('taProbabilityDown', 0)
                tech_score_up = self._technical_score(state, 'up')
                tech_score_down = self._technical_score(state, 'down')
                logger.info(f"Confluence: TA prob UP={ta_prob_up:.2f} DOWN={ta_prob_down:.2f} tech UP={tech_score_up:.1f} DOWN={tech_score_down:.1f}")

                prob_diff_up = ta_prob_up - ta_prob_down
                prob_diff_down = ta_prob_down - ta_prob_up

                # UP
                if (ta_prob_up >= self.settings.min_ta_prob and
                    prob_diff_up >= self.settings.ta_prob_diff_min and
                    tech_score_up >= self.settings.tech_score_confluence):
                    price = up_book.get("best_ask")
                    if price is not None and price <= self.settings.max_entry_price:
                        if time_left_min < self.settings.min_price_apply_minutes and price < self.settings.min_price_at_end:
                            logger.info(f"Price {price:.4f} too low in final minutes ({time_left_min:.1f} min left), skipping")
                            return False
                        logger.info(f"CONFLUENCE SIGNAL: UP (TA prob={ta_prob_up:.2f}, diff={prob_diff_up:.2f}, score={tech_score_up:.1f})")
                        await self._execute_entry("UP", state)
                        return True
                    else:
                        logger.info(f"UP price {price if price is not None else 'N/A'} > {self.settings.max_entry_price}, skipping")

                # DOWN
                if (ta_prob_down >= self.settings.min_ta_prob and
                    prob_diff_down >= self.settings.ta_prob_diff_min and
                    tech_score_down >= self.settings.tech_score_confluence):
                    price = down_book.get("best_ask")
                    if price is not None and price <= self.settings.max_entry_price:
                        if time_left_min < self.settings.min_price_apply_minutes and price < self.settings.min_price_at_end:
                            logger.info(f"Price {price:.4f} too low in final minutes ({time_left_min:.1f} min left), skipping")
                            return False
                        logger.info(f"CONFLUENCE SIGNAL: DOWN (TA prob={ta_prob_down:.2f}, diff={prob_diff_down:.2f}, score={tech_score_down:.1f})")
                        await self._execute_entry("DOWN", state)
                        return True
                    else:
                        logger.info(f"DOWN price {price if price is not None else 'N/A'} > {self.settings.max_entry_price}, skipping")

                logger.info("No confluence signal")
                return False

        # Modo arbitraje (no modificado)
        return False

    async def _execute_entry(self, target_side: str, state: dict) -> bool:
        """Ejecuta la entrada en el lado especificado (UP o DOWN) y añade la posición a la lista."""
        token_id = self.yes_token_id if target_side == "UP" else self.no_token_id
        book = self.get_order_book(token_id)
        fill = self._compute_buy_fill(book.get("asks", []), self.settings.position_size)
        if not fill:
            logger.warning("Insufficient liquidity")
            return False

        price = fill["vwap"]
        if price > self.settings.max_entry_price:
            logger.info(f"Entry price {price:.4f} > max {self.settings.max_entry_price}, skipping")
            return False

        logger.info(f"ENTRY {target_side} @ VWAP ${price:.4f}")
        self.last_action = f"ENTRY {target_side} @ {price:.4f}"
        self.last_trade_pnl = None

        if not self.settings.dry_run:
            place_order(self.settings, side="BUY", token_id=token_id,
                        price=price, size=self.settings.position_size, tif="FOK")
        else:
            cost = price * self.settings.position_size
            self.sim_balance -= cost
            self.total_invested += cost
            self.total_shares_bought += self.settings.position_size
            self.trades_executed += 1
            self.open_positions.append({
                'side': target_side,
                'size': self.settings.position_size,
                'avg_price': price,
                'first_positive_ts': None,
                'trailing_peak': None
            })
            logger.info(f"New balance after entry: ${self.sim_balance:.2f}")
        return True

    def render_display(self, state=None):
        clear_screen()
        up_book = self.get_order_book(self.yes_token_id)
        down_book = self.get_order_book(self.no_token_id)

        print(f"{Colors.BOLD}{Colors.WHITE}╔{'═'*78}╗{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}║{' ' * 78}║{Colors.RESET}")
        title = f" BTC 15min ARBITRAGE BOT - {self.settings.trade_mode.upper()} MODE "
        print(f"{Colors.BOLD}{Colors.WHITE}║{title.center(78)}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        market_line = f" Market: {self.market_slug}"
        time_line = f" Time left: {self.get_time_remaining()}"
        print(f"{Colors.BOLD}{Colors.WHITE}║{market_line:<50}{time_line:>28}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        if state:
            pred = state.get('prediction', 'NEUTRAL')
            prob_up = state.get('probabilityUp', 0)
            prob_down = state.get('probabilityDown', 0)
            edge_up = state.get('edgeUp', 0)
            edge_down = state.get('edgeDown', 0)
            ta_prob_up = state.get('taProbabilityUp', 0)
            ta_prob_down = state.get('taProbabilityDown', 0)
            pred_color = Colors.GREEN if pred == 'LONG' else Colors.RED if pred == 'SHORT' else Colors.GRAY
            print(f"{Colors.BOLD}{Colors.WHITE}║ ASSISTANT: {pred_color}{pred:<6}{Colors.RESET}  UP: {Colors.GREEN}{prob_up*100:5.1f}% (edge {edge_up:+.3f})  DOWN: {Colors.RED}{prob_down*100:5.1f}% (edge {edge_down:+.3f}){Colors.RESET}{' ' * 4}║{Colors.RESET}")
            tech_up = self._technical_score(state, 'up')
            tech_down = self._technical_score(state, 'down')
            print(f"{Colors.BOLD}{Colors.WHITE}║ TECH SCORE:  UP: {Colors.GREEN}{tech_up:.1f}{Colors.RESET}  |  DOWN: {Colors.RED}{tech_down:.1f}{Colors.RESET}{' ' * 46}║{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.WHITE}║ TA PROB:     UP: {Colors.GREEN}{ta_prob_up*100:5.1f}%{Colors.RESET}  |  DOWN: {Colors.RED}{ta_prob_down*100:5.1f}%{Colors.RESET}{' ' * 46}║{Colors.RESET}")
            if self.settings.signal_mode == "indicator_based":
                ind_up = self._indicator_score(state, 'up')
                ind_down = self._indicator_score(state, 'down')
                print(f"{Colors.BOLD}{Colors.WHITE}║ IND SCORE:    UP: {Colors.GREEN}{ind_up:.1f}{Colors.RESET}  |  DOWN: {Colors.RED}{ind_down:.1f}{Colors.RESET}{' ' * 46}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║ ASSISTANT: {Colors.GRAY}No data{Colors.RESET}{' ' * 58}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        print(f"{Colors.BOLD}{Colors.WHITE}║ POSITIONS ({len(self.open_positions)}):{' ' * 62}║{Colors.RESET}")
        if self.open_positions:
            for i, pos in enumerate(self.open_positions):
                side = pos['side']
                size = pos['size']
                avg = pos['avg_price']
                token_id = self.yes_token_id if side == "UP" else self.no_token_id
                book = self.get_order_book(token_id)
                bid = book.get("best_bid")
                if bid:
                    pnl = (bid - avg) / avg * 100
                    pnl_color = Colors.GREEN if pnl > 0 else Colors.RED
                    first_pos = pos.get('first_positive_ts')
                    pos_time = f" (pos {time.time()-first_pos:.0f}s)" if first_pos else ""
                    peak_str = f"  peak {pos['trailing_peak']:.2f}%" if pos.get('trailing_peak') is not None else ""
                    line = f"   #{i+1} {side}: {size:.2f} @ {avg:.4f}  Bid: {bid:.4f}  {pnl_color}P&L: {pnl:+.2f}%{pos_time}{peak_str}{Colors.RESET}"
                else:
                    line = f"   #{i+1} {side}: {size:.2f} @ {avg:.4f}  Bid: N/A"
                print(f"{Colors.BOLD}{Colors.WHITE}║{line:<76}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║   No open positions{' ' * 56}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        def fmt_price(p):
            return f"{p:.4f}" if isinstance(p, (int, float)) else "-"

        up_ask = fmt_price(up_book.get("best_ask"))
        up_bid = fmt_price(up_book.get("best_bid"))
        down_ask = fmt_price(down_book.get("best_ask"))
        down_bid = fmt_price(down_book.get("best_bid"))

        print(f"{Colors.BOLD}{Colors.WHITE}║ MARKET PRICES:{' ' * 62}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}║   UP: Ask: {Colors.GREEN}{up_ask}{Colors.RESET}  Bid: {Colors.GREEN}{up_bid}{Colors.RESET}  |  DOWN: Ask: {Colors.RED}{down_ask}{Colors.RESET}  Bid: {Colors.RED}{down_bid}{Colors.RESET}{' ' * 6}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        balance = self.get_balance()
        bal_color = Colors.GREEN if balance > 0 else Colors.RED
        print(f"{Colors.BOLD}{Colors.WHITE}║ Balance: {bal_color}${balance:,.2f}{Colors.RESET}  |  Invested: ${self.total_invested:.2f}  |  Trades: {self.trades_executed}  |  Opportunities: {self.opportunities_found}{' ' * 10}║{Colors.RESET}")

        total_closed = self.wins + self.losses
        if total_closed > 0:
            winrate = self.wins / total_closed * 100
            print(f"{Colors.BOLD}{Colors.WHITE}║ Wins: {Colors.GREEN}{self.wins}{Colors.RESET}  Losses: {Colors.RED}{self.losses}{Colors.RESET}  Winrate: {Colors.YELLOW}{winrate:.2f}%{Colors.RESET}{' ' * 43}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║ No closed trades yet.{' ' * 55}║{Colors.RESET}")

        # --- Línea de última acción con P&L ---
        action_text = self.last_action
        if self.last_trade_pnl is not None:
            action_text += f" (P&L: {self.last_trade_pnl:+.2f}%)"
        visible_len = len(strip_ansi(f"Last action: {action_text}"))
        padding = max(0, 76 - visible_len)
        print(f"{Colors.BOLD}{Colors.WHITE}║ Last action: {Colors.CYAN}{action_text}{Colors.RESET}{' ' * padding}║{Colors.RESET}")

        # --- Historial de últimos 5 trades ---
        if self.trade_history:
            print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.WHITE}║ TRADE HISTORY (last 5):{' ' * 53}║{Colors.RESET}")
            # Cabecera
            header = f" #  Side  Entry   Exit    P&L%   P&L$   Action"
            print(f"{Colors.BOLD}{Colors.WHITE}║ {header:<76}║{Colors.RESET}")
            for idx, trade in enumerate(reversed(self.trade_history[-5:]), 1):
                side = trade['side']
                entry = trade['entry']
                exit_p = trade['exit']
                pct = trade['profit_pct']
                usd = trade['profit_usd']
                action = trade['action'][:6]  # abreviar
                # Colorear según ganancia/pérdida
                pct_color = Colors.GREEN if pct > 0 else Colors.RED if pct < 0 else Colors.WHITE
                usd_color = Colors.GREEN if usd > 0 else Colors.RED if usd < 0 else Colors.WHITE
                line = f"{idx:2}  {side:4}  {entry:.4f}  {exit_p:.4f}  {pct_color}{pct:+6.2f}%{Colors.RESET}  {usd_color}{usd:+6.2f}${Colors.RESET}  {action:<6}"
                print(f"{Colors.BOLD}{Colors.WHITE}║ {line:<76}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║{' ' * 76}║{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.WHITE}║   No trades yet.{' ' * 64}║{Colors.RESET}")

        print(f"{Colors.BOLD}{Colors.WHITE}╚{'═'*78}╝{Colors.RESET}")
        print(f"{Colors.DIM}{Colors.GRAY}Press Ctrl+C to stop{Colors.RESET}")

    async def monitor(self, interval_seconds=1):
        if getattr(self.settings, "use_wss", False):
            await self.monitor_wss()
            return

        scan_count = 0
        try:
            while True:
                scan_count += 1
                if self.get_time_remaining() == "CLOSED":
                    logger.info("Market closed!")
                    self.show_final_summary()
                    try:
                        new_slug = find_current_btc_15min_market()
                        if new_slug != self.market_slug:
                            logger.info(f"New market: {new_slug}")
                            self._initialize_market(forced_slug=new_slug)
                            scan_count = 0
                            continue
                    except Exception as e:
                        logger.error(f"Error finding new market: {e}")
                        await asyncio.sleep(30)
                        continue

                state = self.load_assistant_state()
                self.render_display(state)
                await self.run_once_async()
                logger.info(f"Scan #{scan_count} completed. Waiting {interval_seconds}s...")
                await asyncio.sleep(interval_seconds)

        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Bot stopped by user")
            self.show_final_summary()

    def show_final_summary(self):
        logger.info("\n" + "="*70)
        logger.info("MARKET CLOSED - FINAL SUMMARY")
        logger.info(f"Market: {self.market_slug}")
        logger.info(f"Mode: {'SIMULATION' if self.settings.dry_run else 'REAL'}")
        logger.info(f"Trades executed: {self.trades_executed}")
        logger.info(f"Wins: {self.wins} | Losses: {self.losses}")
        if self.wins + self.losses > 0:
            logger.info(f"Winrate: {self.wins/(self.wins+self.losses)*100:.2f}%")
        logger.info(f"Total invested: ${self.total_invested:.2f}")
        if self.settings.dry_run:
            logger.info(f"Final simulated balance: ${self.sim_balance:.2f}")
        logger.info("="*70)


async def main():
    settings = load_settings()
    if not settings.private_key:
        logger.error("POLYMARKET_PRIVATE_KEY not configured")
        return
    try:
        bot = SimpleArbitrageBot(settings)
        await bot.monitor(interval_seconds=1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())