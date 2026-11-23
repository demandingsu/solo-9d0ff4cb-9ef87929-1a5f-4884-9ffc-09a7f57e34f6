"""请求/响应模型与输入校验。

时间精度
--------
UTC 纪元秒统一用 :data:`EpochSeconds`：内部是精确的 :class:`~decimal.Decimal`
（最多 6 位小数，即微秒）。输入接受 JSON 数字或十进制字符串；输出时整数秒写
JSON 整数，能由二进制浮点精确表示的小数秒写 JSON 数字，否则写十进制字符串，
因此旧接口的整秒客户端不受影响，微秒精度也不会丢。
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from .domain import coerce_epoch_seconds, epoch_to_api_number, load_zone

# 秒的小数部分：允许逗号或点（ISO 8601 两种写法），最多 6 位（微秒）。
_FRACTION_RE = re.compile(r"\d{2}:\d{2}:\d{2}[.,](\d+)")


def _reject_sub_microsecond(value: str) -> None:
    """Python 的 datetime 会把超过 6 位的小数秒静默截断，这里显式拒绝。"""
    match = _FRACTION_RE.search(value.strip())
    if match and len(match.group(1)) > 6:
        raise ValueError("local_start 最多支持 6 位小数秒（微秒精度）")


EpochSeconds = Annotated[
    Decimal,
    BeforeValidator(coerce_epoch_seconds),
    PlainSerializer(
        epoch_to_api_number, return_type=Union[int, float, str], when_used="json"
    ),
    WithJsonSchema(
        {
            "anyOf": [{"type": "number"}, {"type": "string"}],
            "title": "UTC 纪元秒",
            "description": (
                "UTC 纪元秒，可带至多 6 位小数（微秒）。整数秒按 JSON 整数返回；"
                "小数秒在二进制浮点可精确表示时按 JSON 数字返回，否则按十进制字符串返回。"
            ),
        }
    ),
]


class ProgramIn(BaseModel):
    seq: int = Field(..., ge=1, description="流水号（正整数，全频道唯一、按升序排）")
    local_start: datetime = Field(
        ...,
        description="本地开播时刻（无时区墙钟时间，可带小数秒，如 2024-11-03T01:30:00.250000）",
    )
    duration: int = Field(..., gt=0, description="播出秒数（正整数）")

    @field_validator("local_start", mode="before")
    @classmethod
    def _check_fraction_precision(cls, v: object) -> object:
        if isinstance(v, str):
            _reject_sub_microsecond(v)
        return v

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

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "channel_id": "demo-whole",
                    "timezone": "America/New_York",
                    "programs": [
                        {"seq": 1, "local_start": "2024-11-03T10:00:00", "duration": 900},
                        {"seq": 2, "local_start": "2024-11-03T10:15:00", "duration": 900},
                    ],
                },
                {
                    "channel_id": "demo-frac",
                    "timezone": "America/New_York",
                    "programs": [
                        {
                            "seq": 1,
                            "local_start": "2024-11-03T10:00:00.250000",
                            "duration": 900,
                        },
                        {
                            "seq": 2,
                            "local_start": "2024-11-03T10:15:00.750000",
                            "duration": 900,
                        },
                    ],
                },
                {
                    "channel_id": "demo-fold",
                    "timezone": "America/New_York",
                    "programs": [
                        {
                            "seq": 1,
                            "local_start": "2024-11-03T01:30:00.250000",
                            "duration": 900,
                        },
                        {
                            "seq": 2,
                            "local_start": "2024-11-03T01:45:00.750000",
                            "duration": 900,
                        },
                    ],
                },
            ]
        }
    )

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
    start_utc: EpochSeconds = Field(..., description="停播段起点 UTC 纪元秒（含）")
    end_utc: EpochSeconds = Field(..., description="停播段终点 UTC 纪元秒（不含）")
    note: str = Field("", max_length=500)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "blackout_id": "bo-1",
                    "start_utc": 1730611800,
                    "end_utc": 1730613600,
                    "note": "整秒停播段",
                },
                {
                    "blackout_id": "bo-frac",
                    "start_utc": 1730611800.25,
                    "end_utc": 1730612700.75,
                    "note": "带小数秒的停播段",
                },
            ]
        }
    )

    @model_validator(mode="after")
    def _half_open(self) -> "BlackoutIn":
        if self.end_utc <= self.start_utc:
            raise ValueError("停播段为半开区间 [start_utc, end_utc)，必须 end_utc > start_utc")
        return self


class ProgramOut(BaseModel):
    seq: int
    local_start: str
    duration: int
    utc_options: list[EpochSeconds]


class BlackoutOut(BaseModel):
    blackout_id: str
    start_utc: EpochSeconds
    end_utc: EpochSeconds
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
    utc_second: EpochSeconds
    version: int
    on_air: list[int]
    may_be_gap: bool
    in_blackout: bool
    arrangement_count: int
