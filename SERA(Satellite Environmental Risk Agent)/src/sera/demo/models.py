from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Window(BaseModel):
    start: date
    end: date  # exclusive, matching Earth Engine filterDate

    @model_validator(mode='after')
    def ordered(self):
        if not 1 <= (self.end - self.start).days <= 62:
            raise ValueError('Each window must be 1–62 days, with an exclusive end')
        return self


class Region(BaseModel):
    model_config = ConfigDict(extra='forbid')
    region_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{2,62}$')
    name: str = Field(min_length=1, max_length=160)
    asset_label: str = Field(min_length=1, max_length=160)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-70, le=70)
    radius_km: float = Field(default=3, ge=0.5, le=10)
    asset_radius_m: int = Field(default=500, ge=100, le=2000)
    before: Window
    after: Window
    as_of: date
    indices: list[Literal['NDVI', 'NDWI', 'MNDWI', 'NBR']] = Field(min_length=1, max_length=4)
    baseline_years: int = Field(default=3, ge=0, le=5)
    event_context: str = Field(default='', max_length=2000)
    sources: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def chronology(self):
        if self.before.end > self.after.start or self.after.end > self.as_of:
            raise ValueError('Require before.end <= after.start < after.end <= as_of')
        if self.as_of > date.today():
            raise ValueError('Replay cutoff cannot be in the future')
        if self.before.start < date(2019, 1, 1):
            raise ValueError('This demo supports harmonized Sentinel-2 from 2019 onward')
        if len(set(self.indices)) != len(self.indices):
            raise ValueError('Duplicate indices')
        if self.asset_radius_m > self.radius_km * 1000:
            raise ValueError('Asset buffer must fit inside monitoring radius')
        return self


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    region_id: str
    force_recompute: bool = False


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class Report(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scan_id: str
    asset_id: str
    observations: list[Claim] = Field(min_length=1, max_length=8)
    interpretation: str = Field(min_length=1, max_length=2500)
    limitations: list[str] = Field(min_length=1, max_length=15)
    review_action: str = Field(min_length=1, max_length=1500)
    human_review_required: bool

    @model_validator(mode='after')
    def require_human_review(self):
        if self.human_review_required is not True:
            raise ValueError('Human review is mandatory')
        return self
