"""端到端测试：FastAPI + SQLite，覆盖回拨双时刻、停播段、事务回滚与交错查档。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("RADIO_DB_PATH", db_path)
    # 延迟导入，确保环境变量生效
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


# 2024-11-03 America/New_York 回拨：
#   本地 01:30 -> UTC 05:30Z(1730611800) / 06:30Z(1730615400)
#   本地 01:45 -> UTC 05:45Z(1730612700) / 06:45Z(1730616300)
FALLBACK_LOG = {
    "channel_id": "ny-fm",
    "timezone": "America/New_York",
    "programs": [
        {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 900},
        {"seq": 2, "local_start": "2024-11-03T01:45:00", "duration": 900},
    ],
}


def test_upload_and_fallback_candidates(client):
    r = client.post("/channels", json=FALLBACK_LOG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["solvable"] is True
    assert body["arrangement_count"] == 3  # 早早 / 早晚 / 晚晚

    detail = client.get("/channels/ny-fm").json()
    assert detail["version"] == 1
    assert detail["programs"][0]["utc_options"] == [1730611800, 1730615400]
    assert detail["programs"][1]["utc_options"] == [1730612700, 1730616300]


def test_query_on_air_and_gap(client):
    client.post("/channels", json=FALLBACK_LOG)
    # 05:30Z：排法1/2 播 P1，排法3 空档 -> on_air=[1]，可能空档
    r = client.get("/channels/ny-fm/query", params={"utc_second": 1730611800})
    assert r.status_code == 200
    assert r.json()["on_air"] == [1]
    assert r.json()["may_be_gap"] is True

    # 07:00Z：所有节目都已结束（终点不含）-> 必为空档
    r = client.get("/channels/ny-fm/query", params={"utc_second": 1730617200})
    assert r.json()["on_air"] == []
    assert r.json()["may_be_gap"] is True


def test_blackout_narrows_arrangements_and_removes_gap(client):
    client.post("/channels", json=FALLBACK_LOG)
    # 停播 05:30-06:00Z：封掉早早、早晚两种排法，只剩晚晚
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={
            "blackout_id": "bo-1",
            "start_utc": 1730611800,
            "end_utc": 1730613600,
            "note": "发射机检修",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["arrangement_count"] == 1
    assert r.json()["version"] == 2

    # 停播段内的秒：无节目、空档、in_blackout
    r = client.get("/channels/ny-fm/query", params={"utc_second": 1730611800}).json()
    assert r == {
        "channel_id": "ny-fm",
        "utc_second": 1730611800,
        "version": 2,
        "on_air": [],
        "may_be_gap": True,
        "in_blackout": True,
        "arrangement_count": 1,
    }
    # 唯一排法里 06:30Z 的 P1 必播 -> 不可能空档
    r = client.get("/channels/ny-fm/query", params={"utc_second": 1730615400}).json()
    assert r["on_air"] == [1] and r["may_be_gap"] is False


def test_unsolvable_blackout_is_rejected_and_old_archive_survives(client):
    client.post("/channels", json=FALLBACK_LOG)
    # 先放一个合法停播段
    client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-1", "start_utc": 1730611800, "end_utc": 1730613600},
    )
    before = client.get("/channels/ny-fm").json()
    assert before["version"] == 2 and len(before["blackouts"]) == 1

    # 再封 06:30-07:00Z -> 全部排法封死，必须 409 且旧档不动
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-2", "start_utc": 1730615400, "end_utc": 1730617200},
    )
    assert r.status_code == 409
    after = client.get("/channels/ny-fm").json()
    assert after["version"] == 2
    assert len(after["blackouts"]) == 1  # bo-2 未写入
    # 旧档继续可查
    r = client.get("/channels/ny-fm/query", params={"utc_second": 1730615400})
    assert r.status_code == 200 and r.json()["on_air"] == [1]


def test_delete_blackout_restores_arrangements(client):
    client.post("/channels", json=FALLBACK_LOG)
    client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-1", "start_utc": 1730611800, "end_utc": 1730613600},
    )
    assert client.get("/channels/ny-fm/solve").json()["arrangement_count"] == 1
    r = client.delete("/channels/ny-fm/blackouts/bo-1")
    assert r.status_code == 200 and r.json()["arrangement_count"] == 3
    assert client.get("/channels/ny-fm").json()["version"] == 3


def test_spring_forward_gap_is_rejected(client):
    # 2024-03-10 02:30 在纽约时区不存在（春令跳空）
    payload = {
        "channel_id": "ny-spring",
        "timezone": "America/New_York",
        "programs": [{"seq": 1, "local_start": "2024-03-10T02:30:00", "duration": 600}],
    }
    r = client.post("/channels", json=payload)
    assert r.status_code == 422
    assert "不存在" in r.json()["detail"]
    assert client.get("/channels/ny-spring").status_code == 404


def test_replace_log_keeps_blackout_but_rejects_conflict(client):
    client.post("/channels", json=FALLBACK_LOG)
    base = 1730611800  # 05:30Z
    # 两段 5 分钟停播：旧档（15 分钟节目）仍有 1 种排法，
    # 但 30 分钟长节目的两个回拨候选（05:30Z / 06:30Z 开播）都会撞段。
    client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-a", "start_utc": base + 900, "end_utc": base + 1200},
    )
    client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-b", "start_utc": base + 3600, "end_utc": base + 3900},
    )
    assert client.get("/channels/ny-fm/solve").json()["arrangement_count"] == 1

    conflicting = {
        "channel_id": "ny-fm",
        "timezone": "America/New_York",
        "programs": [
            {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 1800},
        ],
    }
    r = client.post("/channels", json=conflicting)
    assert r.status_code == 409
    detail = client.get("/channels/ny-fm").json()
    assert len(detail["programs"]) == 2  # 旧日志保留、继续可查
    assert client.get("/channels/ny-fm/query", params={"utc_second": base}).status_code == 200


def test_validation_errors(client):
    # 流水号乱序
    bad = {
        "channel_id": "c1",
        "timezone": "America/New_York",
        "programs": [
            {"seq": 2, "local_start": "2024-11-03T02:00:00", "duration": 60},
            {"seq": 1, "local_start": "2024-11-03T01:00:00", "duration": 60},
        ],
    }
    assert client.post("/channels", json=bad).status_code == 422
    # 非法时区
    bad["timezone"] = "Mars/Olympus"
    bad["programs"].sort(key=lambda p: p["seq"])
    assert client.post("/channels", json=bad).status_code == 422
    # 带偏移的本地时刻
    bad["timezone"] = "America/New_York"
    bad["programs"][0]["local_start"] = "2024-11-03T01:00:00+00:00"
    assert client.post("/channels", json=bad).status_code == 422
    # 停播段 end <= start
    client.post("/channels", json=FALLBACK_LOG)
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "x", "start_utc": 100, "end_utc": 100},
    )
    assert r.status_code == 422


def test_interleaved_queries_use_committed_snapshot(client):
    """查档与更正交错：每次查询只基于一份已提交资料，版本号单调可核对。"""
    client.post("/channels", json=FALLBACK_LOG)
    q1 = client.get("/channels/ny-fm/query", params={"utc_second": 1730611800}).json()
    assert q1["version"] == 1 and q1["arrangement_count"] == 3

    client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-1", "start_utc": 1730611800, "end_utc": 1730613600},
    )
    q2 = client.get("/channels/ny-fm/query", params={"utc_second": 1730611800}).json()
    assert q2["version"] == 2 and q2["in_blackout"] is True and q2["on_air"] == []

    # 失败的更正不产生新版本
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bo-2", "start_utc": 1730615400, "end_utc": 1730617200},
    )
    assert r.status_code == 409
    q3 = client.get("/channels/ny-fm/query", params={"utc_second": 1730611800}).json()
    assert q3["version"] == 2 and q3 == q2


def test_half_open_boundary(client):
    payload = {
        "channel_id": "c2",
        "timezone": "UTC",
        "programs": [{"seq": 1, "local_start": "1970-01-01T00:00:00", "duration": 100}],
    }
    client.post("/channels", json=payload)
    assert client.get("/channels/c2/query", params={"utc_second": 0}).json()["on_air"] == [1]
    assert client.get("/channels/c2/query", params={"utc_second": 99}).json()["on_air"] == [1]
    r = client.get("/channels/c2/query", params={"utc_second": 100}).json()
    assert r["on_air"] == [] and r["may_be_gap"] is True


def test_blackout_crud_on_missing_channel_404(client):
    r = client.put(
        "/channels/nope/blackouts",
        json={"blackout_id": "b", "start_utc": 0, "end_utc": 10},
    )
    assert r.status_code == 404
    assert client.get("/channels/nope/query", params={"utc_second": 0}).status_code == 404
