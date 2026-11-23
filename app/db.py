"""SQLite 数据访问层。

一致性策略
----------
* 数据库开启 WAL；每次写操作在单把进程内写锁保护下，于 **一个**
  ``IMMEDIATE`` 事务内完成「落库 + 可解性校验」，校验不过则整体
  ``ROLLBACK`` —— 旧档案原封不动、继续可查。
* 每次读操作在独立连接、单一快照事务内读取整档（频道、节目、停播段），
  因此查询只会看到「某一份已提交资料」，不会读到写一半的中间状态。
* 每个频道带单调递增的 ``version``，每次成功提交 +1，查询响应回显版本号，
  方便核对交错的更正与查档分别基于哪一份资料。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import (
    ArrangementModel,
    Blackout,
    Program,
    coerce_epoch_seconds,
    epoch_to_json_value,
    is_solvable,
    load_zone,
    local_naive_to_utc_candidates,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,
    timezone     TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs (
    channel_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    local_start  TEXT NOT NULL,           -- 原始本地墙钟时刻，ISO 格式、无时区（可带小数秒）
    duration     INTEGER NOT NULL,
    utc_options  TEXT NOT NULL,           -- JSON: 合法 UTC 开播候选（整数或十进制字符串）
    PRIMARY KEY (channel_id, seq),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);
CREATE TABLE IF NOT EXISTS blackouts (
    channel_id   TEXT NOT NULL,
    blackout_id  TEXT NOT NULL,
    start_utc    TEXT NOT NULL,           -- 十进制纪元秒文本（整秒或带小数秒）
    end_utc      TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (channel_id, blackout_id),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);
"""


def _epoch_text(epoch) -> str:
    """纪元秒 -> 十进制文本；SQLite 原样保存，读出后再精确还原。"""
    return format(epoch, "f")


def _migrate_blackout_precision(conn: sqlite3.Connection) -> None:
    """把旧库的 ``blackouts.start_utc/end_utc``（INTEGER 秒）迁到 TEXT。

    旧库里的整数秒与新写入的小数秒必须共用同一列；TEXT 亲和性可原样保存
    ``1730611800.25``，避免 REAL 亲和性把微秒舍入掉。迁移是幂等的：列类型
    已是 TEXT 时直接返回；整个重建在一个事务内完成。
    """
    info = conn.execute("PRAGMA table_info(blackouts)").fetchall()
    if not info:  # pragma: no cover - 建表后必然存在
        return
    types = {row["name"]: (row["type"] or "").upper() for row in info}
    if types.get("start_utc") == "TEXT" and types.get("end_utc") == "TEXT":
        return
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute("ALTER TABLE blackouts RENAME TO blackouts_legacy;")
        conn.execute(
            """
            CREATE TABLE blackouts (
                channel_id   TEXT NOT NULL,
                blackout_id  TEXT NOT NULL,
                start_utc    TEXT NOT NULL,
                end_utc      TEXT NOT NULL,
                note         TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (channel_id, blackout_id),
                FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO blackouts(channel_id, blackout_id, start_utc, end_utc, note) "
            "SELECT channel_id, blackout_id, CAST(start_utc AS TEXT), "
            "CAST(end_utc AS TEXT), note FROM blackouts_legacy;"
        )
        conn.execute("DROP TABLE blackouts_legacy;")
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL;")
            _migrate_blackout_precision(conn)
            conn.commit()

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    # ------------------------------------------------------------------ #
    # 模型构造
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_model(timezone: str, rows: list[dict], blackouts: list[dict]) -> ArrangementModel:
        """把入参行转换为可计算模型；本地时刻非法（春令跳空）时抛 ValueError。"""
        tz: ZoneInfo = load_zone(timezone)
        programs: list[Program] = []
        for row in sorted(rows, key=lambda r: r["seq"]):
            wall = datetime.fromisoformat(row["local_start"])
            if wall.tzinfo is not None:
                raise ValueError(
                    f"流水号 {row['seq']} 的 local_start 必须是不带时区的本地时刻"
                )
            starts = tuple(local_naive_to_utc_candidates(wall, tz))
            if not starts:
                raise ValueError(
                    f"流水号 {row['seq']} 的本地时刻 {row['local_start']} "
                    f"在时区 {timezone} 中不存在（春令时跳空），无法排档"
                )
            programs.append(Program(seq=row["seq"], duration=row["duration"], starts=starts))
        bl = tuple(
            sorted(
                (
                    Blackout(
                        start=coerce_epoch_seconds(b["start_utc"]),
                        end=coerce_epoch_seconds(b["end_utc"]),
                    )
                    for b in blackouts
                ),
                key=lambda b: b.start,
            )
        )
        return ArrangementModel(timezone=timezone, programs=tuple(programs), blackouts=bl)

    def _read_model(self, conn: sqlite3.Connection, channel_id: str) -> ArrangementModel | None:
        ch = conn.execute(
            "SELECT timezone FROM channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if ch is None:
            return None
        prog_rows = conn.execute(
            "SELECT seq, duration, utc_options FROM programs WHERE channel_id=? ORDER BY seq",
            (channel_id,),
        ).fetchall()
        programs = tuple(
            Program(
                seq=r["seq"],
                duration=r["duration"],
                # 旧库写的是整数纪元秒，新库可能混有十进制字符串；统一精确还原。
                starts=tuple(coerce_epoch_seconds(v) for v in json.loads(r["utc_options"])),
            )
            for r in prog_rows
        )
        bl_rows = conn.execute(
            "SELECT start_utc, end_utc FROM blackouts WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        blackouts = tuple(
            sorted(
                (
                    Blackout(
                        coerce_epoch_seconds(r["start_utc"]),
                        coerce_epoch_seconds(r["end_utc"]),
                    )
                    for r in bl_rows
                ),
                key=lambda b: b.start,
            )
        )
        return ArrangementModel(timezone=ch["timezone"], programs=programs, blackouts=blackouts)

    # ------------------------------------------------------------------ #
    # 读（单一快照事务）
    # ------------------------------------------------------------------ #
    def get_channel(self, channel_id: str) -> dict | None:
        with self._connect() as conn:
            conn.execute("BEGIN;")  # 快照事务：整次读取对应同一份已提交资料
            ch = conn.execute(
                "SELECT channel_id, timezone, version, created_at, updated_at "
                "FROM channels WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
            if ch is None:
                conn.execute("ROLLBACK;")
                return None
            programs = conn.execute(
                "SELECT seq, local_start, duration, utc_options "
                "FROM programs WHERE channel_id=? ORDER BY seq",
                (channel_id,),
            ).fetchall()
            blackouts = conn.execute(
                "SELECT blackout_id, start_utc, end_utc, note "
                "FROM blackouts WHERE channel_id=?",
                (channel_id,),
            ).fetchall()
            result = {
                "channel_id": ch["channel_id"],
                "timezone": ch["timezone"],
                "version": ch["version"],
                "created_at": ch["created_at"],
                "updated_at": ch["updated_at"],
                "programs": [
                    {
                        "seq": r["seq"],
                        "local_start": r["local_start"],
                        "duration": r["duration"],
                        "utc_options": [coerce_epoch_seconds(v) for v in json.loads(r["utc_options"])],
                    }
                    for r in programs
                ],
                # TEXT 列按十进制数值而非字典序排序（"1000" < "999" 是字典序陷阱）。
                "blackouts": sorted(
                    (
                        {
                            "blackout_id": r["blackout_id"],
                            "start_utc": coerce_epoch_seconds(r["start_utc"]),
                            "end_utc": coerce_epoch_seconds(r["end_utc"]),
                            "note": r["note"],
                        }
                        for r in blackouts
                    ),
                    key=lambda b: (b["start_utc"], b["end_utc"], b["blackout_id"]),
                ),
            }
            conn.execute("COMMIT;")
            return result

    def get_model(self, channel_id: str) -> ArrangementModel | None:
        model, _ = self.get_snapshot(channel_id)
        return model

    def get_snapshot(self, channel_id: str) -> tuple[ArrangementModel | None, int | None]:
        """单快照事务内同时读取模型与版本号，保证二者属于同一份已提交资料。"""
        with self._connect() as conn:
            conn.execute("BEGIN;")
            row = conn.execute(
                "SELECT version FROM channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK;")
                return None, None
            model = self._read_model(conn, channel_id)
            conn.execute("COMMIT;")
            return model, row["version"]

    def get_version(self, channel_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            return row["version"] if row else None

    # ------------------------------------------------------------------ #
    # 写（进程锁 + 单事务原子提交；失败回滚，旧档不受影响）
    # ------------------------------------------------------------------ #
    def replace_log(self, channel_id: str, timezone: str, rows: list[dict]) -> int:
        """整份替换频道日志（停播段独立维护，不受影响）。

        校验或写入失败时抛异常，事务回滚，旧档案保持可查。返回新版本号。
        """
        model = self.build_model(timezone, rows, blackouts=[])
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with self._write_lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                existing = conn.execute(
                    "SELECT 1 FROM channels WHERE channel_id=?", (channel_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE channels SET timezone=?, version=version+1, updated_at=? "
                        "WHERE channel_id=?",
                        (timezone, now, channel_id),
                    )
                    conn.execute("DELETE FROM programs WHERE channel_id=?", (channel_id,))
                else:
                    conn.execute(
                        "INSERT INTO channels(channel_id, timezone, version, created_at, updated_at) "
                        "VALUES(?, ?, 1, ?, ?)",
                        (channel_id, timezone, now, now),
                    )
                conn.executemany(
                    "INSERT INTO programs(channel_id, seq, local_start, duration, utc_options) "
                    "VALUES(?, ?, ?, ?, ?)",
                    [
                        (
                            channel_id,
                            p.seq,
                            p_local_start(rows, p.seq),
                            p.duration,
                            # 整数候选写 int，带小数秒的候选写精确十进制字符串。
                            json.dumps([epoch_to_json_value(s) for s in p.starts]),
                        )
                        for p in model.programs
                    ],
                )
                # 新日志可能与既有停播段冲突：整档须仍有解，否则回滚保留旧档。
                model_after = self._read_model(conn, channel_id)
                if not is_solvable(model_after):
                    conn.execute("ROLLBACK;")
                    raise UnsolvableError(
                        f"新日志与频道 {channel_id} 已登记的停播段冲突，整档无解，已拒绝并保留旧档"
                    )
                conn.execute("COMMIT;")
            except UnsolvableError:
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        version = self.get_version(channel_id)
        assert version is not None
        return version

    def upsert_blackout(
        self,
        channel_id: str,
        blackout_id: str,
        start_utc: Decimal,
        end_utc: Decimal,
        note: str,
    ) -> int:
        """登记/更正一条 UTC 停播段；若新资料使整档无解则回滚，返回版本号。"""
        start_utc = coerce_epoch_seconds(start_utc)
        end_utc = coerce_epoch_seconds(end_utc)
        if end_utc <= start_utc:
            raise ValueError("停播段为半开区间 [start_utc, end_utc)，必须 end_utc > start_utc")
        with self._write_lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                ch = conn.execute(
                    "SELECT 1 FROM channels WHERE channel_id=?", (channel_id,)
                ).fetchone()
                if ch is None:
                    raise KeyError(channel_id)
                conn.execute(
                    "INSERT INTO blackouts(channel_id, blackout_id, start_utc, end_utc, note) "
                    "VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(channel_id, blackout_id) DO UPDATE SET "
                    "start_utc=excluded.start_utc, end_utc=excluded.end_utc, note=excluded.note",
                    (
                        channel_id,
                        blackout_id,
                        _epoch_text(start_utc),
                        _epoch_text(end_utc),
                        note,
                    ),
                )
                model = self._read_model(conn, channel_id)
                if not is_solvable(model):
                    conn.execute("ROLLBACK;")
                    raise UnsolvableError(
                        f"停播段 {blackout_id} 会使频道 {channel_id} 整档无解，已拒绝并保留旧档"
                    )
                conn.execute(
                    "UPDATE channels SET version=version+1, "
                    "updated_at=? WHERE channel_id=?",
                    (datetime.utcnow().isoformat(timespec="seconds") + "Z", channel_id),
                )
                conn.execute("COMMIT;")
            except UnsolvableError:
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        version = self.get_version(channel_id)
        assert version is not None
        return version

    def delete_blackout(self, channel_id: str, blackout_id: str) -> int:
        with self._write_lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.execute(
                    "DELETE FROM blackouts WHERE channel_id=? AND blackout_id=?",
                    (channel_id, blackout_id),
                )
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK;")
                    raise KeyError(blackout_id)
                conn.execute(
                    "UPDATE channels SET version=version+1, "
                    "updated_at=? WHERE channel_id=?",
                    (datetime.utcnow().isoformat(timespec="seconds") + "Z", channel_id),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        version = self.get_version(channel_id)
        assert version is not None
        return version


class UnsolvableError(Exception):
    """新资料导致整档不存在任何完整排法。"""


def p_local_start(rows: list[dict], seq: int) -> str:
    for r in rows:
        if r["seq"] == seq:
            return r["local_start"]
    raise KeyError(seq)  # pragma: no cover - 内部调用，理论不可达
