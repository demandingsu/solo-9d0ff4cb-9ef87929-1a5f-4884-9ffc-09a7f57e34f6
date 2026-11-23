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


def test_timezone_conversion_semantics():
    from datetime import datetime

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


def test_invalid_timezone_rejected():
    import pytest

    with pytest.raises(ValueError):
        D.load_zone("Mars/Olympus")
