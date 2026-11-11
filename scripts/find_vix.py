"""Find India VIX's Breeze code.  Usage: python scripts/find_vix.py"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from breeze_connect import BreezeConnect  # noqa: E402

sdk = BreezeConnect(api_key=os.environ["BREEZE_API_KEY"])
sdk.generate_session(api_secret=os.environ["BREEZE_API_SECRET"],
                     session_token=os.environ["BREEZE_SESSION_TOKEN"])

print("1) Searching ICICI's instrument list for 'VIX'...")
hits = set()
for table in getattr(sdk, "stock_script_dict_list", []) or []:
    if isinstance(table, dict):
        for k, v in table.items():
            if "VIX" in str(k).upper() or "VIX" in str(v).upper():
                hits.add((str(k), str(v)))
for k, v in sorted(hits)[:30]:
    print("   ", k, "->", v)
if not hits:
    print("    nothing found")

print("2) Asking for quotes...")
for code in ["INDVIX", "INDIAVIX", "INDIA VIX"]:
    try:
        r = sdk.get_quotes(stock_code=code, exchange_code="NSE", product_type="cash")
        ok = bool(r.get("Success"))
        print(f"    {code:10} -> {'FOUND' if ok else 'not found':10}",
              r["Success"][0].get("ltp") if ok else r.get("Error"))
    except Exception as e:
        print(f"    {code:10} -> error: {str(e)[:80]}")
    time.sleep(2)