"""
core/company_names.py — ticker -> company name lookup.

NSE: data/nse_names.csv has columns "NSE Ticker,Name" using the exact same
bare-ticker spelling nse.txt does (e.g. "20MICRONS"), so it's a direct
key match -- built once at import time into a dict, looked up by
stripping the .NS suffix scan results carry.

BSE: deliberately NOT wired up yet. data/bse_codes.csv (as supplied) is
keyed by the numeric BSE scrip code ("500002,ABB INDIA LIMITED"), but
bse.txt (core/universe.py) uses short alphabetic mnemonics ("ABB",
"AEGISLOG", "ARE&M"...) that don't appear anywhere in that CSV. The two
lists are also different lengths (4730 vs 4912) and checking the row
order confirms they're independently sorted, not parallel arrays -- so
there is no reliable positional or key join between them. Guessing via
fuzzy name-matching was deliberately ruled out: a wrong ticker<->name
pairing in a stock scanner is worse than a missing one. get_company_name()
returns None for any .BO symbol until a name file actually keyed by
bse.txt's mnemonics shows up in data/.
"""
import csv
import logging
import os

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_NSE_NAMES_PATH = os.path.join(_DATA_DIR, "nse_names.csv")

_SUFFIXES = (".NS", ".BO")


def _load_nse_names() -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        # utf-8-sig strips the BOM the source file starts with; the source
        # rows also carry trailing padding whitespace on the name column
        # ("20 MICRONS LTD           ") which .strip() cleans up.
        with open(_NSE_NAMES_PATH, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                ticker, name = row[0].strip(), row[1].strip()
                if ticker and ticker != "NSE Ticker":
                    names[ticker] = name
    except FileNotFoundError:
        logger.warning("company_names: %s not found -- NSE company names disabled", _NSE_NAMES_PATH)
    return names


_NSE_NAMES = _load_nse_names()
logger.info("company_names: loaded %d NSE ticker->name entries", len(_NSE_NAMES))


def get_company_name(symbol: str) -> str | None:
    """symbol is a scan-result symbol like 'RELIANCE.NS' or 'ABB.BO'.
    Returns the company name, or None if unknown (unlisted ticker) or
    unsupported (BSE -- see module docstring)."""
    if not symbol:
        return None
    if symbol.endswith(".BO"):
        return None
    bare = symbol
    for suf in _SUFFIXES:
        if bare.endswith(suf):
            bare = bare[: -len(suf)]
            break
    return _NSE_NAMES.get(bare)
