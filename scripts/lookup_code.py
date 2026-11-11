"""Find ICICI's Breeze code for an NSE symbol.  Usage: python scripts/lookup_code.py RELIANCE"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
if len(sys.argv) != 2:
    sys.exit("Usage: python scripts/lookup_code.py NSE_SYMBOL")

from breeze_connect import BreezeConnect  # noqa: E402

sdk = BreezeConnect(api_key=os.environ["BREEZE_API_KEY"])
sdk.generate_session(api_secret=os.environ["BREEZE_API_SECRET"],
                     session_token=os.environ["BREEZE_SESSION_TOKEN"])
print(sdk.get_names(exchange_code="NSE", stock_code=sys.argv[1].upper()))
