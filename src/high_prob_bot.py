"""
High-Confidence Bot — BTC 15-min markets on Polymarket
=======================================================
Strategy: enter the leading side only when implied probability ≥ PROB_THRESHOLD
(default 90%) with 45 seconds to 5 minutes remaining.

Probability definition used here
---------------------------------
  prob = ask_price   (the cost to own 1 share ≡ the implied probability
                      from the buyer's perspective)

  Both sides' asks are checked independently against the threshold.
  They will not sum to exactly 1.0 (the difference is the market's vig).

Order type: GTC limit at current ask.
  • Fills immediately if liquidity exists at that level.
  • Rests in the book until filled OR the market closes (auto-cancel).
  • No loss on unfilled orders.

One bet per market. Stop-loss disabled by default (0.0) — too close to
expiry for meaningful exits; whipsaw risk is high.
Waits for natural market resolution.
"""

import asyncio
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from typing import Optional

from .config import load_settings, Settings
from .lookup import find_active_slug, fetch_market_tokens, slug_end_ts
from .trading import get_client, get_balance, place_buy_gtc

# ---------------------------------------------------------------------------
# Logging — file only; stdout is owned by the TUI renderer
# ---------------------------------------------------------------------------
_file_handler = logging.FileHandler("high_prob_bot.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
class C:
    RST  = "\033[0m"
    BOLD = "\033[1m"; DIM  = "\033[2m"
    RED  = "\033[91m"; GRN  = "\033[92m"; YLW  = "\033[93m"
    CYN  = "\033[96m"; WHT  = "\033[97m"; GRY  = "\033[90m"
    MAG  = "\033[95m"


_IS_TTY = sys.stdout.isatty()


def _term_width() -> int:
    """Return current terminal width, clamped to a sensible range."""
    try:
        return max(60, min(120, shutil.get_terminal_size((80, 24)).columns))
    except Exception:
        return 78


def _strip(t: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", t)


def _pad(t: str, w: int) -> str:
    return t + " " * max(0, w - len(_strip(t)))


def _cc(value: float, pos_col: str, neg_col: str, threshold: float = 1e-4) -> str:
    if not _IS_TTY:
        return ""
    if value > threshold:
        return pos_col
    if value < -threshold:
        return neg_col
    return ""


def _c(flag: bool, col: str) -> str:
    return col if (flag and _IS_TTY) else ""


_ALT_ENTER  = "\033[?1049h"
_ALT_EXIT   = "\033[?1049l"
_CURSOR_OFF = "\033[?25l"
_CURSOR_ON  = "\033[?25h"
_HOME       = "\033[H"
_last_render_lines = 0


def _enter_tui():
    if _IS_TTY:
        sys.stdout.write(_ALT_ENTER + _CURSOR_OFF + "\033[2J" + _HOME)
        sys.stdout.flush()


def _exit_tui():
    if _IS_TTY:
        sys.stdout.write(_CURSOR_ON + _ALT_EXIT)
        sys.stdout.flush()


def _render_buffer(lines: list, W: int):
    global _last_render_lines
    if not _IS_TTY:
        sys.stdout.write("\n".join(_strip(l) for l in lines) + "\n")
        sys.stdout.flush()
        return
    out = [_HOME]
    for line in lines:
        out.append(line + "\033[K\n")
    out.append("\033[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    _last_render_lines = len(lines)


def _progress_bar(elapsed_secs: float, total_secs: float = 900.0, width: int = 30) -> str:
    """Unicode block progress bar representing time elapsed in the market."""
    frac = max(0.0, min(1.0, elapsed_secs / total_secs))
    filled = int(frac * width)
    bar = "▓" * filled + "░" * (width - filled)
    if _IS_TTY:
        col = C.GRN if frac < 0.6 else C.YLW if frac < 0.85 else C.RED
        return f"{col}{bar}{C.RST}"
    return bar


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class HighConfBot:
    def __init__(self, s: Settings):
        self.s = s
        self.client = get_client(s)

        # Active market state
        self.slug:      Optional[str] = None
        self.yes_token: Optional[str] = None
        self.no_token:  Optional[str] = None
        self.end_ts:    Optional[int] = None
        self.outcomes:  list = ["UP", "DOWN"]

        # Current bet — one per market
        self.bet: Optional[dict] = None
        self.stop_triggered = False

        # Latest prices (updated each cycle)
        self._last_prices: dict = {}

        # Session stats
        self.trades: list[dict] = []   # capped at 20
        self.wins = self.losses = self.skipped = 0
        self.total_invested = 0.0
        self.last_scan = "—"
        self.last_scan_ts: float = 0.0

        # Balance
        if s.dry_run:
            self.balance = s.sim_balance
            logger.info(f"[SIM] Starting balance: ${self.balance:.2f}")
        else:
            self.balance = get_balance(s)
            logger.info(f"[LIVE] Starting balance: ${self.balance:.2f}")
        self.start_balance = self.balance

        self._load_market()

    # ------------------------------------------------------------------
    # Market management
    # ------------------------------------------------------------------
    def _load_market(self):
        slug = find_active_slug()
        if not slug:
            self.slug = None
            return

        try:
            info = fetch_market_tokens(slug)
        except Exception as e:
            logger.warning(f"Could not load market {slug}: {e}")
            self.slug = None
            return

        self.slug      = slug
        self.yes_token = info["yes_token_id"]
        self.no_token  = info["no_token_id"]
        self.outcomes  = info.get("outcomes") or ["UP", "DOWN"]
        self.end_ts = slug_end_ts(slug)
        self.bet    = None
        self.stop_triggered = False
        self._last_prices   = {}

        logger.info(
            f"Market loaded: {slug}  closes at {self._time_str()}  "
            f"outcomes={self.outcomes}"
        )

    def _seconds_left(self) -> float:
        if not self.end_ts:
            return 900.0
        return max(0.0, self.end_ts - time.time())

    def _seconds_elapsed(self) -> float:
        if not self.end_ts:
            return 0.0
        return max(0.0, 900.0 - self._seconds_left())

    def _time_str(self) -> str:
        s = int(self._seconds_left())
        if s <= 0:
            return "CLOSED"
        return f"{s // 60:02d}:{s % 60:02d}"

    # ------------------------------------------------------------------
    # Price helpers (async — parallel fetches)
    # ------------------------------------------------------------------
    async def _get_prices(self) -> dict:
        """
        Fetch ask and bid for both sides in parallel using asyncio.gather.
        All 4 API calls run concurrently; total latency ≈ slowest single call.
        """
        loop  = asyncio.get_event_loop()
        out   = {}

        async def _fetch(key: str, token: str, side: str):
            try:
                r = await loop.run_in_executor(
                    None,
                    lambda t=token, s=side: self.client.get_price(token_id=t, side=s),
                )
                out[key] = float(r["price"]) if r and "price" in r else None
            except Exception:
                out[key] = None

        await asyncio.gather(
            _fetch("up_ask", self.yes_token, "buy"),
            _fetch("up_bid", self.yes_token, "sell"),
            _fetch("dn_ask", self.no_token,  "buy"),
            _fetch("dn_bid", self.no_token,  "sell"),
        )
        return out

    def _implied_probs(self, p: dict) -> tuple[Optional[float], Optional[float]]:
        """
        Return (prob_up, prob_dn) using ask prices directly.

        Ask price IS the implied probability: paying $0.92 for a share that
        pays $1 on resolution means the market assigns ~92% probability.

        Both sides are checked independently; they won't sum to 1.0 (the
        difference is the market maker's vig).

        Returns (None, None) when price data is unavailable.
        """
        up_ask = p.get("up_ask")
        dn_ask = p.get("dn_ask")

        if up_ask is None or dn_ask is None:
            return None, None

        return up_ask, dn_ask

    # ------------------------------------------------------------------
    # Unrealized P&L
    # ------------------------------------------------------------------
    def _unrealized_pnl(self, prices: dict) -> Optional[float]:
        """
        Current P&L on the open bet, marked to bid (what you'd receive selling now).
        Positive = in profit, negative = underwater.
        """
        if not self.bet:
            return None
        bid_key = "up_bid" if self.bet["side"] == "UP" else "dn_bid"
        cur_bid = prices.get(bid_key)
        if cur_bid is None:
            return None
        return (cur_bid - self.bet["ask"]) * self.bet["size"]

    # ------------------------------------------------------------------
    # Fill verification (live only)
    # ------------------------------------------------------------------
    def _check_fill(self) -> bool:
        """
        In live mode, verify the order was actually filled before recording P&L.

        Returns True  → order filled (record the trade normally).
        Returns False → order was NOT filled / cancelled (skip P&L recording,
                        restore balance, discard bet).

        In dry_run mode always returns True (no real order was placed).
        """
        if self.s.dry_run:
            return True

        order_id = (self.bet or {}).get("order_id")
        if not order_id:
            # No order ID stored → we can't verify. Log and assume filled to
            # avoid silently discarding a potential real position.
            logger.warning(
                "No order_id on bet — cannot verify fill. "
                "Assuming filled and recording result. Check Polymarket dashboard."
            )
            return True

        try:
            resp = self.client.get_order(order_id)
            status = (resp or {}).get("status", "").upper()
            # Polymarket statuses: MATCHED (filled), OPEN (partial/resting),
            # CANCELED / CANCELLED
            if status in ("MATCHED", "LIVE"):
                logger.info(f"Order {order_id} status={status} → treating as filled")
                return True
            else:
                logger.warning(
                    f"Order {order_id} status={status} → NOT filled. "
                    f"Restoring ${self.bet['cost']:.2f} to balance. No P&L recorded."
                )
                self.balance += self.bet["cost"]
                self.total_invested -= self.bet["cost"]
                return False
        except Exception as e:
            logger.warning(
                f"Could not verify fill for order {order_id}: {e}. "
                "Assuming filled and recording result."
            )
            return True

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    async def _resolve_market(self):
        winner = None
        for _ in range(12):
            p = await self._get_prices()
            if p.get("up_bid") and p["up_bid"] > 0.99:
                winner = "UP"; break
            if p.get("dn_bid") and p["dn_bid"] > 0.99:
                winner = "DOWN"; break
            await asyncio.sleep(0.5)

        if winner is None:
            # Fallback: highest bid wins
            p = await self._get_prices()
            up_b, dn_b = p.get("up_bid"), p.get("dn_bid")
            if up_b is not None and dn_b is not None:
                winner = "UP" if up_b > dn_b else "DOWN"
            else:
                logger.warning("Could not determine winner — will retry next cycle")
                return

        logger.info(f"Market closed. Winner: {winner}")

        if self.bet:
            if self._check_fill():
                self._record_result(winner)
            else:
                self.bet = None   # unfilled — discard silently after balance restore

        if not self.s.dry_run:
            self.balance = get_balance(self.s)

        logger.info(f"Balance after resolution: ${self.balance:.2f}")
        self._load_market()

    def _record_result(self, winner: str):
        bet = self.bet
        won = bet["side"] == winner

        if won:
            self.wins += 1
            pnl_usd = (1.0 - bet["ask"]) * bet["size"]
            pnl_pct = (1.0 / bet["ask"] - 1.0) * 100
            if self.s.dry_run:
                self.balance += bet["size"]          # $1/share on WIN
        else:
            self.losses += 1
            pnl_usd = -bet["cost"]
            pnl_pct = -100.0

        self.trades.append({
            "slug":    self.slug,
            "side":    bet["side"],
            "ask":     bet["ask"],
            "prob":    bet["prob"],
            "size":    bet["size"],
            "cost":    bet["cost"],
            "time":    bet["ts"].strftime("%H:%M:%S"),
            "result":  "WIN" if won else "LOSS",
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        })
        if len(self.trades) > 20:
            self.trades.pop(0)

        icon = "✅" if won else "❌"
        logger.info(
            f"{icon} {'WIN' if won else 'LOSS'} | {bet['side']} "
            f"@ {bet['ask']:.4f} (prob {bet['prob']:.1%}) | "
            f"P&L ${pnl_usd:+.2f} ({pnl_pct:+.1f}%)"
        )
        self.bet = None

    # ------------------------------------------------------------------
    # Stop-loss
    # ------------------------------------------------------------------
    async def _check_stop_loss(self, prices: dict) -> bool:
        if not self.bet or self.stop_triggered or self.s.stop_loss_pct <= 0:
            return False

        bid_key = "up_bid" if self.bet["side"] == "UP" else "dn_bid"
        cur_bid = prices.get(bid_key)
        if cur_bid is None:
            return False

        stop_px = self.bet["ask"] * (1 - self.s.stop_loss_pct)
        if cur_bid > stop_px:
            return False

        loss_usd = (cur_bid - self.bet["ask"]) * self.bet["size"]
        loss_pct = (cur_bid / self.bet["ask"] - 1) * 100
        logger.warning(
            f"🛑 STOP-LOSS | entry={self.bet['ask']:.4f}  "
            f"bid={cur_bid:.4f}  limit={stop_px:.4f}  "
            f"loss=${loss_usd:.2f} ({loss_pct:+.1f}%)"
        )

        if not self.s.dry_run:
            from .trading import place_sell_gtc
            token = self.yes_token if self.bet["side"] == "UP" else self.no_token
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: place_sell_gtc(self.s, token_id=token, price=cur_bid, size=self.bet["size"]),
                )
                logger.info(f"📤 SELL GTC placed @ {cur_bid:.4f}")
            except Exception as e:
                logger.error(f"Stop-loss SELL failed: {e}")
        else:
            recovered = cur_bid * self.bet["size"]
            self.balance += recovered
            logger.info(f"[SIM] Stop recovered ${recovered:.2f} @ bid {cur_bid:.4f}")

        self.trades.append({
            "slug":    self.slug,
            "side":    self.bet["side"],
            "ask":     self.bet["ask"],
            "prob":    self.bet["prob"],
            "size":    self.bet["size"],
            "cost":    self.bet["cost"],
            "time":    self.bet["ts"].strftime("%H:%M:%S"),
            "result":  "STOP",
            "pnl_usd": loss_usd,
            "pnl_pct": loss_pct,
        })
        if len(self.trades) > 20:
            self.trades.pop(0)

        self.losses += 1
        self.stop_triggered = True
        self.bet = None
        return True

    # ------------------------------------------------------------------
    # Main cycle (async)
    # ------------------------------------------------------------------
    async def cycle(self):
        # 1. No market? try to load one.
        if not self.slug:
            self._load_market()
            return

        # 2. Market closed?
        if self._seconds_left() <= 0:
            await self._resolve_market()
            return

        # 3. Bet active → refresh prices, check stop-loss, compute unrealized P&L.
        if self.bet:
            try:
                prices = await self._get_prices()
                self._last_prices = prices
                await self._check_stop_loss(prices)
            except Exception:
                pass
            return

        # 4. Check entry window.
        secs_left = self._seconds_left()
        mins_left = secs_left / 60.0
        in_window = (
            self.s.entry_window_min_sec <= secs_left
            and mins_left <= self.s.entry_window_max_min
        )
        if not in_window:
            self.last_scan = (
                f"waiting — {self._time_str()} left "
                f"(window: last {self.s.entry_window_max_min:.0f} min)"
            )
            return

        # 5. Fetch prices (parallel) and compute probabilities.
        try:
            prices = await self._get_prices()
            self._last_prices = prices
            self.last_scan_ts = time.time()
        except Exception as e:
            logger.error(f"Price fetch error: {e}")
            return

        prob_up, prob_dn = self._implied_probs(prices)

        if prob_up is None:
            self.last_scan = "prices unavailable"
            return

        thr = self.s.prob_threshold
        self.last_scan = (
            f"UP {prob_up:.1%}  DOWN {prob_dn:.1%} "
            f"{'→ SIGNAL' if max(prob_up, prob_dn) >= thr else '→ no signal'}"
        )

        # 6. Signal?
        if prob_up >= thr and prices.get("up_ask"):
            self._enter("UP", prices["up_ask"], prob_up)
        elif prob_dn >= thr and prices.get("dn_ask"):
            self._enter("DOWN", prices["dn_ask"], prob_dn)

    # ------------------------------------------------------------------
    def _enter(self, side: str, ask: float, prob: float):
        size = round(self.s.position_size, 2)
        cost = round(ask * size, 4)
        token = self.yes_token if side == "UP" else self.no_token

        if self.balance < cost:
            logger.warning(
                f"Insufficient balance ${self.balance:.2f} < cost ${cost:.2f} — skipping"
            )
            self.skipped += 1
            return

        order_id: Optional[str] = None
        if not self.s.dry_run:
            try:
                resp     = place_buy_gtc(self.s, token_id=token, price=ask, size=size)
                order_id = (resp or {}).get("orderID") or (resp or {}).get("id")
                if not order_id:
                    logger.warning(f"Order placed but no ID in response: {resp}")
            except Exception as e:
                logger.error(f"Order failed: {e}")
                return

        # Deduct cost optimistically; refreshed from chain after resolution.
        self.balance -= cost
        self.bet = {
            "side": side, "ask": ask, "prob": prob,
            "size": size, "cost": cost, "ts": datetime.now(),
            "order_id": order_id,   # None in dry_run; used for fill check in live
        }
        self.total_invested += cost

        oid_str = f"  order_id={order_id}" if order_id else ""
        logger.info(
            f"{'[SIM] ' if self.s.dry_run else ''}BET {side} "
            f"ask={ask:.4f}  prob={prob:.1%}  {size} shares  cost=${cost:.2f}  "
            f"GTC limit{oid_str}"
        )

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------
    def _weighted_stats(self) -> dict:
        total_won  = sum(t["pnl_usd"] for t in self.trades if t["pnl_usd"] > 0)
        total_lost = sum(abs(t["pnl_usd"]) for t in self.trades if t["pnl_usd"] < 0)
        denom      = total_won + total_lost
        w_wr       = total_won / denom if denom > 0 else None
        asks       = [t["ask"] for t in self.trades if "ask" in t]
        be_wr      = sum(asks) / len(asks) if asks else self.s.prob_threshold
        total      = self.wins + self.losses
        raw_wr     = self.wins / total * 100 if total else None
        return {
            "raw_wr":     raw_wr,
            "w_wr":       w_wr,
            "be_wr":      be_wr,
            "total_won":  total_won,
            "total_lost": total_lost,
        }

    # ------------------------------------------------------------------
    # UI  (adaptive width, progress bar, unrealized P&L)
    # ------------------------------------------------------------------
    def render(self):
        W  = _term_width()
        IW = W - 2          # inner width (between ║ chars)
        WB = f"{C.BOLD}{C.WHT}"; R = C.RST
        buf = []

        def row(t=""):
            buf.append(f"{WB}║{_pad(t, IW)}║{R}")

        def divider(l="╠", m="═", r="╣"):
            buf.append(f"{WB}{l}{m * IW}{r}{R}")

        # ── Header ──────────────────────────────────────────────────────
        title = "HIGH-CONFIDENCE BOT  ·  BTC 15-min  ·  Polymarket"
        buf.append(f"{WB}╔{'═' * IW}╗{R}")
        buf.append(f"{WB}║{title.center(IW)}║{R}")
        divider()

        # ── Config ──────────────────────────────────────────────────────
        mode = f"{C.CYN}SIMULATION{R}" if self.s.dry_run else f"{C.RED}LIVE{R}"
        sl   = f"Stop: {self.s.stop_loss_pct:.0%}" if self.s.stop_loss_pct > 0 else "Stop: off"
        row(
            f" Mode: {mode}  "
            f"Threshold: {C.YLW}{self.s.prob_threshold:.0%}{R}  "
            f"Window: last {self.s.entry_window_max_min:.0f}min  "
            f"Size: {self.s.position_size}sh"
        )
        row(f" {sl}  Poll: {self.s.poll_interval_sec:.1f}s")
        divider()

        # ── Market status + progress bar ─────────────────────────────────
        if self.slug:
            slug_short = self.slug[-18:]
            secs       = int(self._seconds_left())
            mins, secsrem = secs // 60, secs % 60
            elapsed    = self._seconds_elapsed()
            tc = C.GRN if secs > 120 else C.YLW if secs > 45 else C.RED
            bar = _progress_bar(elapsed, width=min(30, IW // 4))
            row(
                f" Market: {C.CYN}{slug_short}{R}  "
                f"Time left: {tc}{mins:02d}:{secsrem:02d}{R}  "
                f"{bar}"
            )
        else:
            row(f" {C.GRY}Waiting for next market…{R}")

        # ── Active bet / stop / idle ─────────────────────────────────────
        if self.bet:
            b     = self.bet
            upnl  = self._unrealized_pnl(self._last_prices)
            upnl_str = ""
            if upnl is not None:
                uc = _cc(upnl, C.GRN, C.RED)
                upnl_str = f"  unrealized: {uc}{upnl:+.2f}${R}"
            stop_px  = b["ask"] * (1 - self.s.stop_loss_pct) if self.s.stop_loss_pct > 0 else None
            sl_str   = f"  stop@{stop_px:.4f}" if stop_px else ""
            row(
                f" {C.GRN}▶ BET ACTIVE{R}  {b['side']}"
                f"  ask={b['ask']:.4f}  prob={b['prob']:.1%}"
                f"  {b['size']}sh  cost=${b['cost']:.2f}"
                f"  @ {b['ts'].strftime('%H:%M:%S')}"
                f"{sl_str}{upnl_str}"
            )
            row(f"   GTC limit — fills immediately or auto-cancels at market close")
        elif self.stop_triggered:
            row(f" {C.RED}⛔ Stop-loss triggered — waiting for market close{R}")
        else:
            row(f" {C.GRY}No active bet — {self.last_scan}{R}")

        divider()

        # ── Stats ────────────────────────────────────────────────────────
        st  = self._weighted_stats()
        net = self.balance - self.start_balance
        nc  = _cc(net, C.GRN, C.RED)

        row(
            f" Wins: {_c(self.wins > 0, C.GRN)}{self.wins}{R}  "
            f"Losses: {_c(self.losses > 0, C.RED)}{self.losses}{R}  "
            f"Skipped: {_c(self.skipped > 0, C.GRY)}{self.skipped}{R}"
        )

        raw_str = f"{st['raw_wr']:.1f}%" if st["raw_wr"] is not None else "n/a"
        if st["w_wr"] is not None:
            wc    = C.GRN if st["w_wr"] >= st["be_wr"] else C.RED
            w_str = f"{wc}{st['w_wr']:.1%}{R}"
        else:
            w_str = "n/a"
        row(
            f" Raw WR: {C.YLW}{raw_str}{R}  "
            f"Weighted WR: {w_str}  "
            f"(break-even: {st['be_wr']:.1%})"
        )

        row(
            f" Net P&L: {nc}{net:+.2f}${R}  "
            f"Balance: ${self.balance:.2f}  "
            f"Invested: ${self.total_invested:.2f}  "
            f"Won: {_cc(st['total_won'], C.GRN, '')}"
            f"+${st['total_won']:.2f}{R}  "
            f"Lost: {_cc(st['total_lost'], '', C.RED)}"
            f"-${st['total_lost']:.2f}{R}"
        )

        # ── Trade history ────────────────────────────────────────────────
        if self.trades:
            divider()
            n_show = min(len(self.trades), 15)
            row(f" HISTORY (last {n_show})")
            row(f"  #  Time      Market            Side  Ask     Prob   Size  Cost    Result  P&L%    P&L$")
            for i, t in enumerate(reversed(self.trades[-n_show:]), 1):
                pc = _cc(t["pnl_pct"], C.GRN, C.RED)
                uc = _cc(t["pnl_usd"], C.GRN, C.RED)
                rc = (
                    (C.GRN if t["result"] == "WIN"
                     else C.YLW if t["result"] == "STOP"
                     else C.RED) if _IS_TTY else ""
                )
                mkt = (t["slug"] or "?")[-15:]
                row(
                    f"  {i:2}  {t['time']}  {mkt}  "
                    f"{t['side']:4}  {t['ask']:.4f}  {t['prob']:.1%}  "
                    f"{t['size']:4.0f}  {t['cost']:6.2f}$  "
                    f"{rc}{t['result']:5}{R}  "
                    f"{pc}{t['pnl_pct']:+6.1f}%{R}  "
                    f"{uc}{t['pnl_usd']:+6.2f}${R}"
                )

        buf.append(f"{WB}╚{'═' * IW}╝{R}")
        buf.append(f"{C.DIM}{C.GRY}  Ctrl+C to stop  ·  log → high_prob_bot.log{R}")

        _render_buffer(buf, W)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def summary(self):
        st  = self._weighted_stats()
        net = self.balance - self.start_balance
        raw = f"{st['raw_wr']:.1f}%" if st["raw_wr"] is not None else "n/a"
        wwr = f"{st['w_wr']:.1%}" if st["w_wr"] is not None else "n/a"
        logger.info("=" * 60)
        logger.info("SESSION SUMMARY")
        logger.info(f"Wins: {self.wins}  Losses: {self.losses}  Skipped: {self.skipped}")
        logger.info(f"Raw WR: {raw}  Weighted WR: {wwr}  Break-even: {st['be_wr']:.1%}")
        logger.info(f"Won: +${st['total_won']:.2f}  Lost: -${st['total_lost']:.2f}")
        logger.info(f"Net P&L: ${net:+.2f}  Balance: ${self.balance:.2f}  Invested: ${self.total_invested:.2f}")
        logger.info("=" * 60)

    async def run(self):
        _enter_tui()
        try:
            while True:
                await self.cycle()
                self.render()
                await asyncio.sleep(self.s.poll_interval_sec)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            _exit_tui()
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
            logging.getLogger().addHandler(sh)
            logger.info("Bot stopped")
            self.summary()


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def _mid(a, b):
    if a is not None and b is not None:
        return (a + b) / 2
    return a or b


# ---------------------------------------------------------------------------
async def main():
    s = load_settings()
    if not s.private_key:
        logger.error("POLYMARKET_PRIVATE_KEY not set in .env")
        return
    bot = HighConfBot(s)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())