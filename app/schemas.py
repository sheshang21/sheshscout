from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class ScanCreateRequest(BaseModel):
    exchanges: list[str] = Field(default=["NSE", "BSE"], description="Subset of NSE/BSE; ignored if symbols is set")
    symbols: list[str] | None = Field(default=None, description="Explicit symbol list, overrides exchanges/range")
    range: dict[str, list[int]] | None = Field(
        default=None,
        description="Optional per-exchange 1-based row range, e.g. {'NSE': [1, 100]}. "
                    "Slices that exchange's universe (as loaded from nse.txt/bse.txt) "
                    "to rows From..To inclusive. Ignored if symbols is set.",
    )
    min_market_cap: float = Field(default=0, ge=0, description="Minimum market cap in crores")
    thresholds: dict[str, Any] | None = Field(default=None, description="Scoring thresholds; defaults used if omitted")


class ScanJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    scan_type: str = "positional"
    total_stocks: int
    scanned_count: int
    failed_count: int
    min_market_cap: float
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    # Computed, not a DB column (db/models.py) -- without this the SSE
    # stream is the only place a client can ever learn a 'running' job is
    # actually dead (server restarted mid-scan). The one-shot GET used by
    # ScanProgress.jsx's SSE onerror fallback, and History.jsx's list view,
    # both need it to detect the same orphaned-job case the /events stream
    # already reports every second.
    is_stale: bool


class ScanResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    symbol: str
    score: float | None
    rating: str | None
    qualified: bool
    sector: str | None
    created_at: datetime
    raw_result: dict[str, Any] | None = None
    # Not a DB column -- core.company_names has no NSE/BSE symbol column to
    # from_attributes off of, so this is always None straight out of
    # model_validate() and gets filled in by the router after validation
    # (see app/routers/scans.py and app/routers/intraday_scans.py).
    # None for anything not in the NSE name file, including every .BO
    # symbol for now -- see core/company_names.py's docstring for why.
    company_name: str | None = None


class IntradayScanCreateRequest(BaseModel):
    """Mirrors ScanCreateRequest's symbol-resolution shape (symbols overrides
    range overrides exchanges) so the frontend's range-scan/custom-list UI
    works unchanged for intraday. `params` overrides
    core.intraday_scanner.DEFAULT_PARAMS[direction]; unknown keys are
    ignored rather than rejected, same forward-compat reasoning as
    ScanCreateRequest.thresholds."""

    direction: Literal["long", "short"]
    exchanges: list[str] = Field(default=["NSE"], description="Subset of NSE/BSE; ignored if symbols is set")
    symbols: list[str] | None = Field(default=None, description="Explicit ticker list, overrides exchanges/range")
    range: dict[str, list[int]] | None = Field(
        default=None,
        description="Optional per-exchange 1-based row range, e.g. {'NSE': [1, 100]}. Ignored if symbols is set.",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Overrides for core.intraday_scanner.DEFAULT_PARAMS[direction] "
                    "(min_price, min_volume, rsi_threshold, stop_loss_pct, target_pct, ...)",
    )
    data_source: Literal["yfinance", "nse", "auto"] = Field(
        default="yfinance",
        description="Where to fetch .NS price/volume data from for this scan. "
                    "'yfinance' (default): unchanged prior behavior, goes through the "
                    "shared Yahoo rate limiter for both NSE and BSE symbols. "
                    "'nse': .NS symbols are fetched directly from NSE (no yfinance call "
                    "at all, immune to Yahoo-side throttling); .BO symbols still use "
                    "yfinance regardless, since NSE's free API has no BSE data. "
                    "'auto': tries NSE first for .NS symbols, falls back to yfinance for "
                    "any symbol NSE fails to return data for.",
    )
