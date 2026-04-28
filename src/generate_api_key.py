"""Generate or derive Polymarket V2 API credentials from a private key."""
import os
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient

load_dotenv()

def main():
    host        = "https://clob.polymarket.com"
    key         = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder      = os.getenv("POLYMARKET_FUNDER", "") or None
    sig_type    = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    chain_id    = 137

    if not key:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set in .env")

    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=key,
        funder=funder,
        signature_type=sig_type,
    )

    try:
        creds = client.create_or_derive_api_key()
        print("Add these to your .env:\n")
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()