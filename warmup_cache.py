"""
캐시 워밍업: 올해 상장 종목 등급을 미리 계산해 stock_grades 테이블에 저장.

라즈베리파이에서 한 번 실행해두면 그 후 텔레그램 /1등급 등 명령이 즉시 응답.
첫 실행은 5분 정도 걸림 (인터넷 속도에 따라).

사용:
    .venv/bin/python warmup_cache.py
"""
from __future__ import annotations
import logging
from datetime import date

from crawler import fetch_listed_stocks, fetch_detail
from db import init_db, get_cached_grade, set_cached_grade
from grader import grade as compute_grade


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    log = logging.getLogger("warmup")

    init_db()
    year = date.today().year
    log.info("=== %d년 종목 캐시 워밍업 시작 ===", year)

    listed = fetch_listed_stocks(year)
    listed = [s for s in listed if s.close_price and s.offering_price]
    log.info("총 %d개 종목 분석", len(listed))

    cached_count = 0
    fetched_count = 0
    failed_count = 0

    for i, s in enumerate(listed, 1):
        if get_cached_grade(s.no) is not None:
            cached_count += 1
            continue
        try:
            d = fetch_detail(s.no, s.name)
            r = compute_grade(d)
            set_cached_grade(s.no, s.name, r.grade, r.total_score, r.insufficient)
            fetched_count += 1
            log.info("[%d/%d] %s → %s (%d/12)",
                     i, len(listed), s.name, r.grade, r.total_score)
        except Exception as e:  # noqa: BLE001
            failed_count += 1
            log.warning("[%s/%s] 실패: %s", s.no, s.name, e)

    log.info("=== 워밍업 완료: 신규 캐시 %d, 기존 %d, 실패 %d ===",
             fetched_count, cached_count, failed_count)


if __name__ == "__main__":
    main()
