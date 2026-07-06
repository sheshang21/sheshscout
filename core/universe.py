"""
core/universe.py — load the scannable stock universe from the same
nse.txt / bse.txt files sheshscout.py has always used.

Mirrors the original app's loading logic exactly (strip whitespace,
skip blanks, suffix .NS/.BO, dedupe while preserving order) so switching
which file backs "NSE" or "BSE" doesn't silently change behavior between
the Streamlit app and this one.
"""
import os

_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_DATA_DIR)  # nse.txt/bse.txt live at repo root, not inside core/

_EXCHANGE_FILES = {
    "NSE": ("nse.txt", ".NS"),
    "BSE": ("bse.txt", ".BO"),
}


def _load_file(filename: str, suffix: str) -> list[str]:
    path = os.path.join(_PROJECT_ROOT, filename)
    symbols = []
    try:
        with open(path, "r") as f:
            for line in f:
                stock = line.strip()
                if stock:
                    symbols.append(f"{stock}{suffix}")
    except FileNotFoundError:
        pass
    return symbols


def load_universe(exchanges: list[str] | None = None) -> list[str]:
    """Return the deduped, suffixed symbol list for the given exchanges.

    exchanges: subset of ["NSE", "BSE"]; defaults to both.
    """
    exchanges = exchanges or list(_EXCHANGE_FILES.keys())
    symbols: list[str] = []
    for exch in exchanges:
        if exch not in _EXCHANGE_FILES:
            continue
        filename, suffix = _EXCHANGE_FILES[exch]
        symbols.extend(_load_file(filename, suffix))
    return list(dict.fromkeys(symbols))  # dedupe, preserve order
