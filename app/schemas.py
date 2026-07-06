from datetime import datetime
from typing import Any
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
    total_stocks: int
    scanned_count: int
    failed_count: int
    min_market_cap: float
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


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
