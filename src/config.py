import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=False)


@dataclass
class Settings:
    api_key: str = os.getenv("POLYMARKET_API_KEY", "")
    api_secret: str = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    signature_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    funder: str = os.getenv("POLYMARKET_FUNDER", "")
    market_slug: str = os.getenv("POLYMARKET_MARKET_SLUG", "")
    market_id: str = os.getenv("POLYMARKET_MARKET_ID", "")
    yes_token_id: str = os.getenv("POLYMARKET_YES_TOKEN_ID", "")
    no_token_id: str = os.getenv("POLYMARKET_NO_TOKEN_ID", "")
    ws_url: str = os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com")
    use_wss: bool = os.getenv("USE_WSS", "false").lower() == "true"
    target_pair_cost: float = float(os.getenv("TARGET_PAIR_COST", "0.99"))
    balance_slack: float = float(os.getenv("BALANCE_SLACK", "0.15"))
    order_size: float = float(os.getenv("ORDER_SIZE", "50"))
    order_type: str = os.getenv("ORDER_TYPE", "FOK").upper()
    yes_buy_threshold: float = float(os.getenv("YES_BUY_THRESHOLD", "0.45"))
    no_buy_threshold: float = float(os.getenv("NO_BUY_THRESHOLD", "0.45"))
    verbose: bool = os.getenv("VERBOSE", "false").lower() == "true"
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"
    cooldown_seconds: float = float(os.getenv("COOLDOWN_SECONDS", "10"))
    sim_balance: float = float(os.getenv("SIM_BALANCE", "0"))
    assistant_state_file: str = os.getenv("ASSISTANT_STATE_FILE", "assistant_state.json")
    trade_mode: str = os.getenv("TRADE_MODE", "directional")

    # Parámetros de la estrategia direccional (para el bot principal)
    profit_target_pct: float = float(os.getenv("PROFIT_TARGET_PCT", "12.0"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "6.0"))
    position_size: float = float(os.getenv("POSITION_SIZE", "20"))

    # --- Modalidad de señal (para el bot principal) ---
    signal_mode: str = os.getenv("SIGNAL_MODE", "confluence").lower()

    # Filtros para modo "confluence"
    min_ta_prob: float = float(os.getenv("MIN_TA_PROB", "0.75"))
    ta_prob_diff_min: float = float(os.getenv("TA_PROB_DIFF_MIN", "0.30"))
    tech_score_confluence: float = float(os.getenv("TECH_SCORE_CONFLUENCE", "8.0"))

    # Filtros para modo "indicator_based"
    indicator_min_score: float = float(os.getenv("INDICATOR_MIN_SCORE", "3.0"))
    weight_rsi: float = float(os.getenv("WEIGHT_RSI", "1.0"))
    weight_macd: float = float(os.getenv("WEIGHT_MACD", "1.0"))
    weight_vwap_slope: float = float(os.getenv("WEIGHT_VWAP_SLOPE", "1.0"))
    weight_delta: float = float(os.getenv("WEIGHT_DELTA", "1.0"))
    weight_heiken: float = float(os.getenv("WEIGHT_HEIKEN", "1.0"))
    rsi_up_threshold: float = float(os.getenv("RSI_UP_THRESHOLD", "55.0"))
    rsi_down_threshold: float = float(os.getenv("RSI_DOWN_THRESHOLD", "45.0"))
    rsi_extreme_bonus: float = float(os.getenv("RSI_EXTREME_BONUS", "0.5"))
    macd_expanding_bonus: float = float(os.getenv("MACD_EXPANDING_BONUS", "0.5"))
    heiken_consecutive_bonus: float = float(os.getenv("HEIKEN_CONSECUTIVE_BONUS", "0.5"))
    max_heiken_bonus: float = float(os.getenv("MAX_HEIKEN_BONUS", "2.0"))

    # Filtros comunes (para ambos modos)
    max_entry_price: float = float(os.getenv("MAX_ENTRY_PRICE", "0.70"))
    min_time_left_minutes: float = float(os.getenv("MIN_TIME_LEFT_MINUTES", "4.0"))

    # Filtro para evitar comprar muy barato al final
    min_price_at_end: float = float(os.getenv("MIN_PRICE_AT_END", "0.20"))
    min_price_apply_minutes: float = float(os.getenv("MIN_PRICE_APPLY_MINUTES", "10.0"))

    # Gestión de posiciones
    trailing_pct: float = float(os.getenv("TRAILING_PCT", "2.0"))
    take_profit_timeout_seconds: float = float(os.getenv("TAKE_PROFIT_TIMEOUT_SECONDS", "90"))
    exit_before_close_minutes: float = float(os.getenv("EXIT_BEFORE_CLOSE_MINUTES", "1.5"))
    cooldown_after_loss_seconds: float = float(os.getenv("COOLDOWN_AFTER_LOSS_SECONDS", "5"))
    max_positions: int = int(os.getenv("MAX_POSITIONS", "3"))

    # Parámetros específicos para high_prob_bot.py
    hedge_prob_threshold: float = float(os.getenv("HEDGE_PROB_THRESHOLD", "0.78"))
    hedge_multiplier: float = float(os.getenv("HEDGE_MULTIPLIER", "4.0"))
    max_bets_per_market: int = int(os.getenv("MAX_BETS_PER_MARKET", "4"))
    entry_prob_threshold: float = float(os.getenv("ENTRY_PROB_THRESHOLD", "0.78"))   # umbral para la entrada inicial
    min_time_to_operate_seconds: float = float(os.getenv("MIN_TIME_TO_OPERATE_SECONDS", "6"))  # segundos antes del cierre para no operar


def load_settings() -> Settings:
    return Settings()