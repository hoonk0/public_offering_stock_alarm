"""
4대 지표 → 점수 → 등급 산정.

config.py의 임계값 테이블만 보고 동작하므로,
임계값을 바꾸려면 grader 코드는 건드리지 말고 config.py를 수정하면 된다.

규칙:
- 4대 지표 중 하나라도 None이면 등급은 "데이터 부족" (insufficient).
- 그 외에는 0~12점 합산해 GRADE_TABLE에서 찾은 등급 반환.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config import (
    COMPETITION_THRESHOLDS,
    FLOAT_RATIO_THRESHOLDS,
    GRADE_TABLE,
    INDICATOR_WEIGHTS,
    LOCKUP_THRESHOLDS,
    PRICE_POSITION_SCORE,
)
from crawler import IpoDetail

log = logging.getLogger(__name__)

# 데이터 부족 표시용 상수
INSUFFICIENT = "데이터 부족"
INSUFFICIENT_EMOJI = "❓"


@dataclass
class GradeResult:
    """채점 결과."""
    total_score: int
    grade: str
    emoji: str
    breakdown: dict[str, int]   # 각 지표별 점수 (없으면 -1)
    insufficient: bool = False
    missing_fields: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.missing_fields is None:
            self.missing_fields = []


# ---------------------------------------------------------------------------
# 개별 지표 → 점수
# ---------------------------------------------------------------------------
def _score_lower_bound(value: float, table: list[tuple[float, int]]) -> int:
    """
    '값이 임계 이상이면 점수' 형태 (경쟁률, 의무보유).
    table은 (하한, 점수)의 리스트, 큰 값부터 정렬되어 있어야 함.
    """
    for threshold, score in table:
        if value >= threshold:
            return score
    return 0


def _score_upper_bound(value: float, table: list[tuple[float, int]]) -> int:
    """
    '값이 임계 이하이면 점수' 형태 (유통물량 - 낮을수록 좋음).
    table은 (상한, 점수)의 리스트, 작은 값부터 정렬되어 있어야 함.
    """
    for threshold, score in table:
        if value <= threshold:
            return score
    return 0


def score_competition(ratio: float) -> int:
    return _score_lower_bound(ratio, COMPETITION_THRESHOLDS)


def score_lockup(ratio: float) -> int:
    return _score_lower_bound(ratio, LOCKUP_THRESHOLDS)


def score_float(ratio: float) -> int:
    return _score_upper_bound(ratio, FLOAT_RATIO_THRESHOLDS)


def score_price_position(position: str) -> int:
    return PRICE_POSITION_SCORE.get(position, 0)


# ---------------------------------------------------------------------------
# 등급 매핑
# ---------------------------------------------------------------------------
def _classify_grade(total: int) -> tuple[str, str]:
    """총점 → (등급명, 이모지). GRADE_TABLE은 점수 큰 것부터 정렬."""
    for min_score, name, emoji in GRADE_TABLE:
        if total >= min_score:
            return name, emoji
    # GRADE_TABLE 마지막 줄(0,...)에서 잡혀야 정상이지만 안전장치
    return "패스", "❌"


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def grade(detail: IpoDetail) -> GradeResult:
    """
    IpoDetail의 4대 지표를 보고 GradeResult를 만든다.

    필수 지표(경쟁률/의무보유/공모가 위치) 중 하나라도 누락이면 insufficient.
    유통가능물량(float_ratio)은 38커뮤가 종목별로 표 구조가 달라 합산 추정이 종종 실패하므로,
    누락 시에도 그 항목만 0점 처리하고 나머지로 등급 산정한다.
    """
    missing_critical: list[str] = []
    if detail.competition_ratio is None:
        missing_critical.append("기관 경쟁률")
    if detail.lockup_ratio is None:
        missing_critical.append("의무보유확약")
    if detail.price_position is None:
        missing_critical.append("공모가 결정 위치")

    if missing_critical:
        log.info("[%s/%s] 데이터 부족: %s", detail.no, detail.name, ", ".join(missing_critical))
        return GradeResult(
            total_score=0,
            grade=INSUFFICIENT,
            emoji=INSUFFICIENT_EMOJI,
            breakdown={"competition": -1, "lockup": -1, "float": -1, "price": -1},
            insufficient=True,
            missing_fields=missing_critical,
        )

    s_comp = score_competition(detail.competition_ratio)        # type: ignore[arg-type]
    s_lock = score_lockup(detail.lockup_ratio)                  # type: ignore[arg-type]
    s_float = score_float(detail.float_ratio) if detail.float_ratio is not None else 0
    s_price = score_price_position(detail.price_position)       # type: ignore[arg-type]

    # 가중치 적용 (config.INDICATOR_WEIGHTS 기반)
    w_comp  = s_comp  * INDICATOR_WEIGHTS["competition"]
    w_lock  = s_lock  * INDICATOR_WEIGHTS["lockup"]
    w_float = s_float * INDICATOR_WEIGHTS["float"]
    w_price = s_price * INDICATOR_WEIGHTS["price"]
    total = int(round(w_comp + w_lock + w_float + w_price))

    grade_name, emoji = _classify_grade(total)

    return GradeResult(
        total_score=total,
        grade=grade_name,
        emoji=emoji,
        breakdown={
            "competition": int(round(w_comp)),
            "lockup":      int(round(w_lock)),
            "float":       int(round(w_float)),
            "price":       int(round(w_price)),
        },
        missing_fields=["유통가능물량"] if detail.float_ratio is None else [],
    )


# ---------------------------------------------------------------------------
# 간단 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 가짜 IpoDetail로 다양한 케이스 점검
    cases = [
        # 풀비례 케이스 (만점)
        IpoDetail(no="1", name="풀비례테스트",
                  competition_ratio=1800.0, lockup_ratio=55.0,
                  float_ratio=18.0, price_position="above_band"),
        # 비례 케이스
        IpoDetail(no="2", name="비례테스트",
                  competition_ratio=1100.0, lockup_ratio=20.0,
                  float_ratio=25.0, price_position="upper"),
        # 균등 케이스 (총 4점)
        IpoDetail(no="3", name="균등테스트",
                  competition_ratio=600.0, lockup_ratio=20.0,
                  float_ratio=35.0, price_position="middle"),
        # 패스 케이스
        IpoDetail(no="4", name="패스테스트",
                  competition_ratio=200.0, lockup_ratio=5.0,
                  float_ratio=50.0, price_position="lower"),
        # 데이터 부족 케이스
        IpoDetail(no="5", name="데이터부족테스트",
                  competition_ratio=1000.0, lockup_ratio=None,
                  float_ratio=20.0, price_position="middle"),
    ]

    for d in cases:
        r = grade(d)
        print(f"{d.name:<14} → {r.emoji} {r.grade:<8} "
              f"(총 {r.total_score}점, breakdown={r.breakdown}, "
              f"insufficient={r.insufficient}, missing={r.missing_fields})")
