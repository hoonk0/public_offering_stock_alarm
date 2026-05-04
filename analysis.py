"""
2025+2026 상장 종목들의 4대 지표 vs 첫날 수익률 분석.

목적: 어느 지표가 첫날 시초가 수익률과 가장 강하게 상관 있는지 보고
      가중치를 데이터 기반으로 재조정.

산출물:
- 종목별 raw 데이터 (CSV로도 저장 가능)
- 그룹별 평균 (대박 / 중간 / 실패)
- 각 지표별 상관계수
- 가중치 제안
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass
from typing import Optional

from crawler import IpoDetail, ListedStock, fetch_detail, fetch_listed_stocks
from db import get_cached_grade, init_db, set_cached_grade
from grader import grade as compute_grade

log = logging.getLogger(__name__)

# 추가로 detail에서 직접 4대 지표 가져오는 캐시 (기존 grade 캐시는 등급/점수만 저장)
# 분석에는 raw 지표값이 필요해서 별도 in-memory 캐시.
_DETAIL_CACHE: dict[str, IpoDetail] = {}


@dataclass
class StockRow:
    no: str
    name: str
    listing_date: str
    offering_price: int
    open_price: int
    close_price: int
    open_return: float
    close_return: float
    competition_ratio: Optional[float]
    lockup_ratio: Optional[float]
    float_ratio: Optional[float]
    price_position: Optional[str]
    band_low: Optional[int]
    band_high: Optional[int]
    final_price: Optional[int]


def _fetch_indicators(no: str, name: str) -> IpoDetail:
    """In-memory 캐시 사용해 detail 1회만 fetch."""
    if no in _DETAIL_CACHE:
        return _DETAIL_CACHE[no]
    d = fetch_detail(no, name)
    _DETAIL_CACHE[no] = d
    # grade 캐시도 채워둠 (bot에서 재사용)
    r = compute_grade(d)
    set_cached_grade(no, name, r.grade, r.total_score, r.insufficient)
    return d


def collect_data(years: list[int]) -> list[StockRow]:
    """여러 연도의 상장 종목 + 4대 지표 수집."""
    init_db()
    rows: list[StockRow] = []
    for year in years:
        log.info("=== %d년 데이터 수집 시작 ===", year)
        listed = fetch_listed_stocks(year)
        listed = [s for s in listed if s.close_price and s.open_price and s.offering_price]
        log.info("%d년 상장 완료 종목: %d", year, len(listed))

        for i, s in enumerate(listed, 1):
            try:
                d = _fetch_indicators(s.no, s.name)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s/%s] detail fetch 실패: %s", s.no, s.name, e)
                continue

            rows.append(StockRow(
                no=s.no,
                name=s.name,
                listing_date=s.listing_date.isoformat() if s.listing_date else "",
                offering_price=s.offering_price,
                open_price=s.open_price,
                close_price=s.close_price,
                open_return=s.open_return_pct,
                close_return=s.return_pct,
                competition_ratio=d.competition_ratio,
                lockup_ratio=d.lockup_ratio,
                float_ratio=d.float_ratio,
                price_position=d.price_position,
                band_low=d.band_low,
                band_high=d.band_high,
                final_price=d.final_price,
            ))
            if i % 10 == 0:
                log.info("[%d/%d] 진행", i, len(listed))
    return rows


def save_csv(rows: list[StockRow], path: str = "analysis.csv") -> None:
    fields = [
        "no", "name", "listing_date", "offering_price", "open_price", "close_price",
        "open_return", "close_return",
        "competition_ratio", "lockup_ratio", "float_ratio", "price_position",
        "band_low", "band_high", "final_price",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r.__dict__)
    log.info("CSV 저장: %s (%d행)", path, len(rows))


# ---------------------------------------------------------------------------
# 통계
# ---------------------------------------------------------------------------
def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson 상관계수 (-1.0 ~ 1.0). 결측 쌍 제거 후."""
    pairs = [(x, y) for x, y in zip(xs, ys) if not (math.isnan(x) or math.isnan(y))]
    if len(pairs) < 3:
        return float("nan")
    xs2 = [p[0] for p in pairs]
    ys2 = [p[1] for p in pairs]
    mx = _avg(xs2)
    my = _avg(ys2)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs2))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys2))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


_PRICE_POS_NUMERIC = {
    "above_band": 4,
    "upper": 3,
    "middle": 2,
    "lower": 1,
    "below_band": 0,
}


def analyze(rows: list[StockRow]) -> None:
    """
    1. 그룹별 평균 (시초가 수익률 기준 대박/중간/실패)
    2. 각 지표 vs 시초가 수익률 Pearson 상관계수
    3. 가중치 제안
    """
    valid = [
        r for r in rows
        if r.competition_ratio is not None
        and r.lockup_ratio is not None
        and r.price_position is not None
        and r.open_return is not None
    ]
    log.info("\n분석 대상: %d종목 (필수 3지표 모두 파싱된 케이스)", len(valid))

    # 그룹별 평균
    big = [r for r in valid if r.open_return >= 200.0]
    mid = [r for r in valid if 50.0 <= r.open_return < 200.0]
    bad = [r for r in valid if r.open_return < 50.0]

    print("\n" + "=" * 80)
    print(f"{'그룹':<20} {'N':>4} {'시초률평균':>12} {'경쟁률':>10} {'의무보유':>10} {'유통':>8} {'공모가':>8}")
    print("-" * 80)
    for label, grp in [
        ("🔥 대박 (≥+200%)", big),
        ("✅ 중간 (+50~200)", mid),
        ("❌ 실패 (<+50%)", bad),
    ]:
        if not grp:
            continue
        avg_ret = _avg([r.open_return for r in grp])
        avg_comp = _avg([r.competition_ratio for r in grp if r.competition_ratio is not None])
        avg_lock = _avg([r.lockup_ratio for r in grp if r.lockup_ratio is not None])
        floats = [r.float_ratio for r in grp if r.float_ratio is not None]
        avg_float = _avg(floats) if floats else float("nan")
        avg_price = _avg([_PRICE_POS_NUMERIC.get(r.price_position, 2) for r in grp])
        print(f"{label:<20} {len(grp):>4} {avg_ret:>+11.1f}% {avg_comp:>9.0f}:1 "
              f"{avg_lock:>9.1f}% {avg_float:>7.1f}% {avg_price:>7.2f}")

    # 상관계수
    print("\n=== 지표 vs 시초가 수익률 Pearson 상관계수 ===")
    print(f"(N={len(valid)}, -1=역상관, 0=무관, +1=강한 정상관)")
    print("-" * 60)
    rets = [r.open_return for r in valid]

    def corr(label: str, getter, valid_filter=lambda r: True) -> None:
        sub = [r for r in valid if valid_filter(r)]
        xs = [getter(r) for r in sub]
        ys = [r.open_return for r in sub]
        c = _pearson(xs, ys)
        print(f"  {label:<28} {c:>+.3f}  (N={len(sub)})")

    corr("기관 경쟁률", lambda r: r.competition_ratio,
         lambda r: r.competition_ratio is not None)
    corr("의무보유확약 (%)", lambda r: r.lockup_ratio,
         lambda r: r.lockup_ratio is not None)
    corr("유통물량 (%) — 역상관 기대", lambda r: r.float_ratio,
         lambda r: r.float_ratio is not None)
    corr("공모가위치 (above=4..below=0)",
         lambda r: _PRICE_POS_NUMERIC.get(r.price_position, 2))

    # 공모가 위치별 평균 시초가 수익률
    print("\n=== 공모가 위치별 평균 시초가 수익률 ===")
    print("-" * 50)
    from collections import defaultdict
    pos_groups: dict[str, list[float]] = defaultdict(list)
    for r in valid:
        pos_groups[r.price_position or "unknown"].append(r.open_return)
    for pos in ["above_band", "upper", "middle", "lower", "below_band", "unknown"]:
        if pos in pos_groups:
            xs = pos_groups[pos]
            print(f"  {pos:<12} N={len(xs):>3}  평균 {_avg(xs):>+7.1f}%  중앙 {sorted(xs)[len(xs)//2]:>+7.1f}%")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    rows = collect_data([2025, 2026])
    save_csv(rows)
    analyze(rows)
