"""
Bot de alta probabilidad para mercados de Bitcoin 15 minutos.
Estrategia: apostar por el lado con probabilidad de mercado > ENTRY_PROB_THRESHOLD cuando queden menos de 7.5 minutos.
Permite coberturas sucesivas (hedge) alternando lados, con tamaño = total_actual_del_lado_contrario * HEDGE_MULTIPLIER.
"""

import asyncio
import logging
import os
import sys
import re
from datetime import datetime
from typing import Optional, List, Dict

import httpx

from .config import load_settings
from .lookup import fetch_market_from_slug
from .trading import get_client, place_order, get_balance

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("high_prob_bot.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)

# Colores ANSI
class Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)

def fmt_price(price):
    return "-" if price is None else f"{price:.4f}"

def find_current_btc_15min_market() -> Optional[str]:
    try:
        page_url = "https://polymarket.com/crypto/15M"
        resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        pattern = r'btc-updown-15m-(\d+)'
        matches = re.findall(pattern, resp.text)
        if not matches:
            return None
        now_ts = int(datetime.now().timestamp())
        all_ts = sorted((int(ts) for ts in matches))
        for ts in all_ts:
            if ts <= now_ts < ts + 900:
                return f"btc-updown-15m-{ts}"
        return None
    except Exception as e:
        logger.error(f"Error buscando mercado: {e}")
        return None

class HighProbabilityBot:
    def __init__(self, settings):
        self.settings = settings
        self.client = get_client(settings)

        self.market_slug = None
        self.yes_token_id = None
        self.no_token_id = None
        self.market_end_timestamp = None

        self.active_bets: List[Dict] = []
        self.last_bet_side = None
        self.market_resolved = False

        self.trades_history: List[Dict] = []
        self.wins = 0
        self.losses = 0
        self.total_invested = 0.0

        # Balance: simulado o real
        if self.settings.dry_run:
            self.balance = self.settings.sim_balance if self.settings.sim_balance > 0 else 100.0
        else:
            self.balance = self._get_real_balance()
        self.start_balance = self.balance

        self.last_action = "None"
        self.last_trade_pnl = None

        # Parámetros
        self.entry_prob_threshold = getattr(settings, 'entry_prob_threshold', 0.78)
        self.hedge_prob_threshold = getattr(settings, 'hedge_prob_threshold', 0.78)
        self.hedge_multiplier = getattr(settings, 'hedge_multiplier', 1.0)
        self.max_bets_per_market = getattr(settings, 'max_bets_per_market', 6)
        self.entry_time_min = 7.5
        self.min_time_to_operate_seconds = getattr(settings, 'min_time_to_operate_seconds', 6)
        self.min_time_to_operate_min = self.min_time_to_operate_seconds / 60.0

        self._refresh_market(raise_on_fail=False)

    def _get_real_balance(self) -> float:
        """Obtiene el balance real desde Polymarket."""
        try:
            return get_balance(self.settings)
        except Exception as e:
            logger.error(f"Error obteniendo balance real: {e}")
            return 0.0

    def _update_balance_display(self):
        """Actualiza self.balance con el valor real (si no es simulación)."""
        if not self.settings.dry_run:
            self.balance = self._get_real_balance()

    def _refresh_market(self, forced_slug: Optional[str] = None, raise_on_fail: bool = True):
        if forced_slug:
            self.market_slug = forced_slug
            logger.info(f"Usando mercado forzado: {self.market_slug}")
        else:
            slug = find_current_btc_15min_market()
            if slug is None:
                if raise_on_fail:
                    raise RuntimeError("No hay mercado activo")
                self.market_slug = None
                return
            self.market_slug = slug
            logger.info(f"Mercado detectado: {self.market_slug}")

        try:
            market_info = fetch_market_from_slug(self.market_slug)
        except Exception as e:
            if raise_on_fail:
                raise
            logger.warning(f"Error obteniendo información del mercado {self.market_slug}: {e}")
            self.market_slug = None
            return

        self.yes_token_id = market_info["yes_token_id"]
        self.no_token_id = market_info["no_token_id"]

        match = re.search(r'btc-updown-15m-(\d+)', self.market_slug)
        market_start = int(match.group(1)) if match else None
        self.market_end_timestamp = market_start + 900 if market_start else None

        self.active_bets = []
        self.last_bet_side = None
        self.market_resolved = False

        logger.info(f"UP token ID: {self.yes_token_id}")
        logger.info(f"DOWN token ID: {self.no_token_id}")

    def get_time_remaining_minutes(self) -> float:
        if not self.market_end_timestamp:
            return 15.0
        remaining = self.market_end_timestamp - int(datetime.now().timestamp())
        return max(0.0, remaining / 60.0)

    def get_time_remaining_str(self) -> str:
        remaining_min = self.get_time_remaining_minutes()
        if remaining_min <= 0:
            return "CLOSED"
        minutes = int(remaining_min)
        seconds = int((remaining_min - minutes) * 60)
        return f"{minutes:02d}:{seconds:02d}"

    def get_market_prices(self):
        up_buy = None
        up_sell = None
        down_buy = None
        down_sell = None
        try:
            resp = self.client.get_price(token_id=self.yes_token_id, side='buy')
            up_buy = float(resp.get('price')) if resp and 'price' in resp else None
        except Exception:
            pass
        try:
            resp = self.client.get_price(token_id=self.yes_token_id, side='sell')
            up_sell = float(resp.get('price')) if resp and 'price' in resp else None
        except Exception:
            pass
        try:
            resp = self.client.get_price(token_id=self.no_token_id, side='buy')
            down_buy = float(resp.get('price')) if resp and 'price' in resp else None
        except Exception:
            pass
        try:
            resp = self.client.get_price(token_id=self.no_token_id, side='sell')
            down_sell = float(resp.get('price')) if resp and 'price' in resp else None
        except Exception:
            pass

        return {
            'up_ask': up_buy,
            'up_bid': up_sell,
            'down_ask': down_buy,
            'down_bid': down_sell,
        }

    def get_market_probabilities(self):
        prices = self.get_market_prices()
        up_ask = prices['up_ask']
        down_ask = prices['down_ask']

        if up_ask is not None and down_ask is not None and (up_ask + down_ask) > 0:
            prob_up = up_ask / (up_ask + down_ask)
            prob_down = down_ask / (up_ask + down_ask)
        else:
            prob_up = prob_down = None

        return {
            'up_ask': up_ask,
            'up_bid': prices['up_bid'],
            'down_ask': down_ask,
            'down_bid': prices['down_bid'],
            'prob_up': prob_up,
            'prob_down': prob_down
        }

    def _place_bet(self, side: str, price: float, size: Optional[float] = None, multiplier: Optional[float] = None):
        if size is None:
            size = self.settings.position_size
        cost = price * size

        if self.settings.dry_run:
            if cost > self.balance:
                logger.error(f"Saldo insuficiente simulado: ${self.balance:.2f} < ${cost:.2f}")
                return False
            self.balance -= cost
            self.total_invested += cost
        else:
            # Modo real: no verificamos saldo simulado, intentamos la orden directamente
            token_id = self.yes_token_id if side == "UP" else self.no_token_id
            try:
                place_order(self.settings, side="BUY", token_id=token_id,
                            price=price, size=size, tif="FOK")
            except Exception as e:
                logger.error(f"Error al colocar orden real: {e}")
                return False

        bet = {
            'side': side,
            'entry_price': price,
            'size': size,
            'cost': cost,
            'multiplier': multiplier,
            'timestamp': datetime.now().isoformat()
        }
        self.active_bets.append(bet)
        self.last_bet_side = side

        msg = f"🟢 APOSTANDO {side} a ${price:.4f} con {size} shares (costo ${cost:.2f})"
        if multiplier is not None:
            msg += f" [hedge x{multiplier}]"
        logger.info(msg)
        if self.settings.dry_run:
            logger.info(f"💰 Balance después de apuesta: ${self.balance:.2f}")
        self.last_action = f"BET {side} @ {price:.4f}"
        return True

    def _resolve_bet(self, bet: Dict, winner: str):
        if bet['side'] == winner:
            result = "WIN"
            profit_usd = (1.0 - bet['entry_price']) * bet['size']
            profit_pct = (1.0 / bet['entry_price'] - 1.0) * 100
            self.wins += 1
            if self.settings.dry_run:
                self.balance += bet['size']
        else:
            result = "LOSS"
            profit_usd = -bet['entry_price'] * bet['size']
            profit_pct = -100.0
            self.losses += 1

        try:
            bet_time = datetime.fromisoformat(bet['timestamp']).strftime("%H:%M:%S")
        except:
            bet_time = "?"

        trade = {
            'market': self.market_slug,
            'side': bet['side'],
            'entry': bet['entry_price'],
            'size': bet['size'],
            'cost': bet['cost'],
            'bet_time': bet_time,
            'result': result,
            'profit_usd': profit_usd,
            'profit_pct': profit_pct,
            'timestamp': datetime.now().isoformat()
        }
        self.trades_history.append(trade)
        if len(self.trades_history) > 15:
            self.trades_history.pop(0)

        self.last_trade_pnl = profit_pct
        logger.info(f"📊 RESOLUCIÓN: {result} | {bet['side']} | P&L: ${profit_usd:.2f} ({profit_pct:+.2f}%)")

    def check_market_resolution(self):
        remaining_min = self.get_time_remaining_minutes()
        if remaining_min <= 0:
            prices = self.get_market_prices()
            up_bid = prices['up_bid']
            down_bid = prices['down_bid']
            if up_bid is not None and up_bid > 0.99:
                winner = "UP"
            elif down_bid is not None and down_bid > 0.99:
                winner = "DOWN"
            else:
                return

            if not self.market_resolved and self.active_bets:
                logger.info(f"Mercado cerrado. Ganador: {winner}")
                for bet in self.active_bets:
                    self._resolve_bet(bet, winner)
                self.active_bets = []
                if self.settings.dry_run:
                    logger.info(f"💰 Balance final del mercado: ${self.balance:.2f}")
                else:
                    # Actualizamos el balance real después de resolver
                    self._update_balance_display()
                    logger.info(f"💰 Balance real actualizado: ${self.balance:.2f}")
            self.market_resolved = True
            self.market_slug = None
            self._refresh_market(raise_on_fail=False)

    def run_cycle(self):
        if self.market_slug is None:
            self._refresh_market(raise_on_fail=False)
            if self.market_slug is None:
                return

        try:
            self.check_market_resolution()
        except Exception as e:
            logger.error(f"Error en resolución de mercado: {e}")
            return

        if self.market_resolved:
            return

        try:
            probs = self.get_market_probabilities()
        except Exception as e:
            logger.error(f"Error obteniendo probabilidades: {e}")
            return

        if probs['prob_up'] is None or probs['prob_down'] is None:
            return

        time_left_min = self.get_time_remaining_minutes()
        if time_left_min < self.min_time_to_operate_min:
            return

        total_up = sum(b['size'] for b in self.active_bets if b['side'] == 'UP')
        total_down = sum(b['size'] for b in self.active_bets if b['side'] == 'DOWN')

        # Entrada inicial
        if not self.active_bets:
            if probs['prob_up'] > self.entry_prob_threshold and probs['up_ask'] is not None and time_left_min < self.entry_time_min:
                self._place_bet("UP", probs['up_ask'])
                return
            elif probs['prob_down'] > self.entry_prob_threshold and probs['down_ask'] is not None and time_left_min < self.entry_time_min:
                self._place_bet("DOWN", probs['down_ask'])
                return
            else:
                return

        # Si ya hay apuestas, solo podemos apostar en el lado contrario al último
        if self.last_bet_side is None:
            return

        opposite_side = "UP" if self.last_bet_side == "DOWN" else "DOWN"
        opposite_prob = probs['prob_up'] if opposite_side == "UP" else probs['prob_down']
        opposite_ask = probs['up_ask'] if opposite_side == "UP" else probs['down_ask']

        if opposite_prob > self.hedge_prob_threshold and opposite_ask is not None:
            if opposite_side == "UP":
                other_total = total_down
            else:
                other_total = total_up
            size = other_total * self.hedge_multiplier
            if size > 0:
                self._place_bet(opposite_side, opposite_ask, size, self.hedge_multiplier)
                return

        if len(self.active_bets) >= self.max_bets_per_market:
            logger.debug(f"Límite de apuestas alcanzado ({self.max_bets_per_market})")

    def render_display(self):
        clear_screen()
        probs = self.get_market_probabilities() if self.market_slug else {}
        time_left = self.get_time_remaining_str() if self.market_slug else "N/A"

        print(f"{Colors.BOLD}{Colors.WHITE}╔{'═'*78}╗{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}║{' ' * 78}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}║{'     HIGH PROBABILITY BOT (15min BTC)     '.center(78)}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        if self.market_slug:
            market_line = f" Market: {self.market_slug}"
            time_line = f" Time left: {time_left}"
            print(f"{Colors.BOLD}{Colors.WHITE}║{market_line:<50}{time_line:>28}║{Colors.RESET}")

            prob_up = probs.get('prob_up')
            prob_down = probs.get('prob_down')
            if prob_up is not None:
                prob_line = f" Market probs: UP {Colors.GREEN}{prob_up:.1%}{Colors.RESET}  DOWN {Colors.RED}{prob_down:.1%}{Colors.RESET}"
            else:
                prob_line = " Market probs: N/A"
            print(f"{Colors.BOLD}{Colors.WHITE}║{prob_line:<76}║{Colors.RESET}")

            up_ask = probs.get('up_ask')
            up_bid = probs.get('up_bid')
            down_ask = probs.get('down_ask')
            down_bid = probs.get('down_bid')
            price_line = f" Prices: UP ask {Colors.GREEN}{fmt_price(up_ask)}{Colors.RESET} bid {Colors.GREEN}{fmt_price(up_bid)}{Colors.RESET} | DOWN ask {Colors.RED}{fmt_price(down_ask)}{Colors.RESET} bid {Colors.RED}{fmt_price(down_bid)}{Colors.RESET}"
            print(f"{Colors.BOLD}{Colors.WHITE}║{price_line:<76}║{Colors.RESET}")

            if self.active_bets:
                total_up = sum(b['size'] for b in self.active_bets if b['side'] == 'UP')
                total_down = sum(b['size'] for b in self.active_bets if b['side'] == 'DOWN')
                print(f"{Colors.BOLD}{Colors.WHITE}║ Active bets: UP {total_up} shares | DOWN {total_down} shares{' ' * 36}║{Colors.RESET}")
                for i, bet in enumerate(self.active_bets):
                    side = bet['side']
                    entry = bet['entry_price']
                    size = bet['size']
                    cost = bet['cost']
                    mult = bet.get('multiplier')
                    mult_str = f" (x{mult})" if mult else ""
                    current_price = probs.get(f"{side.lower()}_bid")
                    if current_price is not None:
                        unrealized = (current_price - entry) * size
                        unrealized_pct = (current_price / entry - 1) * 100 if entry > 0 else 0
                        pnl_color = Colors.GREEN if unrealized > 0 else Colors.RED if unrealized < 0 else Colors.WHITE
                        bet_info = f"   #{i+1} {side}: {size}{mult_str} @ {entry:.4f} (cost ${cost:.2f}) | Current: {current_price:.4f} | {pnl_color}Unrealized: {unrealized:+.2f}$ ({unrealized_pct:+.2f}%){Colors.RESET}"
                    else:
                        bet_info = f"   #{i+1} {side}: {size}{mult_str} @ {entry:.4f} (cost ${cost:.2f}) | Current: N/A"
                    print(f"{Colors.BOLD}{Colors.WHITE}║{bet_info:<76}║{Colors.RESET}")
                if total_up > 0 and total_down > 0:
                    total_cost = sum(b['cost'] for b in self.active_bets)
                    total_payout = total_up + total_down
                    fixed_result = total_payout - total_cost
                    fixed_result_color = Colors.GREEN if fixed_result > 0 else Colors.RED if fixed_result < 0 else Colors.WHITE
                    hedge_info = f"   [HEDGE ACTIVE] Resultado fijo: {fixed_result_color}{fixed_result:+.2f}${Colors.RESET} (costo total ${total_cost:.2f})"
                    print(f"{Colors.BOLD}{Colors.WHITE}║{hedge_info:<76}║{Colors.RESET}")
            else:
                print(f"{Colors.BOLD}{Colors.WHITE}║ No active bets{' ' * 63}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║{'Esperando siguiente mercado...':^76}║{Colors.RESET}")

        print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")

        action_text = self.last_action
        if self.last_trade_pnl is not None:
            action_text += f" (P&L: {self.last_trade_pnl:+.2f}%)"
        visible_len = len(strip_ansi(f"Last action: {action_text}"))
        padding = max(0, 76 - visible_len)
        print(f"{Colors.BOLD}{Colors.WHITE}║ Last action: {Colors.CYAN}{action_text}{Colors.RESET}{' ' * padding}║{Colors.RESET}")

        total_trades = self.wins + self.losses
        winrate = (self.wins / total_trades * 100) if total_trades > 0 else 0
        net_profit = self.balance - self.start_balance
        profit_color = Colors.GREEN if net_profit > 0 else Colors.RED if net_profit < 0 else Colors.WHITE
        print(f"{Colors.BOLD}{Colors.WHITE}║ Wins: {Colors.GREEN}{self.wins}{Colors.RESET}  Losses: {Colors.RED}{self.losses}{Colors.RESET}  Winrate: {Colors.YELLOW}{winrate:.2f}%{Colors.RESET}  Net P&L: {profit_color}${net_profit:+.2f}{Colors.RESET}  Balance: ${self.balance:.2f}  Invested: ${self.total_invested:.2f}{' ' * 4}║{Colors.RESET}")

        if self.trades_history:
            print(f"{Colors.BOLD}{Colors.WHITE}╠{'═'*78}╣{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.WHITE}║ TRADE HISTORY (last 15):{' ' * 52}║{Colors.RESET}")
            header = f" #  Time    Market        Side  Entry   Cost    Result  P&L%       P&L$"
            print(f"{Colors.BOLD}{Colors.WHITE}║ {header:<76}║{Colors.RESET}")
            for idx, trade in enumerate(reversed(self.trades_history[-15:]), 1):
                bet_time = trade.get('bet_time', '?')
                market_short = trade['market'][-12:]
                side = trade['side']
                entry = trade['entry']
                cost = trade.get('cost', 0)
                result = trade['result']
                pct = trade['profit_pct']
                usd = trade['profit_usd']
                pct_color = Colors.GREEN if pct > 0 else Colors.RED if pct < 0 else Colors.WHITE
                usd_color = Colors.GREEN if usd > 0 else Colors.RED if usd < 0 else Colors.WHITE
                line = f"{idx:2}  {bet_time}  {market_short}  {side:4}  {entry:.4f}  {cost:6.2f}$  {result:5}  {pct_color}{pct:+6.2f}%{Colors.RESET}  {usd_color}{usd:+7.2f}$"
                print(f"{Colors.BOLD}{Colors.WHITE}║ {line:<76}║{Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.WHITE}║{' ' * 76}║{Colors.RESET}")
            print(f"{Colors.BOLD}{Colors.WHITE}║   No trades yet.{' ' * 64}║{Colors.RESET}")

        print(f"{Colors.BOLD}{Colors.WHITE}╚{'═'*78}╝{Colors.RESET}")
        print(f"{Colors.DIM}{Colors.GRAY}Press Ctrl+C to stop{Colors.RESET}")

    async def monitor(self, interval_seconds=1):
        try:
            while True:
                self.run_cycle()
                self.render_display()
                # Actualizar balance real cada 10 segundos (modo real)
                if not self.settings.dry_run and not self.market_resolved:
                    self._update_balance_display()
                await asyncio.sleep(interval_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Bot detenido por el usuario")
            self.show_final_summary()

    def show_final_summary(self):
        total_trades = self.wins + self.losses
        winrate = (self.wins / total_trades * 100) if total_trades > 0 else 0
        net_profit = self.balance - self.start_balance
        logger.info("\n" + "="*70)
        logger.info("RESUMEN FINAL")
        logger.info(f"Mercados procesados: {len(self.trades_history)}")
        logger.info(f"Wins: {self.wins} | Losses: {self.losses} | Winrate: {winrate:.2f}%")
        logger.info(f"Beneficio neto: ${net_profit:.2f} (Balance final: ${self.balance:.2f})")
        logger.info("="*70)


async def main():
    settings = load_settings()
    if not settings.private_key:
        logger.error("POLYMARKET_PRIVATE_KEY no configurado")
        return
    bot = HighProbabilityBot(settings)
    await bot.monitor(interval_seconds=1)

if __name__ == "__main__":
    asyncio.run(main())