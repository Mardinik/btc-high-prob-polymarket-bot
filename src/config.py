import os
from dotenv import load_dotenv

_env_file = os.getenv("ENV_FILE", ".env")
load_dotenv(_env_file, override=False)


class Settings:
    def __init__(self):
        # --- Credenciales ---
        self.private_key    = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.api_key        = os.getenv("POLYMARKET_API_KEY", "")
        self.api_secret     = os.getenv("POLYMARKET_API_SECRET", "")
        self.api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        self.funder         = os.getenv("POLYMARKET_FUNDER", "")

        # --- Mercado ---
        # MARKET_TYPE: "15m" o "5m"
        self.market_type = os.getenv("MARKET_TYPE", "15m").lower().strip()
        # ASSET: "btc", "eth", "sol" (solo btc+eth disponibles en 5m)
        self.asset       = os.getenv("ASSET", "btc").lower().strip()

        # Derivados del tipo de mercado
        _is_5m = self.market_type == "5m"
        self.market_duration: int = 300 if _is_5m else 900
        self.slug_prefix: str     = f"{self.asset}-updown-{self.market_type}"

        # Defaults de ventana según duración
        _def_max = "1.0"  if _is_5m else "5.0"
        _def_min = "20.0" if _is_5m else "45.0"
        _def_lcs = "12.0" if _is_5m else "30.0"

        # --- Estrategia ---
        self.prob_threshold       = float(os.getenv("PROB_THRESHOLD", "0.90"))
        self.entry_window_max_min = float(os.getenv("ENTRY_WINDOW_MAX_MIN", _def_max))
        self.entry_window_min_sec = float(os.getenv("ENTRY_WINDOW_MIN_SEC", _def_min))
        self.low_conv_secs        = float(os.getenv("LOW_CONV_SECS", _def_lcs))
        self.position_size        = float(os.getenv("POSITION_SIZE", "5"))
        self.stop_loss_pct        = float(os.getenv("STOP_LOSS_PCT", "0.0"))

        # --- Operación ---
        self.poll_interval_sec = float(os.getenv("POLL_INTERVAL_SEC", "1.0"))

        # --- Simulación ---
        self.dry_run     = os.getenv("DRY_RUN", "true").lower() == "true"
        self.sim_balance = float(os.getenv("SIM_BALANCE", "400"))


def load_settings() -> Settings:
    return Settings()