import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=False)


@dataclass
class Settings:
    # --- Credenciales Polymarket ---
    private_key:    str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    api_key:        str = os.getenv("POLYMARKET_API_KEY", "")
    api_secret:     str = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    signature_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    funder:         str = os.getenv("POLYMARKET_FUNDER", "")

    # --- Estrategia ≥90% ---
    prob_threshold:       float = float(os.getenv("PROB_THRESHOLD", "0.90"))
    entry_window_max_min: float = float(os.getenv("ENTRY_WINDOW_MAX_MIN", "5.0"))
    entry_window_min_sec: float = float(os.getenv("ENTRY_WINDOW_MIN_SEC", "45.0"))
    position_size:        float = float(os.getenv("POSITION_SIZE", "5"))
    # Stop-loss: vender si el bid cae STOP_LOSS_PCT bajo el precio de entrada.
    # 0.0 = desactivado.
    stop_loss_pct:        float = float(os.getenv("STOP_LOSS_PCT", "0.45"))

    # --- Simulación ---
    dry_run:     bool  = os.getenv("DRY_RUN", "true").lower() == "true"
    sim_balance: float = float(os.getenv("SIM_BALANCE", "400"))


def load_settings() -> Settings:
    return Settings()