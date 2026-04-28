"""
Simple script to test Polymarket V2 balance retrieval.
"""
import os
from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient, ApiCreds

load_dotenv()

def main():
    host           = "https://clob.polymarket.com"
    private_key    = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key        = os.getenv("POLYMARKET_API_KEY")
    api_secret     = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    funder         = os.getenv("POLYMARKET_FUNDER", "") or None

    print("=" * 70)
    print("POLYMARKET V2 BALANCE TEST")
    print("=" * 70)
    print(f"Host:            {host}")
    print(f"Signature Type:  {signature_type}")
    print(f"Private Key:     {'✓' if private_key else '✗ MISSING'}")
    print(f"API Key:         {'✓' if api_key else '✗ (will derive)'}")
    print("=" * 70)

    try:
        # --- Step 1: derive credentials ---
        print("\n1. Deriving API credentials (L1 auth)...")
        l1 = ClobClient(
            host=host,
            chain_id=137,
            key=private_key,
            funder=funder,
            signature_type=signature_type,
        )
        creds = l1.create_or_derive_api_key()
        print(f"   ✓ API Key: {creds.api_key}")

        # --- Step 2: full authenticated client ---
        print("\n2. Building authenticated client (L1 + L2)...")
        client = ClobClient(
            host=host,
            chain_id=137,
            key=private_key,
            funder=funder,
            signature_type=signature_type,
            creds=creds,
        )
        address = client.get_address()
        print(f"   ✓ Wallet: {address}")

        # --- Step 3: balance ---
        print("\n3. Fetching pUSD balance...")
        try:
            result = client.get_balance()
            print(f"   Raw response: {result}")
            if isinstance(result, dict):
                raw = float(result.get("balance", 0))
            else:
                raw = float(result)
            print(f"   💰 pUSD balance: ${raw / 1_000_000:.6f}")
        except Exception as e:
            print(f"   get_balance() failed: {e}")
            print("   Trying get_balance_allowance()...")
            try:
                from py_clob_client_v2 import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                result = client.get_balance_allowance(params)
                print(f"   Raw: {result}")
                raw = float(result.get("balance", 0))
                print(f"   💰 pUSD balance: ${raw / 1_000_000:.6f}")
            except Exception as e2:
                print(f"   Both methods failed: {e2}")

        print("\n" + "=" * 70)
        print("TEST COMPLETE")
        print("=" * 70)

    except Exception as e:
        import traceback
        print(f"\n✗ ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()