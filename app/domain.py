"""核心领域逻辑：IANA 时区解析、本地开播时刻 -> UTC 候选、整档可解性与查询。

约定
----
* 所有时刻在内部都以「UTC 纪元秒（整数）」表示。
* 节目 i 的时段为半开区间 ``[start, start + duration)``，含起点、不含终点。
* 日志按流水号升序；一份「完整排法」即为每个节目从其 UTC 候选开播时刻中
  选取一个，使节目之间（按流水号顺序）首尾不相叠、且不与任何停播段相交。
* 夏令时回拨（fold=0 / fold=1）时，同一个本地墙钟时刻对应两个不同的 UTC
  时刻，两者都必须纳入核对；春令时跳空产生的不存在时刻没有任何 UTC 对应，
  该档案无解。

可解性与查询均为 O(n·k) 的动态规划（k 为每个节目的候选数，最多为 2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# --------------------------------------------------------------------------- #
# 时区与本地时刻
# --------------------------------------------------------------------------- #
def load_zone(iana_name: str) -> ZoneInfo:
    """按 IANA 名称加载时区；非法名称抛出 ValueError。"""
    if not isinstance(iana_name, str) or not iana_name.strip():
        raise ValueError("时区名称不能为空")
    try:
        return ZoneInfo(iana_name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - 防御
        raise ValueError(f"无法识别的 IANA 时区: {iana_name!r}") from exc


def local_naive_to_utc_candidates(wall: datetime, tz: ZoneInfo) -> list[int]:
    """把本地「无时区」墙钟时刻换算为全部合法的 UTC 纪元秒候选。

    返回列表按时间升序、去重：

    * 普通时刻：1 个候选；
    * 回拨重叠时刻（夏令时结束、本地时刻出现两次）：2 个候选，
      分别对应 fold=0（第一次/夏令时）与 fold=1（第二次/标准时）；
    * 春令时跳空的不存在时刻：0 个候选（调用方据此判定档案无解）。
    """
    if wall.tzinfo is not None:
        # 输入模型已禁止带偏移的时刻，这里再兜底一次。
        wall = wall.replace(tzinfo=None)

    candidates: list[int] = []
    for fold in (0, 1):
        aware = wall.replace(tzinfo=tz, fold=fold)
        utc_second = int(aware.timestamp())
        # 回查：把换算结果再投影回本地墙钟。跳空时刻会被规整到相邻的
        # 合法时刻，回查必然不等于原输入，据此识别「不存在的本地时刻」。
        back = datetime.fromtimestamp(utc_second, tz).replace(tzinfo=None)
        if back == wall and utc_second not in candidates:
            candidates.append(utc_second)
    return sorted(candidates)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Program:
    seq: int
    duration: int
    starts: tuple[int, ...]  # 合法 UTC 开播候选（升序、去重），空元组=不存在的本地时刻


@dataclass(frozen=True)
class Blackout:
    start: int
    end: int


@dataclass(frozen=True)
class ArrangementModel:
    """一份已提交档案的可计算模型：节目按流水号升序，停播段按起点升序。"""

    timezone: str
    programs: tuple[Program, ...]
    blackouts: tuple[Blackout, ...]


# --------------------------------------------------------------------------- #
# 区间工具（半开：含起点、不含终点）
# --------------------------------------------------------------------------- #
def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _hits_blackout(start: int, end: int, blackouts: tuple[Blackout, ...]) -> bool:
    for b in blackouts:
        if _overlaps(start, end, b.start, b.end):
            return True
        if b.start >= end:
            break  # 已按起点排序，后面的停播段更不可能相交
    return False


# --------------------------------------------------------------------------- #
# 动态规划
# --------------------------------------------------------------------------- #
def _forward_reach(model: ArrangementModel) -> list[list[bool]]:
    """reach[i][j]：节目 i 取第 j 个候选开播时，前缀 0..i 是否存在合法排法。

    与上一节目「任一」可达候选相容即可；不能只看最早结束时刻，
    否则会漏掉通过更晚候选衔接的合法排法。
    """
    reach: list[list[bool]] = []
    for i, program in enumerate(model.programs):
        row: list[bool] = []
        for start in program.starts:
            end = start + program.duration
            if _hits_blackout(start, end, model.blackouts):
                row.append(False)
                continue
            if i == 0:
                row.append(True)
                continue
            prev = model.programs[i - 1]
            row.append(
                any(
                    reach[i - 1][k] and s + prev.duration <= start
                    for k, s in enumerate(prev.starts)
                )
            )
        reach.append(row)
    return reach


def _backward_reach(model: ArrangementModel) -> list[list[bool]]:
    """later[i][j]：节目 i 取第 j 个候选时，后缀 i..n-1 是否存在合法排法。

    必须与下一节目「任一」可达候选做相容性检查；不能只看最早开播时刻，
    否则会漏掉通过更晚候选衔接的合法排法。
    """
    n = len(model.programs)
    later: list[list[bool]] = [[False] * len(p.starts) for p in model.programs]
    for i in range(n - 1, -1, -1):
        program = model.programs[i]
        for j, start in enumerate(program.starts):
            end = start + program.duration
            if _hits_blackout(start, end, model.blackouts):
                continue
            if i == n - 1:
                later[i][j] = True
                continue
            later[i][j] = any(
                later[i + 1][k] and end <= next_start
                for k, next_start in enumerate(model.programs[i + 1].starts)
            )
    return later


def is_solvable(model: ArrangementModel) -> bool:
    """整档是否至少存在一种完整排法。"""
    if not model.programs:
        return True
    reach = _forward_reach(model)
    return any(reach[-1])


def count_arrangements(model: ArrangementModel) -> int:
    """统计完整排法数量（沿 DP 计数，仅用于查询结果中的可观测信息）。"""
    if not model.programs:
        return 1
    prev_counts: list[int] | None = None
    prev_ends: list[int] | None = None
    for program in model.programs:
        counts: list[int] = []
        ends: list[int] = []
        for start in program.starts:
            end = start + program.duration
            if _hits_blackout(start, end, model.blackouts):
                counts.append(0)
                ends.append(end)
                continue
            if prev_counts is None:
                counts.append(1)
            else:
                total = 0
                for pc, pe in zip(prev_counts, prev_ends):
                    if pc and pe <= start:
                        total += pc
                counts.append(total)
            ends.append(end)
        prev_counts, prev_ends = counts, ends
    return sum(prev_counts or [])


def query_second(model: ArrangementModel, t: int) -> dict:
    """查询 UTC 秒 ``t`` 的播出情况。

    返回字段：

    * ``on_air``：在至少一种完整排法中，时段覆盖 ``t`` 的节目流水号
      （半开区间 ``[start, end)``，即 ``start <= t < end``）；
    * ``may_be_gap``：是否存在一种完整排法，使 ``t`` 不被任何节目覆盖
      （停播段内的秒恒为空档）；
    * ``in_blackout``：``t`` 是否落在某个停播段内；
    * ``arrangement_count``：该档案完整排法总数。
    """
    n = len(model.programs)
    in_blackout = any(b.start <= t < b.end for b in model.blackouts)

    if n == 0:
        return {
            "on_air": [],
            "may_be_gap": True,
            "in_blackout": in_blackout,
            "arrangement_count": count_arrangements(model),
        }

    reach = _forward_reach(model)
    later = _backward_reach(model)

    on_air: list[int] = []
    for i, program in enumerate(model.programs):
        coverable = False
        for j, start in enumerate(program.starts):
            end = start + program.duration
            if start <= t < end and reach[i][j] and later[i][j]:
                coverable = True
                break
        if coverable:
            on_air.append(program.seq)

    # 空档：存在完整排法，使没有任何节目覆盖 t。
    # 正向 DP 额外携带「前缀排法是否已覆盖 t」的状态。
    # covered[k] / uncovered[k]：到达节目 i 的候选 k 时，
    # 前缀中已覆盖 / 尚未覆盖 t 的合法路径数（布尔意义即可，用计数更直观）。
    covered: list[int] = []
    uncovered: list[int] = []
    prev_ends: list[int] | None = None
    for i, program in enumerate(model.programs):
        new_covered: list[int] = []
        new_uncovered: list[int] = []
        new_ends: list[int] = []
        for j, start in enumerate(program.starts):
            end = start + program.duration
            new_ends.append(end)
            if not (reach[i][j] and later[i][j]):
                new_covered.append(0)
                new_uncovered.append(0)
                continue
            covers = start <= t < end
            if i == 0:
                new_covered.append(1 if covers else 0)
                new_uncovered.append(0 if covers else 1)
                continue
            c = u = 0
            for k, pc in enumerate(covered):
                if pc == 0 and uncovered[k] == 0:
                    continue
                if prev_ends[k] <= start:  # 与上一节目不叠（首尾相接允许）
                    # 此前已覆盖 t 的路径：覆盖态延续
                    c += pc
                    if covers:
                        # 此前未覆盖、本节目开始覆盖 -> 转入覆盖态
                        c += uncovered[k]
                    else:
                        # 本节目不覆盖 -> 保持未覆盖态
                        u += uncovered[k]
            new_covered.append(c)
            new_uncovered.append(u)
        covered, uncovered, prev_ends = new_covered, new_uncovered, new_ends

    # 停播段内的秒不可能被任何节目覆盖（节目与停播段相交即非法），
    # 因此档案有解时 uncovered 路径必然存在；may_be_gap 直接取 DP 结果。
    may_be_gap = sum(uncovered) > 0

    return {
        "on_air": on_air,
        "may_be_gap": may_be_gap,
        "in_blackout": in_blackout,
        "arrangement_count": count_arrangements(model),
    }
