"""核心领域逻辑：IANA 时区解析、本地开播时刻 -> UTC 候选、整档可解性与查询。

约定
----
* 所有时刻在内部都以「UTC 纪元秒」表示，类型为 :class:`~decimal.Decimal`。
  整数秒与小数秒（微秒精度）共用同一套精确算术，不做浮点四舍五入。
* 节目 i 的时段为半开区间 ``[start, start + duration)``，含起点、不含终点。
* 日志按流水号升序；一份「完整排法」即为每个节目从其 UTC 候选开播时刻中
  选取一个，使节目之间（按流水号顺序）首尾不相叠、且不与任何停播段相交。
* 夏令时回拨（fold=0 / fold=1）时，同一个本地墙钟时刻对应两个不同的 UTC
  时刻，两者都必须纳入核对；春令时跳空产生的不存在时刻没有任何 UTC 对应，
  该档案无解。

精度链路：本地墙钟（最多 6 位小数）→ 整数微秒换算 → 精确 Decimal 纪元秒 →
SQLite 文本 → 排法计算 → 查档响应。旧库中的整数纪元秒可直接参与同一套比较。

可解性与查询均为 O(n·k) 的动态规划（k 为每个节目的候选数，最多为 2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# --------------------------------------------------------------------------- #
# 纪元秒（Decimal，微秒精度）
# --------------------------------------------------------------------------- #
MICROS_PER_SECOND = Decimal(1_000_000)
# 纪元秒的合理范围：与 64 位有符号整数一致，远超任何电台档案；
# 同时避免极端指数输入在序列化/浮点回退时产生无界整数。
MAX_ABS_EPOCH_SECONDS = Decimal(2**63)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def coerce_epoch_seconds(value: object) -> Decimal:
    """把整数、浮点、字符串或 Decimal 统一成精确的 Decimal 纪元秒。

    最多允许 6 位小数（微秒）；更细的精度会被拒绝而不是静默截断。
    旧库里的整数纪元秒与新写入的小数纪元秒因此可以安全混用。
    """
    if isinstance(value, bool):
        raise ValueError("UTC 纪元秒不能是布尔值")
    if isinstance(value, Decimal):
        epoch = value
    elif isinstance(value, int):
        epoch = Decimal(value)
    elif isinstance(value, float):
        # str(float) 取最短往返表示，避免 Decimal(0.1) 的二进制误差。
        epoch = Decimal(str(value))
    elif isinstance(value, str):
        try:
            epoch = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"无法解析 UTC 纪元秒: {value!r}") from exc
    else:
        raise ValueError(f"无法解析 UTC 纪元秒: {value!r}")

    if not epoch.is_finite():
        raise ValueError("UTC 纪元秒必须是有限数值")
    if abs(epoch) > MAX_ABS_EPOCH_SECONDS:
        raise ValueError(f"UTC 纪元秒超出合理范围（绝对值需 ≤ {MAX_ABS_EPOCH_SECONDS}）")
    # 精确判断小数位：用定点乘 10^6，不受 Decimal 默认 28 位有效数字限制。
    with localcontext() as ctx:
        ctx.prec = max(28, len(epoch.as_tuple().digits) + 8)
        scaled = epoch.scaleb(6)
        if scaled != scaled.to_integral_value():
            raise ValueError("UTC 纪元秒最多支持 6 位小数（微秒精度）")
    return epoch


def epoch_to_json_value(epoch: Decimal) -> int | str:
    """把纪元秒编码为可无损往返的 JSON 标量。

    整数秒写 int；带小数秒写十进制字符串（存储与响应都优先保证微秒精度，
    避免 JSON 浮点在大纪元值上丢尾数）。旧库里的 int 仍可读。
    """
    if epoch == epoch.to_integral_value():
        return int(epoch)
    return format(epoch, "f")


def epoch_to_api_number(epoch: Decimal) -> int | float | str:
    """响应里的纪元秒：整数写 int，小数写 JSON 数字；仅当二进制浮点无法
    精确表示时退化为十进制字符串，确保 6 位小数不丢精度。"""
    if epoch == epoch.to_integral_value():
        return int(epoch)
    as_float = float(epoch)
    if Decimal(repr(as_float)) == epoch:
        return as_float
    return format(epoch, "f")


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


def _utc_microseconds(aware: datetime) -> int:
    """带时区时刻 -> 自 Unix 纪元起的整数微秒（负值同样精确）。"""
    delta = aware.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _microseconds_to_wall(micros: int, tz: ZoneInfo) -> datetime:
    """整数微秒 -> 指定时区的无时区墙钟时刻（fold 归零便于比较）。"""
    instant = _EPOCH + timedelta(microseconds=micros)
    return instant.astimezone(tz).replace(tzinfo=None, fold=0)


def local_naive_to_utc_candidates(wall: datetime, tz: ZoneInfo) -> list[Decimal]:
    """把本地「无时区」墙钟时刻换算为全部合法的 UTC 纪元秒候选。

    返回列表按时间升序、去重：

    * 普通时刻：1 个候选；
    * 回拨重叠时刻（夏令时结束、本地时刻出现两次）：2 个候选，
      分别对应 fold=0（第一次/夏令时）与 fold=1（第二次/标准时）；
    * 春令时跳空的不存在时刻：0 个候选（调用方据此判定档案无解）。

    换算全程以整数微秒完成，``1.25`` 这类小数秒既不会被截断，也不会被
    误判成春令时跳空；回查同样精确到微秒。
    """
    if wall.tzinfo is not None:
        # 输入模型已禁止带偏移的时刻，这里再兜底一次。
        wall = wall.replace(tzinfo=None)
    wall = wall.replace(fold=0)

    candidates: list[Decimal] = []
    for fold in (0, 1):
        micros = _utc_microseconds(wall.replace(tzinfo=tz, fold=fold))
        # 回查：把换算结果再投影回本地墙钟。跳空时刻会被规整到相邻的
        # 合法时刻，回查必然不等于原输入，据此识别「不存在的本地时刻」。
        if _microseconds_to_wall(micros, tz) != wall:
            continue
        epoch = Decimal(micros) / MICROS_PER_SECOND
        if epoch not in candidates:
            candidates.append(epoch)
    return sorted(candidates)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Program:
    seq: int
    duration: int
    # 合法 UTC 开播候选（Decimal 纪元秒，升序、去重），空元组=不存在的本地时刻
    starts: tuple[Decimal, ...]


@dataclass(frozen=True)
class Blackout:
    start: Decimal
    end: Decimal


@dataclass(frozen=True)
class ArrangementModel:
    """一份已提交档案的可计算模型：节目按流水号升序，停播段按起点升序。"""

    timezone: str
    programs: tuple[Program, ...]
    blackouts: tuple[Blackout, ...]


# --------------------------------------------------------------------------- #
# 区间工具（半开：含起点、不含终点）
# --------------------------------------------------------------------------- #
def _overlaps(
    a_start: Decimal, a_end: Decimal, b_start: Decimal, b_end: Decimal
) -> bool:
    return a_start < b_end and b_start < a_end


def _hits_blackout(
    start: Decimal, end: Decimal, blackouts: tuple[Blackout, ...]
) -> bool:
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


def resolve_query_window(t: Decimal) -> tuple[Decimal, Decimal, bool]:
    """把查询时刻规范成「时间窗口」。

    * 整秒查询（``t`` 是整数）：窗口为该整秒 ``[t, t + 1)``。这样按整秒查档
      仍能命中在该秒内（哪怕是在 ``.25`` 处）开播或收播的节目；对全部由整秒
      边界构成的旧档案，结果与「查 ``t`` 这一时刻」完全一致。
    * 带小数秒查询：窗口退化为精确时刻 ``t``（``is_point=True``），按微秒判定。

    返回 ``(lo, hi, is_point)``。
    """
    if t == t.to_integral_value():
        return t, t + 1, False
    return t, t, True


def _in_window(
    start: Decimal,
    end: Decimal,
    lo: Decimal,
    hi: Decimal,
    is_point: bool,
) -> bool:
    """半开区间 ``[start, end)`` 是否与查询窗口相交（点查询等价于 ``start <= t < end``）。"""
    if is_point:
        return start <= lo < end
    return start < hi and lo < end


def query_second(model: ArrangementModel, t: Decimal) -> dict:
    """查询 UTC 秒 ``t`` 的播出情况。

    返回字段：

    * ``on_air``：在至少一种完整排法中，时段与查询窗口相交的节目流水号
      （节目时段为半开区间 ``[start, end)``）。整秒查询的窗口是该秒
      ``[t, t + 1)``，带小数秒查询则是精确时刻；
    * ``may_be_gap``：是否存在一种完整排法，使查询窗口完全没被任何节目覆盖
      （停播段内的秒恒为空档）；
    * ``in_blackout``：查询窗口是否与某个停播段相交；
    * ``arrangement_count``：该档案完整排法总数。
    """
    t = coerce_epoch_seconds(t)
    lo, hi, is_point = resolve_query_window(t)
    n = len(model.programs)
    in_blackout = any(_in_window(b.start, b.end, lo, hi, is_point) for b in model.blackouts)

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
            if _in_window(start, end, lo, hi, is_point) and reach[i][j] and later[i][j]:
                coverable = True
                break
        if coverable:
            on_air.append(program.seq)

    # 空档：存在完整排法，使没有任何节目覆盖查询窗口。
    # 正向 DP 额外携带「前缀排法是否已覆盖窗口」的状态。
    # covered[k] / uncovered[k]：到达节目 i 的候选 k 时，
    # 前缀中已覆盖 / 尚未覆盖窗口的合法路径数（布尔意义即可，用计数更直观）。
    covered: list[int] = []
    uncovered: list[int] = []
    prev_ends: list[Decimal] | None = None
    for i, program in enumerate(model.programs):
        new_covered: list[int] = []
        new_uncovered: list[int] = []
        new_ends: list[Decimal] = []
        for j, start in enumerate(program.starts):
            end = start + program.duration
            new_ends.append(end)
            if not (reach[i][j] and later[i][j]):
                new_covered.append(0)
                new_uncovered.append(0)
                continue
            covers = _in_window(start, end, lo, hi, is_point)
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
