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


# --------------------------------------------------------------------------- #
# 时间精度链路：小数秒上传 -> UTC 候选 -> SQLite -> 排法 -> 查档响应
# --------------------------------------------------------------------------- #
FRACTIONAL_FALLBACK_LOG = {
    "channel_id": "ny-frac",
    "timezone": "America/New_York",
    "programs": [
        {"seq": 1, "local_start": "2024-11-03T01:30:00.250000", "duration": 900},
        {"seq": 2, "local_start": "2024-11-03T01:45:00.750000", "duration": 900},
    ],
}


def test_fractional_seconds_are_not_mistaken_for_spring_gap(client):
    """本次修复的核心回归：带小数秒的合法时刻此前被 422 误报为春令跳空。"""
    r = client.post("/channels", json=FRACTIONAL_FALLBACK_LOG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["solvable"] is True
    assert body["arrangement_count"] == 3  # 早早 / 早晚 / 晚晚

    detail = client.get("/channels/ny-frac").json()
    assert detail["programs"][0]["local_start"] == "2024-11-03T01:30:00.250000"
    # 回拨仍返回两个带小数秒的 UTC 候选。
    assert detail["programs"][0]["utc_options"] == [1730611800.25, 1730615400.25]
    assert detail["programs"][1]["utc_options"] == [1730612700.75, 1730616300.75]


def test_fractional_seconds_survive_sqlite_and_query(client):
    client.post("/channels", json=FRACTIONAL_FALLBACK_LOG)

    # 精确到微秒：05:30:00.249999Z 尚未开播，05:30:00.250000Z 已开播。
    r = client.get("/channels/ny-frac/query", params={"utc_second": "1730611800.249999"})
    assert r.status_code == 200 and r.json()["on_air"] == []
    r = client.get("/channels/ny-frac/query", params={"utc_second": "1730611800.250000"})
    assert r.json()["utc_second"] == 1730611800.25
    assert r.json()["on_air"] == [1] and r.json()["may_be_gap"] is True

    # 回拨的第二个候选同样带小数秒。
    r = client.get("/channels/ny-frac/query", params={"utc_second": 1730615400.25})
    assert r.json()["on_air"] == [1]

    # 按整秒查询（旧客户端用法）仍能命中覆盖该秒的节目。
    r = client.get("/channels/ny-frac/query", params={"utc_second": 1730611800})
    assert r.status_code == 200 and r.json()["on_air"] == [1]
    assert r.json()["utc_second"] == 1730611800

    # 半开区间：P1 时段 [1800.25, 2700.25)，终点那一微秒起换节目 2。
    r = client.get("/channels/ny-frac/query", params={"utc_second": "1730612700.249999"})
    assert r.json()["on_air"] == [1]
    r = client.get("/channels/ny-frac/query", params={"utc_second": "1730612700.25"})
    assert r.json()["on_air"] == []


def test_fractional_seconds_normal_time_is_accepted(client):
    payload = {
        "channel_id": "plain-frac",
        "timezone": "America/New_York",
        "programs": [{"seq": 1, "local_start": "2024-11-03T10:00:00.123456", "duration": 60}],
    }
    r = client.post("/channels", json=payload)
    assert r.status_code == 201, r.text
    assert client.get("/channels/plain-frac").json()["programs"][0]["utc_options"] == [
        1730646000.123456
    ]


def test_spring_gap_still_rejected_with_fractional_seconds(client):
    payload = {
        "channel_id": "ny-spring-frac",
        "timezone": "America/New_York",
        "programs": [{"seq": 1, "local_start": "2024-03-10T02:30:00.500000", "duration": 600}],
    }
    r = client.post("/channels", json=payload)
    assert r.status_code == 422
    assert "不存在" in r.json()["detail"]
    assert client.get("/channels/ny-spring-frac").status_code == 404


def test_fractional_blackout_and_unsolvable_upload_does_not_mutate(client):
    client.post("/channels", json=FRACTIONAL_FALLBACK_LOG)
    before = client.get("/channels/ny-frac").json()
    assert before["version"] == 1

    # 一条与全部旧候选都不相交的带小数秒停播段（留给后面的无解上传用）。
    r = client.put(
        "/channels/ny-frac/blackouts",
        json={
            "blackout_id": "bo-guard",
            "start_utc": 1730619000.5,
            "end_utc": 1730622600.5,
            "note": "检修",
        },
    )
    assert r.status_code == 200, r.text
    detail = client.get("/channels/ny-frac").json()
    assert detail["version"] == 2
    assert detail["blackouts"][0]["start_utc"] == 1730619000.5
    assert detail["blackouts"][0]["end_utc"] == 1730622600.5
    assert detail["programs"] == before["programs"]

    # 无解上传：02:30:00.500000 本地时刻（回拨后唯一）恰在停播段起点上，
    # 整档无解 -> 409，已提交档案（节目、停播段、版本号）一律不变。
    conflicting = {
        "channel_id": "ny-frac",
        "timezone": "America/New_York",
        "programs": [
            {"seq": 1, "local_start": "2024-11-03T02:30:00.500000", "duration": 1800},
        ],
    }
    r = client.post("/channels", json=conflicting)
    assert r.status_code == 409
    after = client.get("/channels/ny-frac").json()
    assert after["version"] == 2
    assert after["programs"] == detail["programs"]  # 已提交档案原封不动
    assert after["blackouts"] == detail["blackouts"]
    assert client.get("/channels/ny-frac/query", params={"utc_second": "1730615400.25"}).json()[
        "on_air"
    ] == [1]


def test_fractional_unsolvable_blackout_is_rejected(client):
    client.post("/channels", json=FRACTIONAL_FALLBACK_LOG)
    before = client.get("/channels/ny-frac").json()
    # 封住两个回拨候选（05:30:00.25Z 与 06:30:00.25Z 各 900 秒）-> 无解 -> 409。
    r = client.put(
        "/channels/ny-frac/blackouts",
        json={
            "blackout_id": "bo-all",
            "start_utc": 1730611800.25,
            "end_utc": 1730616300.25,
        },
    )
    assert r.status_code == 409
    after = client.get("/channels/ny-frac").json()
    assert after["version"] == 1
    assert after["blackouts"] == []
    assert after["programs"] == before["programs"]
    assert client.get("/channels/ny-frac/query", params={"utc_second": "1730611800.25"}).json()[
        "on_air"
    ] == [1]


def test_invalid_fractional_precision_rejected(client):
    client.post("/channels", json=FALLBACK_LOG)
    # 超过微秒精度的查询值被拒绝，而不是静默截断。
    r = client.get("/channels/ny-fm/query", params={"utc_second": "1730611800.1234567"})
    assert r.status_code == 422
    # 停播段同样拒绝超精度输入。
    r = client.put(
        "/channels/ny-fm/blackouts",
        json={"blackout_id": "bad", "start_utc": "1730611800.1234567", "end_utc": 1730613600},
    )
    assert r.status_code == 422
    # 非法字符串查询值。
    assert client.get("/channels/ny-fm/query", params={"utc_second": "abc"}).status_code == 422


def test_legacy_integer_rows_mix_with_new_fractional_rows(client):
    """旧库整秒日志与新写入的小数秒记录混用：先后、半开区间、停播冲突判断一致。"""
    import sqlite3

    from app.db import Repository
    from app.domain import query_second

    db_path = client.app.state.repo.db_path
    # 直接按旧 schema 造一份「整秒」档案，模拟升级前的库。
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS blackouts;
        DROP TABLE IF EXISTS programs;
        DROP TABLE IF EXISTS channels;
        CREATE TABLE channels (channel_id TEXT PRIMARY KEY, timezone TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE programs (channel_id TEXT NOT NULL, seq INTEGER NOT NULL,
            local_start TEXT NOT NULL, duration INTEGER NOT NULL, utc_options TEXT NOT NULL,
            PRIMARY KEY (channel_id, seq),
            FOREIGN KEY (channel_id) REFERENCES channels(channel_id));
        CREATE TABLE blackouts (channel_id TEXT NOT NULL, blackout_id TEXT NOT NULL,
            start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (channel_id, blackout_id),
            FOREIGN KEY (channel_id) REFERENCES channels(channel_id));
        INSERT INTO channels VALUES
            ('old', 'America/New_York', 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        INSERT INTO programs VALUES
            ('old', 1, '2024-11-03T01:30:00', 900, '[1730611800, 1730615400]'),
            ('old', 2, '2024-11-03T01:45:00', 900, '[1730612700, 1730616300]');
        INSERT INTO blackouts VALUES ('old', 'bo-old', 1730610000, 1730610900, '');
        """
    )
    conn.commit()
    conn.close()

    # 重新打开仓库触发幂等迁移，旧整秒行原样保留。
    client.app.state.repo = Repository(db_path)
    detail = client.get("/channels/old").json()
    assert detail["programs"][0]["utc_options"] == [1730611800, 1730615400]
    assert detail["blackouts"][0]["start_utc"] == 1730610000

    # 新写入一条带小数秒的停播段，与旧整秒段共存。
    r = client.put(
        "/channels/old/blackouts",
        json={"blackout_id": "bo-new", "start_utc": 1730611800.25, "end_utc": 1730612700.75},
    )
    assert r.status_code == 200, r.text
    blackouts = client.get("/channels/old").json()["blackouts"]
    assert [(b["blackout_id"], b["start_utc"]) for b in blackouts] == [
        ("bo-old", 1730610000),
        ("bo-new", 1730611800.25),
    ]

    # 混用后：节目先后仍是半开区间；按整秒查询命中覆盖该秒的节目。
    # 该停播段封掉两个「早候选」（P1@1800.25、P2@2700.75 前），只剩晚晚排法：
    # P1 [1730615400, 1730616300)、P2 [1730616300, 1730617200)。
    r = client.get("/channels/old/query", params={"utc_second": 1730611800})
    assert r.status_code == 200 and r.json()["on_air"] == []
    assert r.json()["in_blackout"] is True
    # 旧整秒查询命中晚候选的节目；新带小数秒查询在边界两侧切换节目。
    r = client.get("/channels/old/query", params={"utc_second": 1730615400})
    assert r.json()["on_air"] == [1]
    r = client.get("/channels/old/query", params={"utc_second": "1730616300.000001"})
    assert r.json()["on_air"] == [2]
    r = client.get("/channels/old/query", params={"utc_second": "1730616299.999999"})
    assert r.json()["on_air"] == [1]

    # 停播冲突判断在混合精度下仍一致：新日志的整秒候选落进旧库与新库的
    # 停播段（整秒 + 小数秒）时，整档无解 -> 409 保留旧档。
    # 再加一条带小数秒、位于所有旧候选之外的停播段。
    r = client.put(
        "/channels/old/blackouts",
        json={
            "blackout_id": "bo-late-frac",
            "start_utc": 1730619900.25,
            "end_utc": 1730620800.75,
        },
    )
    assert r.status_code == 200, r.text
    model_before, version_before = client.app.state.repo.get_snapshot("old")
    assert version_before == 3
    assert len(model_before.blackouts) == 3

    # 03:00 EST -> 08:00Z = 1730620800（整秒候选），被带小数秒的停播段盖住。
    r = client.post(
        "/channels",
        json={
            "channel_id": "old",
            "timezone": "America/New_York",
            "programs": [
                {"seq": 1, "local_start": "2024-11-03T03:00:00", "duration": 900},
            ],
        },
    )
    assert r.status_code == 409
    model_after, version_after = client.app.state.repo.get_snapshot("old")
    assert version_after == 3
    assert model_after.programs == model_before.programs
    assert model_after.blackouts == model_before.blackouts

    # 旧整秒查询接口（int 参数）仍然可用；旧的整秒停播段仍生效。
    assert query_second(model_after, 1730615400)["on_air"] == [1]
    assert query_second(model_after, 1730610300)["in_blackout"] is True

