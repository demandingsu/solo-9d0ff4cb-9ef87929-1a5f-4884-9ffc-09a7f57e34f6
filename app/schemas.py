"""请求/响应模型与输入校验。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import load_zone


class ProgramIn(BaseModel):
    seq: int = Field(..., ge=1, description="流水号（正整数，全频道唯一、按升序排）")
    local_start: datetime = Field(
        ..., description="本地开播时刻（无时区墙钟时间，如 2024-11-03T01:30:00）"
    )
    duration: int = Field(..., gt=0, description="播出秒数（正整数）")

    @field_validator("local_start")
    @classmethod
    def _naive_wall_clock(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            raise ValueError("local_start 必须是不带时区偏移的本地墙钟时刻")
        return v


class LogUpload(BaseModel):
    channel_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    timezone: str = Field(..., description="IANA 时区名，如 America/New_York")
    programs: list[ProgramIn] = Field(..., min_length=1)

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        load_zone(v)  # 非法时区直接 422
        return v.strip()

    @model_validator(mode="after")
    def _check_seq(self) -> "LogUpload":
        seqs = [p.seq for p in self.programs]
        if len(set(seqs)) != len(seqs):
            raise ValueError("流水号必须唯一，不能重复")
        if seqs != sorted(seqs):
            raise ValueError("整份日志必须按流水号升序排列")
        return self


class BlackoutIn(BaseModel):
    blackout_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    start_utc: int = Field(..., description="停播段起点 UTC 纪元秒（含）")
    end_utc: int = Field(..., description="停播段终点 UTC 纪元秒（不含）")
    note: str = Field("", max_length=500)

    @model_validator(mode="after")
    def _half_open(self) -> "BlackoutIn":
        if self.end_utc <= self.start_utc:
            raise ValueError("停播段为半开区间 [start_utc, end_utc)，必须 end_utc > start_utc")
        return self


class ProgramOut(BaseModel):
    seq: int
    local_start: str
    duration: int
    utc_options: list[int]


class BlackoutOut(BaseModel):
    blackout_id: str
    start_utc: int
    end_utc: int
    note: str


class ChannelOut(BaseModel):
    channel_id: str
    timezone: str
    version: int
    created_at: str
    updated_at: str
    programs: list[ProgramOut]
    blackouts: list[BlackoutOut]


class CommitOut(BaseModel):
    channel_id: str
    version: int
    solvable: bool
    arrangement_count: int


class QueryOut(BaseModel):
    channel_id: str
    utc_second: int
    version: int
    on_air: list[int]
    may_be_gap: bool
    in_blackout: bool
    arrangement_count: int
