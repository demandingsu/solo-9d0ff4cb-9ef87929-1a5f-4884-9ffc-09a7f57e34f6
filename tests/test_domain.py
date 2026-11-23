"""DP 与暴力枚举的逐秒一致性测试（含回拨双候选与停播段）。"""

from __future__ import annotations

import itertools

import app.domain as D
from app.domain import ArrangementModel, Blackout, Program, query_second


def _brute(model: ArrangementModel, t: int):
    on_air: set[int] = set()
    gap = False
    total = 0
    ranges = [range(len(p.starts)) for p in model.programs]
    for combo in itertools.product(*ranges):
        intervals = []
        ok = True
        for i, j in enumerate(combo):
            s = model.programs[i].starts[j]
            e = s + model.programs[i].duration
            if any(D._overlaps(s, e, b.start, b.end) for b in model.blackouts):
                ok = False
                break
            intervals.append((s, e))
        if not ok:
            continue
        if any(intervals[k][1] > intervals[k + 1][0] for k in range(len(intervals) - 1)):
            continue
        total += 1
        covered = [
            model.programs[i].seq
            for i, (s, e) in enumerate(intervals)
            if s <= t < e
        ]
        if not covered:
            gap = True
        on_air.update(covered)
    return sorted(on_air), gap, total


MODELS = [
    ArrangementModel("X", (
        Program(1, 900, (1730611800, 1730615400)),
        Program(2, 900, (1730612700, 1730616300)),
    ), ()),
    ArrangementModel("X", (
        Program(1, 600, (0, 3600)),
        Program(2, 600, (600, 4200)),
        Program(3, 600, (1200, 4800)),
    ), ()),
    ArrangementModel("X", (
        Program(1, 900, (100, 200)),
        Program(2, 900, (150, 300)),
        Program(3, 900, (260, 400)),
    ), (Blackout(180, 260),)),
    ArrangementModel("X", (Program(7, 600, (1000,)),), ()),
    ArrangementModel("X", (
        Program(1, 300, (0, 1000)),
        Program(2, 300, (200, 1100)),
    ), (Blackout(500, 900), Blackout(1050, 1400))),
]


def test_dp_matches_brute_force_second_by_second():
    for model in MODELS:
        lo = min(s for p in model.programs for s in p.starts) - 200
        hi = max(s + p.duration for p in model.programs for s in p.starts) + 200
        assert D.count_arrangements(model) == _brute(model, lo)[2]
        for t in range(lo, hi, 37):
            expected_air, expected_gap, expected_total = _brute(model, t)
            result = query_second(model, t)
            assert result["on_air"] == expected_air
            assert result["may_be_gap"] == expected_gap
            assert result["arrangement_count"] == expected_total


def _brute_window(model: ArrangementModel, t):
    """暴力枚举：按 query_second 的查询窗口语义逐窗口核对。"""
    from decimal import Decimal

    from app.domain import _in_window, resolve_query_window

    lo, hi, is_point = resolve_query_window(Decimal(t))
    on_air: set[int] = set()
    gap = False
    total = 0
    for combo in itertools.product(*[range(len(p.starts)) for p in model.programs]):
        intervals = []
        ok = True
        for i, j in enumerate(combo):
            s = model.programs[i].starts[j]
            e = s + model.programs[i].duration
            if any(D._overlaps(s, e, b.start, b.end) for b in model.blackouts):
                ok = False
                break
            intervals.append((s, e))
        if not ok:
            continue
        if any(intervals[k][1] > intervals[k + 1][0] for k in range(len(intervals) - 1)):
            continue
        total += 1
        covered = [
            model.programs[i].seq
            for i, (s, e) in enumerate(intervals)
            if _in_window(s, e, lo, hi, is_point)
        ]
        if not covered:
            gap = True
        on_air.update(covered)
    return sorted(on_air), gap, total


FRACTIONAL_MODELS = [
    # 普通时刻带小数秒：两个节目在 .25/.75 处相接。
    ArrangementModel("X", (
        Program(1, 900, (D.Decimal("1730646000.25"),)),
        Program(2, 900, (D.Decimal("1730646900.75"),)),
    ), ()),
    # 回拨 + 小数秒：双候选，部分排法可能空档。
    ArrangementModel("X", (
        Program(1, 900, (D.Decimal("1730611800.25"), D.Decimal("1730615400.25"))),
        Program(2, 900, (D.Decimal("1730612700.75"), D.Decimal("1730616300.75"))),
    ), ()),
    # 混合精度：旧档整秒候选 + 新档小数秒候选。
    ArrangementModel("X", (
        Program(1, 600, (1730611800, D.Decimal("1730611800.5"))),
        Program(2, 600, (D.Decimal("1730612700.25"),)),
    ), (D.Blackout(1730611800, D.Decimal("1730611800.4")),)),
]


def test_fractional_dp_matches_brute_force_at_boundaries():
    for model in FRACTIONAL_MODELS:
        points = set()
        for p in model.programs:
            for s in p.starts:
                one_us = D.Decimal("0.000001")
                whole = D.Decimal(s).to_integral_value()
                points.update(
                    {
                        s - one_us,
                        s,
                        s + one_us,
                        s + p.duration - one_us,
                        s + p.duration,
                        s + p.duration + one_us,
                        whole,
                        whole + 1,
                    }
                )
        for b in model.blackouts:
            points.update({b.start, b.end, b.start - D.Decimal("0.000001")})
        for t in sorted(points):
            expected = _brute_window(model, t)
            result = query_second(model, t)
            assert (result["on_air"], result["may_be_gap"], result["arrangement_count"]) == expected, (
                model,
                t,
            )
        assert D.count_arrangements(model) == _brute_window(model, 0)[2]


def test_integer_query_hits_fractional_program_within_that_second():
    # 节目在 05:30:00.25Z 开播：按整秒 1730611800 查询仍应命中该秒内的节目。
    model = ArrangementModel("X", (Program(1, 900, (D.Decimal("1730611800.25"),)),), ())
    assert query_second(model, D.Decimal("1730611800"))["on_air"] == [1]
    # 精确到微秒的查询在开播前 0.25 秒应查不到。
    assert query_second(model, D.Decimal("1730611800.249999"))["on_air"] == []
    assert query_second(model, D.Decimal("1730611800.25"))["on_air"] == [1]
    # 终点半开：结束前一微秒仍播出，终点本身不再是该节目。
    assert query_second(model, D.Decimal("1730612700.249999"))["on_air"] == [1]
    assert query_second(model, D.Decimal("1730612700.25"))["on_air"] == []
    # 整秒查询落在结束边界之后的那一秒 -> 不再命中。
    assert query_second(model, D.Decimal("1730612701"))["on_air"] == []


def test_timezone_conversion_semantics():
    from datetime import datetime
    from decimal import Decimal

    from app.domain import load_zone, local_naive_to_utc_candidates

    ny = load_zone("America/New_York")
    # 回拨：两个 UTC 候选
    assert local_naive_to_utc_candidates(datetime(2024, 11, 3, 1, 30), ny) == [
        1730611800,
        1730615400,
    ]
    # 普通时刻：唯一候选
    assert len(local_naive_to_utc_candidates(datetime(2024, 11, 3, 10, 0), ny)) == 1
    # 春令跳空：零候选
    assert local_naive_to_utc_candidates(datetime(2024, 3, 10, 2, 30), ny) == []

    # 小数秒不得被截断：普通时刻仍是一个候选，且保留微秒。
    assert local_naive_to_utc_candidates(datetime(2024, 11, 3, 10, 0, 0, 250000), ny) == [
        Decimal("1730646000.25")
    ]
    assert local_naive_to_utc_candidates(datetime(2024, 11, 3, 10, 0, 0, 123456), ny) == [
        Decimal("1730646000.123456")
    ]
    # 回拨 + 小数秒：两个带小数秒的候选，不能误判成跳空。
    assert local_naive_to_utc_candidates(datetime(2024, 11, 3, 1, 30, 0, 250000), ny) == [
        Decimal("1730611800.25"),
        Decimal("1730615400.25"),
    ]
    # 回拨边界 01:00:00 同样对应两个候选。
    assert local_naive_to_utc_candidates(datetime(2024, 11, 3, 1, 0), ny) == [
        1730610000,
        1730613600,
    ]
    # 春令跳空 + 小数秒：仍然零候选（真正不存在的本地时刻继续拒绝）。
    assert local_naive_to_utc_candidates(datetime(2024, 3, 10, 2, 30, 0, 500000), ny) == []
    # 跳空结束后的第一个合法时刻 03:00 存在（且带小数秒）。
    assert local_naive_to_utc_candidates(datetime(2024, 3, 10, 3, 0, 0, 500000), ny) == [
        Decimal("1710054000.5")
    ]


def test_epoch_coercion_precision():
    from decimal import Decimal

    import pytest

    from app.domain import coerce_epoch_seconds, epoch_to_json_value

    assert coerce_epoch_seconds(1730611800) == Decimal("1730611800")
    assert coerce_epoch_seconds("1730611800.25") == Decimal("1730611800.25")
    assert coerce_epoch_seconds(1730611800.123456) == Decimal("1730611800.123456")
    assert coerce_epoch_seconds(Decimal("1730611800.250000")) == Decimal("1730611800.25")
    # 超过微秒精度必须拒绝，而不是静默截断。
    for bad in ("1730611800.1234567", 1730611800.1234567):
        with pytest.raises(ValueError):
            coerce_epoch_seconds(bad)
    with pytest.raises(ValueError):
        coerce_epoch_seconds("not-a-time")
    with pytest.raises(ValueError):
        coerce_epoch_seconds(float("nan"))
    with pytest.raises(ValueError):
        coerce_epoch_seconds(True)
    # 存储编码：整秒写 int，小数秒写精确十进制文本。
    assert epoch_to_json_value(Decimal("1730611800")) == 1730611800
    assert epoch_to_json_value(Decimal("1730611800.250000")) == "1730611800.250000"


def test_invalid_timezone_rejected():
    import pytest

    with pytest.raises(ValueError):
        D.load_zone("Mars/Olympus")
