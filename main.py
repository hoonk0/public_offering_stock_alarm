"""
메인 실행 / 스케줄러.

3종 알림을 KST(Asia/Seoul) 기준으로 발동:

  1) 매월 MONTHLY_DAY일 MONTHLY_HOUR:MM
       → run_monthly_digest()
       → 그 달의 청약 예정 종목을 한 메시지로 묶어 발송 (등급 X)

  2) 매일 DAY1_HOUR:MM
       → run_day1_alert()
       → 청약 시작일이 '오늘'인 종목만 추려 4대 지표 + 등급 알림

  3) 매일 DAY2_HOUR:MM
       → run_day2_reminder()
       → 청약 종료일이 '오늘'(이고 시작일과 다름)인 종목 → 마감 임박 리마인더

CLI:
    python main.py                       # 스케줄러 모드 (3개 잡 동시 등록)
    python main.py --mode monthly        # 월간 다이제스트 즉시 1회 실행
    python main.py --mode day1           # 청약 첫날 알림 즉시 1회 실행
    python main.py --mode day2           # 청약 둘째날 리마인더 즉시 1회 실행
    python main.py --mode day1 --target-date 2026-05-04   # 특정일로 강제
    python main.py --mode day1 --dry-run # 텔레그램 발송 없이 메시지만 로그
"""

from __future__ import annotations

import argparse
import calendar
import logging
import sys
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import run_polling

from config import (
    DAY1_HOUR,
    DAY1_MINUTE,
    DAY2_HOUR,
    DAY2_MINUTE,
    DAY2_MORNING_HOUR,
    DAY2_MORNING_MINUTE,
    LISTING_DAY_HOUR,
    LISTING_DAY_MINUTE,
    LISTING_EVE_HOUR,
    LISTING_EVE_MINUTE,
    LOG_PATH,
    MONTHLY_DAY,
    MONTHLY_HOUR,
    MONTHLY_MINUTE,
)
from crawler import IpoSchedule, fetch_detail, fetch_listed_stocks, fetch_schedule_list
from db import (
    PHASE_DAY1,
    PHASE_DAY2,
    PHASE_DAY2_MORNING,
    PHASE_LISTING_DAY,
    PHASE_LISTING_EVE,
    already_digested,
    already_sent,
    init_db,
    mark_digested,
    mark_sent,
)
from grader import grade
from notifier import (
    build_message,
    build_monthly_digest_message,
    notify,
    notify_listing,
    notify_monthly_digest,
    send_telegram,
)

log = logging.getLogger("ipo_bot")


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _safe_fetch_schedules() -> list[IpoSchedule]:
    """일정 페이지 크롤링. 실패 시 빈 리스트(전체 잡 중단 방지)."""
    try:
        return fetch_schedule_list()
    except Exception as e:  # noqa: BLE001
        log.exception("청약일정 페이지 크롤링 실패: %s", e)
        return []


def _process_one(sched: IpoSchedule, phase: str, dry_run: bool) -> bool:
    """
    한 종목에 대해 상세 → 채점 → 발송 → DB 기록.
    True 반환이면 발송 성공 (또는 dry-run 시뮬레이션).
    """
    if already_sent(sched.no, sched.subscribe_start, phase):
        log.info("[%s/%s/%s] 이미 발송됨 - 스킵", sched.no, sched.name, phase)
        return False

    try:
        detail = fetch_detail(sched.no, sched.name, base=sched)
    except Exception as e:  # noqa: BLE001
        log.exception("[%s/%s] 상세 파싱 실패: %s", sched.no, sched.name, e)
        return False

    result = grade(detail)

    if dry_run:
        preview = build_message(detail, result, phase=phase)
        log.info("[DRY-RUN/%s] %s/%s 등급=%s 점수=%s\n%s",
                 phase, sched.no, sched.name, result.grade, result.total_score, preview)
        return True

    ok = notify(detail, result, phase=phase)
    if ok:
        mark_sent(
            no=sched.no,
            subscribe_day=sched.subscribe_start,
            phase=phase,
            name=sched.name,
            grade=result.grade,
            total_score=result.total_score,
        )
    return ok


# ---------------------------------------------------------------------------
# 1) 월간 다이제스트
# ---------------------------------------------------------------------------
def run_monthly_digest(target_month: Optional[date] = None,
                       dry_run: bool = False,
                       force: bool = False) -> None:
    """
    target_month가 속한 연-월의 청약 예정 종목을 한 통으로 발송.
    target_month 미지정 시 today.
    이미 그 달 다이제스트를 보냈으면 스킵 (force=True면 무시하고 재발송).
    """
    init_db()
    if target_month is None:
        target_month = date.today()

    year, month = target_month.year, target_month.month
    ym_key = f"{year:04d}-{month:02d}"
    log.info("=== 월간 다이제스트 시작 (%s, dry_run=%s, force=%s) ===",
             ym_key, dry_run, force)

    if not force and already_digested(ym_key):
        log.info("[%s] 이미 다이제스트 발송됨 - 스킵", ym_key)
        return

    schedules = _safe_fetch_schedules()
    last_day = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    items = [
        s for s in schedules
        if s.subscribe_start is not None
        and month_start <= s.subscribe_start <= month_end
    ]
    log.info("%s 청약 예정 %d종목", ym_key, len(items))

    if dry_run:
        msg = build_monthly_digest_message(items, year, month)
        log.info("[DRY-RUN/monthly]\n%s", msg)
        return

    ok = notify_monthly_digest(items, year, month)
    if ok:
        mark_digested(ym_key, item_count=len(items))
    log.info("=== 월간 다이제스트 종료 (발송=%s) ===", ok)


# ---------------------------------------------------------------------------
# 2) 청약 첫날 알림 (D-Day 08:30)
# ---------------------------------------------------------------------------
def run_day1_alert(target_day: Optional[date] = None,
                   dry_run: bool = False) -> None:
    """청약 시작일이 target_day(기본: 오늘)인 종목 → 4대 지표 + 등급 알림."""
    init_db()
    if target_day is None:
        target_day = date.today()
    log.info("=== Day1 알림 시작 (target_day=%s, dry_run=%s) ===", target_day, dry_run)

    schedules = _safe_fetch_schedules()
    targets = [s for s in schedules if s.subscribe_start == target_day]
    log.info("청약 첫날 매칭 %d종목", len(targets))

    sent = skip = fail = 0
    for s in targets:
        try:
            ok = _process_one(s, PHASE_DAY1, dry_run)
            if ok:
                sent += 1
            elif already_sent(s.no, s.subscribe_start, PHASE_DAY1):
                skip += 1
            else:
                fail += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s/%s] day1 처리 중 예외: %s", s.no, s.name, e)
            fail += 1

    log.info("=== Day1 알림 종료: 발송 %d, 스킵 %d, 실패 %d ===", sent, skip, fail)


# ---------------------------------------------------------------------------
# 3-a) 청약 둘째날 오전 알림 (마감일 08:30)
# ---------------------------------------------------------------------------
def run_day2_morning_alert(target_day: Optional[date] = None,
                           dry_run: bool = False) -> None:
    """청약 마감일 아침 알림. Day2 리마인더와 동일 로직, phase만 다름."""
    init_db()
    if target_day is None:
        target_day = date.today()
    log.info("=== Day2 오전 알림 시작 (target_day=%s, dry_run=%s) ===", target_day, dry_run)

    schedules = _safe_fetch_schedules()
    targets = [
        s for s in schedules
        if s.subscribe_end == target_day
        and s.subscribe_start is not None
        and s.subscribe_start != s.subscribe_end
    ]
    log.info("청약 둘째날(오전) 매칭 %d종목", len(targets))

    sent = skip = fail = 0
    for s in targets:
        try:
            ok = _process_one(s, PHASE_DAY2_MORNING, dry_run)
            if ok:
                sent += 1
            elif already_sent(s.no, s.subscribe_start, PHASE_DAY2_MORNING):
                skip += 1
            else:
                fail += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s/%s] day2_morning 처리 중 예외: %s", s.no, s.name, e)
            fail += 1

    log.info("=== Day2 오전 알림 종료: 발송 %d, 스킵 %d, 실패 %d ===", sent, skip, fail)


# ---------------------------------------------------------------------------
# 3-b) 청약 둘째날 마감 임박 리마인더 (마감일 15:00)
# ---------------------------------------------------------------------------
def run_day2_reminder(target_day: Optional[date] = None,
                      dry_run: bool = False) -> None:
    """
    청약 종료일이 target_day(기본: 오늘)이고
    종료일과 시작일이 다른 종목(=2일짜리 청약의 마감일) → 리마인더.
    단일일 청약(start==end)은 day1으로 충분하므로 스킵.
    """
    init_db()
    if target_day is None:
        target_day = date.today()
    log.info("=== Day2 리마인더 시작 (target_day=%s, dry_run=%s) ===", target_day, dry_run)

    schedules = _safe_fetch_schedules()
    targets = [
        s for s in schedules
        if s.subscribe_end == target_day
        and s.subscribe_start is not None
        and s.subscribe_start != s.subscribe_end
    ]
    log.info("청약 둘째날(마감) 매칭 %d종목", len(targets))

    sent = skip = fail = 0
    for s in targets:
        try:
            ok = _process_one(s, PHASE_DAY2, dry_run)
            if ok:
                sent += 1
            elif already_sent(s.no, s.subscribe_start, PHASE_DAY2):
                skip += 1
            else:
                fail += 1
        except Exception as e:  # noqa: BLE001
            log.exception("[%s/%s] day2 처리 중 예외: %s", s.no, s.name, e)
            fail += 1

    log.info("=== Day2 리마인더 종료: 발송 %d, 스킵 %d, 실패 %d ===", sent, skip, fail)


# ---------------------------------------------------------------------------
# 4) 상장 전날/당일 알림
# ---------------------------------------------------------------------------
def _run_listing_alert(target_listing_day: date, phase: str, dry_run: bool) -> None:
    """
    target_listing_day에 상장하는 종목들에게 알림 발송.
    phase: PHASE_LISTING_EVE (전날 15:00) 또는 PHASE_LISTING_DAY (당일 08:30)
    """
    init_db()
    log.info("=== 상장 알림(%s) 시작 (listing_day=%s, dry_run=%s) ===",
             phase, target_listing_day, dry_run)

    # 신규상장 페이지에서 작년+올해 데이터로 후보 추출
    candidates = []
    today_year = date.today().year
    for y in (today_year - 1, today_year):
        try:
            candidates.extend(fetch_listed_stocks(y))
        except Exception as e:  # noqa: BLE001
            log.exception("listed_stocks(%d) fetch 실패: %s", y, e)

    targets = [s for s in candidates if s.listing_date == target_listing_day]
    log.info("상장 매칭 %d종목", len(targets))

    sent = skip = fail = 0
    for s in targets:
        # 중복 방지 키 = (no, listing_date, phase)
        if already_sent(s.no, s.listing_date, phase):
            log.info("[%s/%s/%s] 이미 발송됨 - 스킵", s.no, s.name, phase)
            skip += 1
            continue

        try:
            detail = fetch_detail(s.no, s.name)
            # listed_stocks에 있는 정보를 detail에도 보강
            if not detail.listing_date:
                detail.listing_date = s.listing_date
            if not detail.final_price:
                detail.final_price = s.offering_price
        except Exception as e:  # noqa: BLE001
            log.exception("[%s/%s] 상세 파싱 실패: %s", s.no, s.name, e)
            fail += 1
            continue

        result = grade(detail)

        if dry_run:
            from notifier import build_listing_message
            preview = build_listing_message(detail, result, phase=phase)
            log.info("[DRY-RUN/%s] %s/%s\n%s", phase, s.no, s.name, preview)
            sent += 1
            continue

        ok = notify_listing(detail, result, phase=phase)
        if ok:
            mark_sent(
                no=s.no,
                subscribe_day=s.listing_date,  # 상장 알림에선 상장일을 key로
                phase=phase,
                name=s.name,
                grade=result.grade,
                total_score=result.total_score,
            )
            sent += 1
        else:
            fail += 1

    log.info("=== 상장 알림(%s) 종료: 발송 %d, 스킵 %d, 실패 %d ===",
             phase, sent, skip, fail)


def run_listing_eve_alert(target_listing_day: Optional[date] = None,
                          dry_run: bool = False) -> None:
    """상장 전날 알림: target_listing_day(기본: 내일)에 상장하는 종목들."""
    if target_listing_day is None:
        target_listing_day = date.today() + timedelta(days=1)
    _run_listing_alert(target_listing_day, PHASE_LISTING_EVE, dry_run)


def run_listing_day_alert(target_listing_day: Optional[date] = None,
                          dry_run: bool = False) -> None:
    """상장 당일 알림: target_listing_day(기본: 오늘)에 상장하는 종목들."""
    if target_listing_day is None:
        target_listing_day = date.today()
    _run_listing_alert(target_listing_day, PHASE_LISTING_DAY, dry_run)


# ---------------------------------------------------------------------------
# 캐시 자동 갱신 (백그라운드)
# 라즈베리파이처럼 38커뮤 fetch가 느린 환경에서도 사용자는 항상 즉시 응답 받게.
# 매시간 정각에 백그라운드로 schedule_list + listed_stocks 캐시 갱신.
# ---------------------------------------------------------------------------
def refresh_caches() -> None:
    """
    fetch_schedule_list + fetch_listed_stocks (작년+올해 2년치) 캐시 갱신.
    수익률 조회가 작년+올해 데이터를 쓰므로 둘 다 워밍업.
    """
    log.info("=== 캐시 자동 갱신 시작 ===")
    try:
        schedules = fetch_schedule_list()
        log.info("schedule_list 갱신: %d종목", len(schedules))
    except Exception as e:  # noqa: BLE001
        log.exception("schedule_list 갱신 실패: %s", e)

    from crawler import fetch_listed_stocks
    today = date.today()
    for year in (today.year - 1, today.year):
        try:
            listed = fetch_listed_stocks(year)
            log.info("listed_stocks(%d) 갱신: %d종목", year, len(listed))
        except Exception as e:  # noqa: BLE001
            log.exception("listed_stocks(%d) 갱신 실패: %s", year, e)
    log.info("=== 캐시 자동 갱신 완료 ===")


# ---------------------------------------------------------------------------
# 스케줄러
# ---------------------------------------------------------------------------
def run_scheduler(enable_polling: bool = True) -> None:
    """
    3개 cron 잡을 백그라운드로 등록하고, 메인 스레드에서 텔레그램 봇 폴링 실행.
    enable_polling=False면 cron만 돌리고 폴링은 스킵 (봇 명령어 응답 안 함).
    """
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    scheduler.add_job(
        run_monthly_digest,
        trigger=CronTrigger(day=MONTHLY_DAY, hour=MONTHLY_HOUR, minute=MONTHLY_MINUTE),
        name="ipo_monthly_digest",
    )
    scheduler.add_job(
        run_day1_alert,
        trigger=CronTrigger(hour=DAY1_HOUR, minute=DAY1_MINUTE),
        name="ipo_day1_alert",
    )
    scheduler.add_job(
        run_day2_morning_alert,
        trigger=CronTrigger(hour=DAY2_MORNING_HOUR, minute=DAY2_MORNING_MINUTE),
        name="ipo_day2_morning",
    )
    scheduler.add_job(
        run_day2_reminder,
        trigger=CronTrigger(hour=DAY2_HOUR, minute=DAY2_MINUTE),
        name="ipo_day2_reminder",
    )
    # 상장 전날 15:00, 상장 당일 08:30
    scheduler.add_job(
        run_listing_eve_alert,
        trigger=CronTrigger(hour=LISTING_EVE_HOUR, minute=LISTING_EVE_MINUTE),
        name="ipo_listing_eve",
    )
    scheduler.add_job(
        run_listing_day_alert,
        trigger=CronTrigger(hour=LISTING_DAY_HOUR, minute=LISTING_DAY_MINUTE),
        name="ipo_listing_day",
    )
    # 캐시 자동 갱신: 매시간 정각 (사용자는 항상 즉시 응답)
    scheduler.add_job(
        refresh_caches,
        trigger=CronTrigger(minute=0),
        name="cache_refresh",
    )

    log.info(
        "스케줄러 시작 (KST): "
        "월간=매월%d일 %02d:%02d, Day1=매일 %02d:%02d, "
        "Day2오전=매일 %02d:%02d, Day2오후=매일 %02d:%02d, "
        "상장D-1=매일 %02d:%02d, 상장당일=매일 %02d:%02d, 캐시갱신=매시간 정각",
        MONTHLY_DAY, MONTHLY_HOUR, MONTHLY_MINUTE,
        DAY1_HOUR, DAY1_MINUTE,
        DAY2_MORNING_HOUR, DAY2_MORNING_MINUTE,
        DAY2_HOUR, DAY2_MINUTE,
        LISTING_EVE_HOUR, LISTING_EVE_MINUTE,
        LISTING_DAY_HOUR, LISTING_DAY_MINUTE,
    )
    scheduler.start()

    # 봇 시작 즉시 첫 워밍업 (백그라운드, 폴링 블로킹 안 됨)
    import threading
    threading.Thread(target=refresh_caches, name="initial_warmup", daemon=True).start()

    try:
        if enable_polling:
            # 메인 스레드에서 폴링 (블로킹). cron은 백그라운드 스레드에서 계속 발동됨.
            run_polling()
        else:
            log.info("폴링 비활성화 — Ctrl+C로 종료")
            import threading
            threading.Event().wait()  # 무한 대기 (cron만 돌게)
    except (KeyboardInterrupt, SystemExit):
        log.info("종료 신호 수신")
    finally:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_date_arg(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="IPO 등급 알림 봇")
    parser.add_argument(
        "--mode",
        choices=["monthly", "day1", "day2_morning", "day2", "listing_eve", "listing_day"],
        default=None,
        help="즉시 1회 실행할 잡. 미지정 시 스케줄러 모드.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="텔레그램 발송 없이 로그만")
    parser.add_argument("--target-date", type=_parse_date_arg, default=None,
                        help="day1/day2 모드용: 청약 시작일(또는 종료일) 강제 지정. "
                             "monthly 모드: 그 날짜가 속한 달을 대상으로.")
    parser.add_argument("--force", action="store_true",
                        help="monthly 모드에서 이미 발송됐어도 재발송")
    parser.add_argument("--no-polling", action="store_true",
                        help="스케줄러 모드에서 봇 명령어 폴링 비활성화")
    parser.add_argument("--bot-only", action="store_true",
                        help="cron 스케줄러 없이 봇 명령어 폴링만 실행")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG 로그")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if args.mode == "monthly":
        run_monthly_digest(target_month=args.target_date, dry_run=args.dry_run, force=args.force)
    elif args.mode == "day1":
        run_day1_alert(target_day=args.target_date, dry_run=args.dry_run)
    elif args.mode == "day2_morning":
        run_day2_morning_alert(target_day=args.target_date, dry_run=args.dry_run)
    elif args.mode == "day2":
        run_day2_reminder(target_day=args.target_date, dry_run=args.dry_run)
    elif args.mode == "listing_eve":
        run_listing_eve_alert(target_listing_day=args.target_date, dry_run=args.dry_run)
    elif args.mode == "listing_day":
        run_listing_day_alert(target_listing_day=args.target_date, dry_run=args.dry_run)
    elif args.bot_only:
        run_polling()
    else:
        run_scheduler(enable_polling=not args.no_polling)


if __name__ == "__main__":
    main()
