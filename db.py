"""
SQLite 기반 중복 발송 방지.

테이블 두 개:

1) sent_alerts
   - 청약 첫날(day1)/둘째날(day2) 알림 중복 방지
   - PK (no, subscribe_day, phase)
     · phase = 'day1' | 'day2'
     · 같은 종목이라도 첫날/둘째날 따로 카운트해서 둘 다 보낼 수 있게.

2) monthly_digests
   - 매월 1일 다이제스트가 그 달에 이미 보내졌는지 추적
   - PK year_month (예: '2026-05')
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from config import DB_PATH

log = logging.getLogger(__name__)

# 발송 단계
PHASE_DAY1 = "day1"                  # 청약 첫날 08:30
PHASE_DAY2_MORNING = "day2_morning"  # 청약 마감일 08:30
PHASE_DAY2 = "day2"                  # 청약 마감일 15:00 임박 리마인더
PHASE_REFUND = "refund"              # 환불일 09:00 (마통 갚으라고)
PHASE_LISTING_EVE = "listing_eve"    # 상장 전날 15:00
PHASE_LISTING_DAY = "listing_day"    # 상장 당일 08:30


@contextmanager
def _connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """필요 테이블 생성. 이미 있으면 무시."""
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_alerts (
                no            TEXT NOT NULL,
                subscribe_day TEXT NOT NULL,
                phase         TEXT NOT NULL,
                name          TEXT,
                grade         TEXT,
                total_score   INTEGER,
                sent_at       TEXT NOT NULL,
                PRIMARY KEY (no, subscribe_day, phase)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_digests (
                year_month TEXT PRIMARY KEY,   -- 'YYYY-MM'
                item_count INTEGER,
                sent_at    TEXT NOT NULL
            )
        """)
        # 종목별 등급 캐시 - 등급별 수익률 조회 시 매번 detail 파싱 안 하려고
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_grades (
                no            TEXT PRIMARY KEY,
                name          TEXT,
                grade         TEXT,
                total_score   INTEGER,
                insufficient  INTEGER,         -- 0/1
                computed_at   TEXT NOT NULL
            )
        """)
    log.debug("DB 초기화 완료: %s", db_path)


def get_cached_grade(no: str, db_path: Path = DB_PATH) -> Optional[tuple[str, int, bool]]:
    """캐시된 (등급, 점수, 데이터부족여부). 없으면 None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT grade, total_score, insufficient FROM stock_grades WHERE no=?",
            (no,),
        ).fetchone()
    if row is None:
        return None
    return row[0], row[1], bool(row[2])


def set_cached_grade(no: str,
                     name: str,
                     grade: str,
                     total_score: int,
                     insufficient: bool,
                     db_path: Path = DB_PATH) -> None:
    """등급 결과 캐시 (덮어쓰기)."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO stock_grades (no, name, grade, total_score, insufficient, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(no) DO UPDATE SET
                name=excluded.name,
                grade=excluded.grade,
                total_score=excluded.total_score,
                insufficient=excluded.insufficient,
                computed_at=excluded.computed_at
            """,
            (no, name, grade, total_score, 1 if insufficient else 0,
             datetime.now().isoformat(timespec="seconds")),
        )


# ---------------------------------------------------------------------------
# 일일 알림 (day1/day2)
# ---------------------------------------------------------------------------
def already_sent(no: str,
                 subscribe_day: Optional[date],
                 phase: str,
                 db_path: Path = DB_PATH) -> bool:
    """이미 (종목, 청약일, 단계) 조합으로 발송된 적 있으면 True."""
    if subscribe_day is None:
        return False
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_alerts WHERE no=? AND subscribe_day=? AND phase=?",
            (no, subscribe_day.isoformat(), phase),
        ).fetchone()
    return row is not None


def mark_sent(no: str,
              subscribe_day: Optional[date],
              phase: str,
              name: str,
              grade: str,
              total_score: int,
              db_path: Path = DB_PATH) -> None:
    """알림 발송 완료 기록."""
    if subscribe_day is None:
        log.warning("subscribe_day 미상으로 mark_sent 스킵: no=%s name=%s", no, name)
        return
    sent_at = datetime.now().isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sent_alerts
                (no, subscribe_day, phase, name, grade, total_score, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (no, subscribe_day.isoformat(), phase, name, grade, total_score, sent_at),
        )
    log.debug("mark_sent: no=%s day=%s phase=%s grade=%s",
              no, subscribe_day.isoformat(), phase, grade)


# ---------------------------------------------------------------------------
# 월간 다이제스트
# ---------------------------------------------------------------------------
def _ym_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def already_digested(year_month: date | str, db_path: Path = DB_PATH) -> bool:
    """해당 연-월의 월간 다이제스트가 이미 발송됐는지."""
    key = _ym_key(year_month) if isinstance(year_month, date) else year_month
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM monthly_digests WHERE year_month=?", (key,)
        ).fetchone()
    return row is not None


def mark_digested(year_month: date | str,
                  item_count: int,
                  db_path: Path = DB_PATH) -> None:
    key = _ym_key(year_month) if isinstance(year_month, date) else year_month
    sent_at = datetime.now().isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO monthly_digests (year_month, item_count, sent_at)
            VALUES (?, ?, ?)
            """,
            (key, item_count, sent_at),
        )
    log.debug("mark_digested: %s (%d종목)", key, item_count)


# ---------------------------------------------------------------------------
# 간단 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

    tmp = Path("/tmp/ipo_alerts_test.db")
    if tmp.exists():
        tmp.unlink()
    init_db(tmp)

    today = date.today()

    # day1/day2가 별개 phase로 카운트되는지
    assert already_sent("9999", today, PHASE_DAY1, tmp) is False
    mark_sent("9999", today, PHASE_DAY1, "테스트종목", "풀비례", 11, tmp)
    assert already_sent("9999", today, PHASE_DAY1, tmp) is True
    assert already_sent("9999", today, PHASE_DAY2, tmp) is False, "day2는 별개"
    mark_sent("9999", today, PHASE_DAY2, "테스트종목", "풀비례", 11, tmp)
    assert already_sent("9999", today, PHASE_DAY2, tmp) is True

    # 월간 다이제스트
    assert already_digested(today, tmp) is False
    mark_digested(today, item_count=5, db_path=tmp)
    assert already_digested(today, tmp) is True
    assert already_digested("2099-12", tmp) is False

    print("db.py 테스트 통과 ✅")
    tmp.unlink()
