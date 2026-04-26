"""
High-Confidence Bot — BTC 15-min markets on Polymarket
=======================================================
Strategy: enter the leading side only when implied probability ≥ PROB_THRESHOLD
(default 90%) with 45 seconds to 5 minutes remaining.

Order type: GTC limit at current ask.
  • If there is liquidity at that price → fills immediately.
  • If not → order rests in the book until filled OR the market closes,
    at which point Polymarket auto-cancels it. Zero loss on unfilled orders.

One bet per market. No stop-loss (too close to expiry for meaningful exits).
Waits for natural market resolution.
"""

import asyncio
import logging
import re
import sys
import time
from datetime import datetime
from typing import Optional

from .config import load_settings, Settings
from .lookup import find_active_slug, fetch_market_tokens
from .trading import get_client, get_balance, place_buy_gtc

# ---------------------------------------------------------------------------
# Logging — file + stdout
# ---------------------------------------------------------------------------
# File handler only — stdout is owned by the TUI renderer.
# Startup messages and summaries are written to high_prob_bot.log.
_file_handler = logging.FileHandler("high_prob_bot.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
class C:
    RST = "\033[0m"
    BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GRN = "\033[92m"; YLW = "\033[93m"
    CYN = "\033[96m"; WHT = "\033[97m"; GRY = "\033[90m"


# True if stdout is a real terminal that supports ANSI
_IS_TTY = sys.stdout.isatty()


def _strip(t: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", t)


def _pad(t: str, w: int = 78) -> str:
    return t + " " * max(0, w - len(_strip(t)))


def _cc(value: float, pos_col: str, neg_col: str, rst: str, threshold: float = 1e-4) -> str:
    """Return color code only when |value| > threshold. Zero = no color."""
    if not _IS_TTY:
        return ""
    if value > threshold:
        return pos_col
    if value < -threshold:
        return neg_col
    return ""


def _c(flag: bool, col: str, rst: str) -> str:
    """Return color code only when flag is True and stdout is a tty."""
    return col if (flag and _IS_TTY) else ""


_last_render_lines = 0

# ANSI alternate screen + cursor control
_ALT_ENTER  = "\033[?1049h"   # enter alternate screen (blank, separate from scrollback)
_ALT_EXIT   = "\033[?1049l"   # exit alternate screen (restores original)
_CURSOR_OFF = "\033[?25l"     # hide cursor during redraw
_CURSOR_ON  = "\033[?25h"     # show cursor on exit
_HOME       = "\033[H"        # cursor to top-left


def _enter_tui():
    """Switch to alternate screen and hide cursor (tty only)."""
    if _IS_TTY:
        sys.stdout.write(_ALT_ENTER + _CURSOR_OFF)
        sys.stdout.flush()


def _exit_tui():
    """Restore original screen and cursor (tty only)."""
    if _IS_TTY:
        sys.stdout.write(_CURSOR_ON + _ALT_EXIT)
        sys.stdout.flush()


def _render_buffer(lines: list):
    """
    Redraw in-place — no flicker, no leftover text.
    On a real tty: uses alternate screen + home + erase-to-EOL per line.
    On a non-tty (piped/redirected): plain newlines (logs/CI friendly).
    """
    global _last_render_lines
    if not _IS_TTY:
        # Non-interactive: just print the last line as a status tick
        sys.stdout.write("\n".join(_strip(l) for l in lines) + "\n")
        sys.stdout.flush()
        return
    out = [_HOME]
    for line in lines:
        out.append(line + "\033[K\n")
    for _ in range(max(0, _last_render_lines - len(lines))):
        out.append("\033[K\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    _last_render_lines = len(lines)


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

        # Current bet — one per market
        # Fields: side, ask, size, cost, ts
        self.bet: Optional[dict] = None
        self.stop_triggered = False

        # Session stats
        self.trades: list[dict] = []   # capped at 20
        self.wins = self.losses = self.skipped = 0
        self.total_invested = 0.0
        self.last_scan = "—"

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
        """Find and load the active market. Silently clears state on failure."""
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
        m = re.search(r"btc-updown-15m-(\d+)", slug)
        self.end_ts = int(m.group(1)) + 900 if m else None
        self.bet    = None
        self.stop_triggered = False

        logger.info(f"Market loaded: {slug}  closes at {self._time_str()}")

    def _seconds_left(self) -> float:
        if not self.end_ts:
            return 900.0
        return max(0.0, self.end_ts - time.time())

    def _time_str(self) -> str:
        s = int(self._seconds_left())
        if s <= 0:
            return "CLOSED"
        return f"{s // 60:02d}:{s % 60:02d}"

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------
    def _get_prices(self) -> dict:
        """Fetch ask and bid for both sides. Returns None for any that fail."""
        out = {k: None for k in ("up_ask", "up_bid", "dn_ask", "dn_bid")}
        pairs = [
            ("up_ask", self.yes_token, "buy"),
            ("up_bid", self.yes_token, "sell"),
            ("dn_ask", self.no_token,  "buy"),
            ("dn_bid", self.no_token,  "sell"),
        ]
        for key, token, side in pairs:
            try:
                r = self.client.get_price(token_id=token, side=side)
                if r and "price" in r:
                    out[key] = float(r["price"])
            except Exception:
                pass
        return out

    @staticmethod
    def _implied_probs(p: dict) -> tuple[Optional[float], Optional[float]]:
        """
        Normalised mid-price probabilities.
        Returns (prob_up, prob_down) or (None, None) if prices unavailable.
        """
        up = _mid(p["up_ask"], p["up_bid"])
        dn = _mid(p["dn_ask"], p["dn_bid"])
        if not up or not dn:
            return None, None
        total = up + dn
        return up / total, dn / total

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def _resolve_market(self):
        """
        Determine the winning side after close and record the bet result.
        Reloads the next market afterwards.
        """
        winner = None
        for _ in range(12):
            p = self._get_prices()
            if p["up_bid"] and p["up_bid"] > 0.99:
                winner = "UP"; break
            if p["dn_bid"] and p["dn_bid"] > 0.99:
                winner = "DOWN"; break
            time.sleep(0.5)

        if winner is None:
            # Fallback: highest bid wins
            p = self._get_prices()
            if p["up_bid"] and p["dn_bid"]:
                winner = "UP" if p["up_bid"] > p["dn_bid"] else "DOWN"
            else:
                logger.warning("Could not determine winner — will retry next cycle")
                return

        logger.info(f"Market closed. Winner: {winner}")

        if self.bet:
            self._record_result(winner)

        if not self.s.dry_run:
            self.balance = get_balance(self.s)

        logger.info(f"Balance: ${self.balance:.2f}")
        self._load_market()

    def _record_result(self, winner: str):
        bet = self.bet
        won = bet["side"] == winner

        if won:
            self.wins += 1
            pnl_usd = (1.0 - bet["ask"]) * bet["size"]
            pnl_pct = (1.0 / bet["ask"] - 1.0) * 100
            if self.s.dry_run:
                self.balance += bet["size"]          # receive $1/share
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
    def _check_stop_loss(self, prices: dict) -> bool:
        """
        Returns True if the stop-loss was just triggered.
        Places a SELL GTC at current bid (or simulates it).
        """
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
                place_sell_gtc(self.s, token_id=token, price=cur_bid, size=self.bet["size"])
                logger.info(f"📤 SELL GTC placed @ {cur_bid:.4f}")
            except Exception as e:
                logger.error(f"Stop-loss SELL failed: {e}")
        else:
            recovered = cur_bid * self.bet["size"]
            self.balance += recovered
            logger.info(f"[SIM] Stop recovered ${recovered:.2f} @ bid {cur_bid:.4f}")

        # Record as STOP trade
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
    # Main cycle
    # ------------------------------------------------------------------
    def cycle(self):
        # 1. No market? try to load one.
        if not self.slug:
            self._load_market()
            return

        # 2. Market closed?
        if self._seconds_left() <= 0:
            self._resolve_market()
            return

        # 3. Bet active → check stop-loss, then wait for resolution.
        if self.bet:
            try:
                prices = self._get_prices()
                self._check_stop_loss(prices)
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

        # 5. Fetch prices and compute probabilities.
        try:
            prices = self._get_prices()
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
            f"({'SIGNAL' if max(prob_up, prob_dn) >= thr else 'no signal'})"
        )

        # 6. Signal?
        if prob_up >= thr and prices["up_ask"]:
            self._enter("UP", prices["up_ask"], prob_up)
        elif prob_dn >= thr and prices["dn_ask"]:
            self._enter("DOWN", prices["dn_ask"], prob_dn)

    # ------------------------------------------------------------------
    def _enter(self, side: str, ask: float, prob: float):
        size = round(self.s.position_size, 2)
        cost = round(ask * size, 4)
        token = self.yes_token if side == "UP" else self.no_token

        # Balance check
        if self.balance < cost:
            logger.warning(
                f"Insufficient balance ${self.balance:.2f} < cost ${cost:.2f} — skipping"
            )
            self.skipped += 1
            return

        # Place order (or simulate)
        if not self.s.dry_run:
            try:
                place_buy_gtc(self.s, token_id=token, price=ask, size=size)
            except Exception as e:
                logger.error(f"Order failed: {e}")
                return
            self.balance -= cost   # optimistic deduction; refreshed after resolution
        else:
            self.balance -= cost

        self.bet = {
            "side": side, "ask": ask, "prob": prob,
            "size": size, "cost": cost, "ts": datetime.now(),
        }
        self.total_invested += cost

        logger.info(
            f"{'[SIM] ' if self.s.dry_run else ''}BET {side} "
            f"ask={ask:.4f}  prob={prob:.1%}  {size} shares  cost=${cost:.2f}  "
            f"GTC limit (fills or auto-cancels at close)"
        )


    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------
    def _weighted_stats(self) -> dict:
        """
        Weighted win rate: fraction of dollars-at-risk recovered as profit.

          w_wr = total_won / (total_won + total_lost)

        Unlike raw WR (counts), this weights by actual dollar impact, so one
        large loss is not hidden by several small wins.

        break-even WR = average entry price, because for a binary market:
          WR × (1 - ask) = (1 - WR) × ask  →  WR = ask
        """
        total_won  = sum(t["pnl_usd"] for t in self.trades if t["pnl_usd"] > 0)
        total_lost = sum(abs(t["pnl_usd"]) for t in self.trades if t["pnl_usd"] < 0)
        denom = total_won + total_lost

        w_wr = total_won / denom if denom > 0 else None

        asks  = [t["ask"] for t in self.trades if "ask" in t]
        be_wr = sum(asks) / len(asks) if asks else self.s.prob_threshold

        total  = self.wins + self.losses
        raw_wr = self.wins / total * 100 if total else None

        return {
            "raw_wr":     raw_wr,
            "w_wr":       w_wr,
            "be_wr":      be_wr,
            "total_won":  total_won,
            "total_lost": total_lost,
        }

    # ------------------------------------------------------------------
    # UI  (buffer-based, no flicker)
    # ------------------------------------------------------------------
    def render(self):
        W = f"{C.BOLD}{C.WHT}"; R = C.RST
        buf = []

        def row(t=""):
            buf.append(f"{W}║{_pad(t)}║{R}")

        def divider():
            buf.append(f"{W}╠{'═' * 78}╣{R}")

        # Header
        title = "HIGH-CONFIDENCE BOT  ·  BTC 15-min  ·  Polymarket"
        buf.append(f"{W}╔{'═' * 78}╗{R}")
        buf.append(f"{W}║{title.center(78)}║{R}")
        divider()

        # Mode + config (two rows so nothing overflows 78 chars)
        mode = f"{C.CYN}SIMULATION{R}" if self.s.dry_run else f"{C.RED}LIVE{R}"
        sl   = f"Stop: {self.s.stop_loss_pct:.0%}" if self.s.stop_loss_pct > 0 else "Stop: off"
        row(
            f" Mode: {mode}   Threshold: {C.YLW}{self.s.prob_threshold:.0%}{R}"
            f"   Window: last {self.s.entry_window_max_min:.0f} min   Size: {self.s.position_size} shares"
        )
        row(f" {sl}   Poll: {self.s.poll_interval_sec:.1f}s")
        divider()

        # Market status
        if self.slug:
            slug_short = self.slug[-16:]
            secs = int(self._seconds_left())
            mins, secsrem = secs // 60, secs % 60
            tc = C.GRN if secs > 120 else C.YLW if secs > 45 else C.RED
            row(f" Market: {slug_short}   Time left: {tc}{mins:02d}:{secsrem:02d}{R}")
        else:
            row(f" {C.GRY}Waiting for next market…{R}")

        # Bet / stop / idle
        if self.bet:
            b = self.bet
            stop_px = b["ask"] * (1 - self.s.stop_loss_pct) if self.s.stop_loss_pct > 0 else None
            sl_str  = f"   stop @ {stop_px:.4f}" if stop_px else ""
            row(
                f" {C.GRN}▶ BET ACTIVE{R}  {b['side']}  ask={b['ask']:.4f}"
                f"  prob={b['prob']:.1%}  size={b['size']}sh  cost=${b['cost']:.2f}"
                f"  @ {b['ts'].strftime('%H:%M:%S')}{sl_str}"
            )
            row(f"   GTC limit — fills immediately or auto-cancels at market close")
        elif self.stop_triggered:
            row(f" {C.RED}⛔ Stop-loss triggered — waiting for market close{R}")
        else:
            row(f" {C.GRY}No active bet — {self.last_scan}{R}")

        divider()

        # Stats rows
        st  = self._weighted_stats()
        net = self.balance - self.start_balance
        nc  = _cc(net, C.GRN, C.RED, R)

        # Stats row 1: counts
        row(
            f" Wins: {_c(self.wins > 0, C.GRN, R)}{self.wins}{R}  "
            f"Losses: {_c(self.losses > 0, C.RED, R)}{self.losses}{R}  "
            f"Skipped: {_c(self.skipped > 0, C.GRY, R)}{self.skipped}{R}"
        )

        # Stats row 2: WR metrics
        raw_str = f"{st['raw_wr']:.1f}%" if st["raw_wr"] is not None else "n/a"
        if st["w_wr"] is not None:
            wc = C.GRN if st["w_wr"] >= st["be_wr"] else C.RED
            w_str = f"{wc}{st['w_wr']:.1%}{R}"
        else:
            w_str = "n/a"
        row(
            f" Raw WR: {C.YLW}{raw_str}{R}  "
            f"Weighted WR: {w_str}  "
            f"(break-even: {st['be_wr']:.1%})"
        )

        # Stats row 3: P&L + balance + invested
        row(
            f" Net P&L: {nc}{net:+.2f}${R}  "
            f"Balance: ${self.balance:.2f}  "
            f"Invested: ${self.total_invested:.2f}  "
            f"Won: {_cc(st['total_won'], C.GRN, '', R)}+${st['total_won']:.2f}{R}  "
            f"Lost: {_cc(st['total_lost'], '', C.RED, R)}-${st['total_lost']:.2f}{R}"
        )

        # Trade history
        if self.trades:
            divider()
            row(f" HISTORY (last {min(len(self.trades), 15)})")
            row(f"  #  Time      Market           Side  Ask     Prob   Size  Cost   Result  P&L%    P&L$")
            for i, t in enumerate(reversed(self.trades[-15:]), 1):
                pc = _cc(t["pnl_pct"], C.GRN, C.RED, R)
                uc = _cc(t["pnl_usd"], C.GRN, C.RED, R)
                rc = (C.GRN if t["result"] == "WIN" else (C.YLW if t["result"] == "STOP" else C.RED)) if _IS_TTY else ""
                mkt = (t["slug"] or "?")[-14:]
                row(
                    f"  {i:2}  {t['time']}  {mkt}  "
                    f"{t['side']:4}  {t['ask']:.4f}  {t['prob']:.1%}  "
                    f"{t['size']:4.0f}  {t['cost']:5.2f}$  "
                    f"{rc}{t['result']:5}{R}  "
                    f"{pc}{t['pnl_pct']:+6.1f}%{R}  "
                    f"{uc}{t['pnl_usd']:+6.2f}${R}"
                )

        buf.append(f"{W}╚{'═' * 78}╝{R}")
        buf.append(f"{C.DIM}{C.GRY}  Ctrl+C to stop{R}")

        _render_buffer(buf)

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
                self.cycle()
                self.render()
                await asyncio.sleep(self.s.poll_interval_sec)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            _exit_tui()
            # Restore a usable stdout logger for the summary
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
            logging.getLogger().addHandler(sh)
            logger.info("Bot stopped")
            self.summary()


# ---------------------------------------------------------------------------
def _mid(a, b):
    if a is not None and b is not None:
        return (a + b) / 2
    return a or b


async def main():
    s = load_settings()
    if not s.private_key:
        logger.error("POLYMARKET_PRIVATE_KEY not set in .env")
        return
    bot = HighConfBot(s)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())