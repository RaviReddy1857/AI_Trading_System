"""Refresh the Nifty 50 part of the watchlist in config.yaml from a constituents file.

    python scripts/update_watchlist.py                  # data/nifty50.csv -> config.yaml
    python scripts/update_watchlist.py --dry-run        # show what would change, write nothing
    python scripts/update_watchlist.py --csv path/to/ind_nifty50list.csv

Download the file from niftyindices.com (Nifty 50 -> constituents, columns Company Name,
Industry, Symbol, Series, ISIN Code). Each NSE symbol is mapped to its Breeze code with
ICICI's own instrument list (the SecurityMaster file breeze-connect downloads; no login is
needed). The ISIN must match too. Symbols that cannot be mapped are listed and left out.
Only the lines between the "nifty50" markers in config.yaml change; NIFTY, BANKNIFTY and
INDIAVIX stay as they are. The new config is validated before it is written.

Afterwards download history for new symbols: python -m app.history
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.feeds.breeze_feed import nse_scrip_master  # noqa: E402  (SDK stays in breeze_feed.py)
from app.watchlist import update_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(ROOT / "data" / "nifty50.csv"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    text = Path(args.csv).read_text(encoding="utf-8-sig")
    print("Loading ICICI's NSE instrument list ...")
    rows = nse_scrip_master()
    print(f"  {len(rows)} instruments")
    rep = update_config(Path(args.config), text, rows, write=not args.dry_run)

    print(f"Constituents in file: {rep['constituents']}")
    print(f"Mapped: {len(rep['mapped'])}")
    for sym, code, sector in rep["mapped"]:
        print(f"  {sym:12s} -> {code:8s} {sector}")
    print(f"Not mapped (left out): {len(rep['unmapped'])}")
    for sym, why in rep["unmapped"]:
        print(f"  {sym:12s} {why}")
    print(f"Added to watchlist:   {', '.join(rep['added']) or 'none'}")
    print(f"Removed from watchlist: {', '.join(rep['removed']) or 'none'}")
    for sym, old, new in rep["code_changed"]:
        print(f"  WARNING: {sym} code changed {old} -> {new}")
    print(f"Watchlist size: {rep['watchlist_size']}")
    if args.dry_run:
        print("Dry run: config.yaml not changed.")
    else:
        print("config.yaml updated." if rep["written"] else "config.yaml already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
