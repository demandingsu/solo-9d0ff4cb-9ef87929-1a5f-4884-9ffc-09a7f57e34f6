"""FastAPI 入口：电台档案 UTC 查档服务。

端点
----
* ``POST /channels``                接收/整份替换单频道日志（含 IANA 时区）
* ``GET  /channels/{id}``           读取一份已提交的完整档案（含版本号）
* ``GET  /channels/{id}/solve``     整档可解性与完整排法数
* ``PUT  /channels/{id}/blackouts`` 登记/更正一条 UTC 停播段
* ``DELETE /channels/{id}/blackouts/{bid}`` 删除停播段
* ``GET  /channels/{id}/query?utc_second=`` 查档：某 UTC 秒的播出节目与空档判定

每次查询在单快照事务内读取同一份已提交资料；写入在单事务内校验并提交，
整档无解或写入失败一律回滚，旧档继续可查。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .db import Repository, UnsolvableError
from .domain import count_arrangements, is_solvable, query_second
from .schemas import (
    BlackoutIn,
    ChannelOut,
    CommitOut,
    LogUpload,
    QueryOut,
)

DB_PATH = os.environ.get("RADIO_DB_PATH", "/data/radio.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.repo = Repository(DB_PATH)
    yield


app = FastAPI(
    title="电台档案 UTC 查档服务",
    version="1.0.0",
    description=(
        "按 UTC 秒查询单频道电台节目。日志只记本地开播时刻，服务依据 IANA 时区"
        "换算 UTC；夏令时回拨导致本地时刻出现两次时，两种 UTC 时刻均纳入核对。"
        "时段均为半开区间 [start, end)。"
    ),
    lifespan=lifespan,
)


def _repo() -> Repository:
    return app.state.repo


def _require_channel(channel_id: str):
    model = _repo().get_model(channel_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return model


def _require_snapshot(channel_id: str):
    """返回同一份已提交资料的模型与版本号（单快照事务）。"""
    model, version = _repo().get_snapshot(channel_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return model, version


# --------------------------------------------------------------------------- #
# 日志写入
# --------------------------------------------------------------------------- #
@app.post("/channels", response_model=CommitOut, status_code=201, tags=["日志"])
def upload_log(payload: LogUpload) -> CommitOut:
    """接收（或整份替换）单频道日志。

    新日志与既有停播段冲突导致整档无解时返回 409，旧档案保留不动。
    """
    rows = [
        {
            "seq": p.seq,
            "local_start": p.local_start.isoformat(),
            "duration": p.duration,
        }
        for p in payload.programs
    ]
    try:
        version = _repo().replace_log(payload.channel_id, payload.timezone, rows)
    except UnsolvableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # 春令跳空等
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    model = _require_channel(payload.channel_id)
    return CommitOut(
        channel_id=payload.channel_id,
        version=version,
        solvable=is_solvable(model),
        arrangement_count=count_arrangements(model),
    )


# --------------------------------------------------------------------------- #
# 停播段
# --------------------------------------------------------------------------- #
@app.put(
    "/channels/{channel_id}/blackouts",
    response_model=CommitOut,
    tags=["停播段"],
)
def upsert_blackout(channel_id: str, payload: BlackoutIn) -> CommitOut:
    """登记或更正一条 UTC 停播段（按 blackout_id 覆盖）。

    若该停播段会使整档不存在任何完整排法，返回 409 并保留旧档。
    """
    try:
        version = _repo().upsert_blackout(
            channel_id,
            payload.blackout_id,
            payload.start_utc,
            payload.end_utc,
            payload.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在") from exc
    except UnsolvableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    model = _require_channel(channel_id)
    return CommitOut(
        channel_id=channel_id,
        version=version,
        solvable=True,
        arrangement_count=count_arrangements(model),
    )


@app.delete(
    "/channels/{channel_id}/blackouts/{blackout_id}",
    response_model=CommitOut,
    tags=["停播段"],
)
def delete_blackout(channel_id: str, blackout_id: str) -> CommitOut:
    try:
        version = _repo().delete_blackout(channel_id, blackout_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"频道或停播段不存在: {channel_id}/{blackout_id}"
        ) from exc
    model = _require_channel(channel_id)
    return CommitOut(
        channel_id=channel_id,
        version=version,
        solvable=is_solvable(model),
        arrangement_count=count_arrangements(model),
    )


# --------------------------------------------------------------------------- #
# 查档
# --------------------------------------------------------------------------- #
@app.get("/channels/{channel_id}", response_model=ChannelOut, tags=["查档"])
def get_channel(channel_id: str) -> ChannelOut:
    data = _repo().get_channel(channel_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"频道 {channel_id} 不存在")
    return ChannelOut(**data)


@app.get("/channels/{channel_id}/solve", tags=["查档"])
def solve(channel_id: str) -> dict:
    model, version = _require_snapshot(channel_id)
    return {
        "channel_id": channel_id,
        "version": version,
        "solvable": is_solvable(model),
        "arrangement_count": count_arrangements(model),
    }


@app.get("/channels/{channel_id}/query", response_model=QueryOut, tags=["查档"])
def query(
    channel_id: str,
    utc_second: int = Query(..., description="要查的 UTC 纪元秒"),
) -> QueryOut:
    """列出在至少一种完整排法中播到该秒的节目流水号，并判定该秒是否可能是空档。"""
    model, version = _require_snapshot(channel_id)
    if not is_solvable(model):
        # 正常情况下无解资料无法提交；这里防御性处理，保证语义完整。
        raise HTTPException(status_code=409, detail="该档案当前不存在完整排法，无法查档")
    result = query_second(model, utc_second)
    return QueryOut(
        channel_id=channel_id,
        utc_second=utc_second,
        version=version,
        on_air=result["on_air"],
        may_be_gap=result["may_be_gap"],
        in_blackout=result["in_blackout"],
        arrangement_count=result["arrangement_count"],
    )


@app.get("/", tags=["meta"])
def root() -> dict:
    return {"service": "radio-archive", "docs": "/docs"}
