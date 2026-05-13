"""
텔레그램 알림 발송.

requests로 Telegram Bot API의 sendMessage를 직접 호출한다.
(python-telegram-bot 라이브러리 의존 줄이려고 직접 호출 방식 채택)

메시지는 MarkdownV2가 아니라 일반 HTML 파싱 모드를 쓴다.
- HTML이 이스케이프 규칙이 단순하고(<,>,& 만), 등급 이모지도 자유롭게 박을 수 있음.
"""

from __future__ import annotations

import html
import logging
from datetime import date
from typing import Optional

import requests

from config import (
    DETAIL_URL_FMT,
    GRADE_HISTORICAL_RETURNS,
    MARGIN_LOAN,
    SCHEDULE_URL,
    TELEGRAM_API_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from crawler import IpoDetail, IpoSchedule
from grader import GradeResult

# 발송 단계 (db.py와 같은 값 사용)
PHASE_DAY1 = "day1"
PHASE_DAY2_MORNING = "day2_morning"
PHASE_DAY2 = "day2"
PHASE_REFUND = "refund"
PHASE_LISTING_EVE = "listing_eve"
PHASE_LISTING_DAY = "listing_day"

log = logging.getLogger(__name__)


def _esc(text: object) -> str:
    """HTML 파싱 모드용 이스케이프. None은 빈 문자열."""
    if text is None:
        return "-"
    return html.escape(str(text), quote=False)


def _margin_loan_section(detail: IpoDetail) -> str:
    """
    마통(마이너스통장) 거치 비용 표시.
    청약 시작일 ~ 환불일까지 마통 7천만원을 거치하는 동안 발생하는 이자만 단순 계산.
    환불일은 정확한 데이터가 없을 땐 청약 종료일 + 2일로 가정.
    """
    amount = int(MARGIN_LOAN["scenario_amount"])
    rate = MARGIN_LOAN["annual_rate"]

    # 거치 기간: 청약 시작 ~ 환불일 추정 (= 청약 종료 + 2일)
    if detail.subscribe_start and detail.subscribe_end:
        from datetime import timedelta
        refund_day = detail.subscribe_end + timedelta(days=2)
        days = (refund_day - detail.subscribe_start).days + 1  # 시작일 포함
    else:
        days = int(MARGIN_LOAN["default_days"])
        refund_day = None

    interest = int(round(amount * rate * days / 365))

    if refund_day and detail.subscribe_start:
        period_str = (
            f"{detail.subscribe_start.strftime('%m/%d')} ~ "
            f"{refund_day.strftime('%m/%d')}(환불일 추정)"
        )
    else:
        period_str = f"{days}일"

    return (
        f"<b>🏦 마통 {amount//10_000_000}천만원 거치 비용</b> "
        f"(연 {rate*100:.1f}%)\n"
        f"• 거치 기간: {_esc(period_str)} ({days}일)\n"
        f"• 마통 이자: <b>-{interest:,}원</b>"
    )


def _fmt_band(detail: IpoDetail) -> str:
    """공모가 밴드/확정가 한 줄 포맷."""
    if detail.band_low is not None and detail.band_high is not None:
        band = f"{detail.band_low:,} ~ {detail.band_high:,}원"
    else:
        band = "-"
    final = f"{detail.final_price:,}원" if detail.final_price is not None else "-"
    pos_kr = {
        "above_band": "밴드 상단 초과",
        "upper": "밴드 상단",
        "middle": "밴드 중간",
        "lower": "밴드 하단",
        "below_band": "밴드 하단 미만",
    }.get(detail.price_position or "", "-")
    return f"{band} → 확정 {final} ({pos_kr})"


def build_message(detail: IpoDetail, result: GradeResult, phase: str = PHASE_DAY1) -> str:
    """
    텔레그램에 보낼 HTML 메시지.
    phase = 'day1' → 청약 첫날 알림 (4대 지표 + 등급)
    phase = 'day2' → 청약 둘째날 마감 임박 리마인더 (메시지 헤더만 다름)
    데이터 부족이면 누락 항목 안내 메시지로 분기.
    """
    name = _esc(detail.name)
    no = _esc(detail.no)
    detail_url = DETAIL_URL_FMT.format(no=detail.no)

    # 단계별 헤더 라벨
    if phase == PHASE_DAY2:
        phase_header = "⏰ <b>오늘 청약 마감! (오후 알림)</b>\n\n"
    elif phase == PHASE_DAY2_MORNING:
        phase_header = "🌅 <b>오늘 청약 마감일! (오전 알림)</b>\n\n"
    else:
        phase_header = "🔔 <b>오늘 청약 첫날!</b>\n\n"

    # 청약일 표시
    if detail.subscribe_start and detail.subscribe_end:
        if detail.subscribe_start == detail.subscribe_end:
            sub_period = detail.subscribe_start.strftime("%Y-%m-%d")
        else:
            sub_period = (
                f"{detail.subscribe_start.strftime('%Y-%m-%d')} ~ "
                f"{detail.subscribe_end.strftime('%Y-%m-%d')}"
            )
    else:
        sub_period = "-"

    underwriter = _esc(detail.underwriter or "-")

    # 데이터 부족 → 간단 안내
    if result.insufficient:
        missing = ", ".join(result.missing_fields) if result.missing_fields else "-"
        return (
            f"{phase_header}"
            f"{result.emoji} <b>[{name}] 데이터 부족</b>\n"
            f"\n"
            f"청약일: {_esc(sub_period)}\n"
            f"주관사: {underwriter}\n"
            f"누락 지표: {_esc(missing)}\n"
            f"\n"
            f'<a href="{_esc(detail_url)}">38커뮤 상세 보기</a>'
        )

    # 정상 등급
    comp = f"{detail.competition_ratio:,.2f}:1" if detail.competition_ratio is not None else "-"
    lockup = f"{detail.lockup_ratio:.2f}%" if detail.lockup_ratio is not None else "-"
    floatr = f"{detail.float_ratio:.2f}%" if detail.float_ratio is not None else "-"
    band_line = _fmt_band(detail)

    bd = result.breakdown
    margin_section = _margin_loan_section(detail)

    # 1주당 예상 수익: 등급별 과거 평균 시초가 수익률 × 공모가
    stat = GRADE_HISTORICAL_RETURNS.get(result.grade)
    if stat and detail.final_price:
        avg_pct = stat["avg_pct"]
        gain_per_share = int(round(detail.final_price * avg_pct / 100))
        expected_line = (
            f"📈 <b>1주당 예상 수익: +{gain_per_share:,}원</b> "
            f"({result.grade} 평균 +{avg_pct:.0f}%)\n\n"
        )
    else:
        expected_line = ""

    # 상장일 (있을 때만)
    listing_line = ""
    if detail.listing_date:
        weekday = ["월","화","수","목","금","토","일"][detail.listing_date.weekday()]
        listing_line = (
            f"🎯 상장일: {detail.listing_date.strftime('%Y-%m-%d')}({weekday})\n"
        )

    return (
        f"{phase_header}"
        f"{result.emoji} <b>[{name}] {result.grade}</b>  "
        f"(<b>{result.total_score}/12</b>점)\n"
        f"\n"
        f"📅 청약일: {_esc(sub_period)}\n"
        f"{listing_line}"
        f"🏦 주관사: {underwriter}\n"
        f"\n"
        f"<b>📊 4대 지표</b>\n"
        f"• 기관 경쟁률: <b>{_esc(comp)}</b>  ({bd['competition']}/3)\n"
        f"• 의무보유확약: <b>{_esc(lockup)}</b>  ({bd['lockup']}/6)\n"
        f"• 유통가능물량: <b>{_esc(floatr)}</b>  ({bd['float']}/3)\n"
        f"• 공모가: {_esc(band_line)}  ({bd['price']}/3)\n"
        f"\n"
        f"{expected_line}"
        f"{margin_section}\n"
        f"\n"
        f'🔗 <a href="{_esc(detail_url)}">38커뮤 상세 (no={no})</a>'
    )


# ---------------------------------------------------------------------------
# 월간 다이제스트 (매월 1일 발송)
# ---------------------------------------------------------------------------
_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _fmt_period(s: IpoSchedule) -> str:
    """청약기간을 'M/D(요일) ~ M/D' 형식으로."""
    if s.subscribe_start is None:
        return "-"
    start = s.subscribe_start
    start_str = f"{start.month}/{start.day}({_WEEKDAY_KR[start.weekday()]})"
    if s.subscribe_end and s.subscribe_end != start:
        end = s.subscribe_end
        return f"{start_str} ~ {end.month}/{end.day}"
    return start_str


def build_monthly_digest_message(items: list[IpoSchedule], year: int, month: int) -> str:
    """
    매월 1일 보내는 그 달의 청약 예정 종목 다이제스트.
    수요예측 결과가 아직 없는 종목이 많을 시점이라 등급은 매기지 않고
    종목명/청약일/주관사만 한 번에 묶어 발송한다.
    """
    header = f"📅 <b>{year}년 {month}월 청약 예정 공모주</b>\n총 <b>{len(items)}</b>개\n\n"

    if not items:
        body = "이번 달 청약 예정 종목이 38커뮤 일정에 잡혀있지 않습니다."
    else:
        # 청약 시작일 빠른 순으로 정렬 (None은 뒤로)
        sorted_items = sorted(
            items,
            key=lambda s: (s.subscribe_start is None, s.subscribe_start or date.max),
        )
        lines = []
        for s in sorted_items:
            url = DETAIL_URL_FMT.format(no=s.no)
            period = _fmt_period(s)
            uw = f" — {_esc(s.underwriter)}" if s.underwriter else ""
            lines.append(
                f'• <a href="{_esc(url)}">{_esc(s.name)}</a>'
                f"  {_esc(period)}{uw}"
            )
        body = "\n".join(lines)

    footer = (
        f"\n\n청약 첫날 08:30, 둘째날 15:00에 종목별 알림이 갑니다."
        f'\n🔗 <a href="{_esc(SCHEDULE_URL)}">38커뮤 청약일정</a>'
    )
    return header + body + footer


def build_refund_message(detail: IpoDetail) -> str:
    """
    청약 환불일 오전 알림.
    마통으로 청약했다면 환불금 받자마자 마통으로 즉시 갚으라고 안내.
    """
    name = _esc(detail.name)
    no = _esc(detail.no)
    detail_url = DETAIL_URL_FMT.format(no=detail.no)

    # 청약기간
    if detail.subscribe_start and detail.subscribe_end:
        if detail.subscribe_start == detail.subscribe_end:
            sub_period = detail.subscribe_start.strftime("%Y-%m-%d")
        else:
            sub_period = (
                f"{detail.subscribe_start.strftime('%Y-%m-%d')} ~ "
                f"{detail.subscribe_end.strftime('%Y-%m-%d')}"
            )
    else:
        sub_period = "-"

    refund_str = "-"
    if detail.refund_date:
        wd = ["월","화","수","목","금","토","일"][detail.refund_date.weekday()]
        refund_str = f"{detail.refund_date.strftime('%Y-%m-%d')}({wd})"

    underwriter = _esc(detail.underwriter or "-")
    final_price = (f"{detail.final_price:,}원" if detail.final_price is not None else "-")

    # 마통 5천만원 일일 이자 (기본 가정)
    amount = int(MARGIN_LOAN["scenario_amount"])
    rate = MARGIN_LOAN["annual_rate"]
    daily_interest = int(round(amount * rate / 365))

    return (
        f"💸 <b>오늘 청약 환불일!</b>\n\n"
        f"<b>[{name}]</b>\n\n"
        f"📅 청약일: {_esc(sub_period)}\n"
        f"💵 환불일: <b>{_esc(refund_str)}</b>\n"
        f"🏦 증권사: {underwriter}\n"
        f"💰 공모가: {_esc(final_price)}\n\n"
        f"<b>⚠️ 마통 사용 시 즉시 이체 권장</b>\n"
        f"• 마통 {amount//10_000_000}천만원 기준 하루 이자 ≈ <b>{daily_interest:,}원</b>\n"
        f"• 환불금 입금 확인 후 바로 마통 계좌로 송금하세요\n\n"
        f'🔗 <a href="{_esc(detail_url)}">38커뮤 상세 (no={no})</a>'
    )


def build_listing_message(detail: IpoDetail, result, phase: str = PHASE_LISTING_EVE) -> str:
    """
    상장 알림 메시지 (전날 15:00 또는 당일 08:30).
    Day1/Day2 알림과 별개 — 청약은 이미 끝났고 상장 임박 안내.
    증권사(주관사), 공모가, 등급, 등급별 평균 시초가 수익률 포함.
    """
    name = _esc(detail.name)
    no = _esc(detail.no)
    detail_url = DETAIL_URL_FMT.format(no=detail.no)

    if phase == PHASE_LISTING_DAY:
        phase_header = "🎉 <b>오늘 상장!</b>\n\n"
    else:
        phase_header = "📢 <b>내일 상장 (D-1)</b>\n\n"

    # 상장일
    listing_str = "-"
    if detail.listing_date:
        weekday = ["월","화","수","목","금","토","일"][detail.listing_date.weekday()]
        listing_str = f"{detail.listing_date.strftime('%Y-%m-%d')}({weekday})"

    # 공모가
    final_price = (
        f"{detail.final_price:,}원" if detail.final_price is not None
        else f"{detail.band_low:,}~{detail.band_high:,}원" if detail.band_low and detail.band_high
        else "-"
    )

    underwriter = _esc(detail.underwriter or "-")

    # 등급별 예상 시초가
    expected_line = ""
    if not result.insufficient:
        stat = GRADE_HISTORICAL_RETURNS.get(result.grade)
        if stat and detail.final_price:
            avg_pct = stat["avg_pct"]
            gain = int(round(detail.final_price * avg_pct / 100))
            target_price = detail.final_price + gain
            expected_line = (
                f"📈 <b>예상 시초가</b>: 약 {target_price:,}원 "
                f"(+{gain:,}원)\n"
                f"   ({result.grade} 평균 시초가 +{avg_pct:.0f}%, "
                f"과거 {int(stat['n'])}개 기준)\n"
            )

    grade_line = ""
    if not result.insufficient:
        grade_line = f"{result.emoji} <b>등급: {result.grade}</b> ({result.total_score}/12)\n"

    return (
        f"{phase_header}"
        f"<b>[{name}]</b>\n\n"
        f"🎯 상장일: {_esc(listing_str)}\n"
        f"🏦 증권사: <b>{underwriter}</b>\n"
        f"💰 공모가: <b>{_esc(final_price)}</b>\n"
        f"{grade_line}"
        f"\n"
        f"{expected_line}"
        f'\n🔗 <a href="{_esc(detail_url)}">38커뮤 상세 (no={no})</a>'
    )


def send_telegram(text: str,
                  bot_token: Optional[str] = None,
                  chat_id: Optional[str] = None,
                  disable_preview: bool = True) -> bool:
    """
    Telegram sendMessage 호출. 성공 True, 실패 False.
    예외는 로그만 남기고 호출부 흐름은 끊지 않는다.
    """
    token = bot_token or TELEGRAM_BOT_TOKEN
    chat = chat_id or TELEGRAM_CHAT_ID

    if not token or not chat:
        log.error("텔레그램 토큰/챗ID 미설정 - 메시지 발송 스킵")
        return False

    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code != 200:
            log.error("텔레그램 발송 실패 status=%s body=%s", resp.status_code, resp.text[:300])
            return False
        body = resp.json()
        if not body.get("ok"):
            log.error("텔레그램 응답 ok=false body=%s", body)
            return False
        return True
    except requests.RequestException as e:
        log.error("텔레그램 요청 예외: %s", e)
        return False


def notify(detail: IpoDetail, result: GradeResult, phase: str = PHASE_DAY1) -> bool:
    """편의 함수: 종목별 메시지 빌드 + 전송."""
    msg = build_message(detail, result, phase=phase)
    ok = send_telegram(msg)
    if ok:
        log.info("[%s/%s/%s] 텔레그램 발송 OK (%s/%s점)",
                 detail.no, detail.name, phase, result.grade, result.total_score)
    return ok


def notify_monthly_digest(items: list[IpoSchedule], year: int, month: int) -> bool:
    """월간 다이제스트 1통 발송."""
    msg = build_monthly_digest_message(items, year, month)
    ok = send_telegram(msg)
    if ok:
        log.info("월간 다이제스트 발송 OK (%d-%02d, %d종목)", year, month, len(items))
    return ok


def notify_listing(detail: IpoDetail, result, phase: str = PHASE_LISTING_EVE) -> bool:
    """상장 전날/당일 알림 발송."""
    msg = build_listing_message(detail, result, phase=phase)
    ok = send_telegram(msg)
    if ok:
        log.info("[%s/%s/%s] 상장 알림 발송 OK", detail.no, detail.name, phase)
    return ok


def notify_refund(detail: IpoDetail) -> bool:
    """환불일 알림 발송."""
    msg = build_refund_message(detail)
    ok = send_telegram(msg)
    if ok:
        log.info("[%s/%s/refund] 환불 알림 발송 OK", detail.no, detail.name)
    return ok


# ---------------------------------------------------------------------------
# 간단 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import datetime as _dt

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 메시지 빌드만 점검 (실제 발송은 .env 셋팅된 경우에만)
    fake_detail = IpoDetail(
        no="9999",
        name="테스트공모주",
        competition_ratio=1623.45,
        lockup_ratio=42.5,
        float_ratio=22.3,
        price_position="upper",
        band_low=10000,
        band_high=12000,
        final_price=12000,
        underwriter="미래에셋증권",
        subscribe_start=_dt.date(2026, 5, 4),
        subscribe_end=_dt.date(2026, 5, 5),
    )
    fake_result = GradeResult(
        total_score=10,
        grade="풀비례",
        emoji="🔥",
        breakdown={"competition": 3, "lockup": 2, "float": 2, "price": 2},
    )

    print("=== Day1 메시지 미리보기 ===")
    print(build_message(fake_detail, fake_result, phase=PHASE_DAY1))

    print("\n=== Day2 리마인더 미리보기 ===")
    print(build_message(fake_detail, fake_result, phase=PHASE_DAY2))

    print("\n=== 월간 다이제스트 미리보기 ===")
    fake_items = [
        IpoSchedule(no="1001", name="공모주A",
                    subscribe_start=_dt.date(2026, 5, 4),
                    subscribe_end=_dt.date(2026, 5, 5),
                    underwriter="미래에셋증권"),
        IpoSchedule(no="1002", name="공모주B",
                    subscribe_start=_dt.date(2026, 5, 12),
                    subscribe_end=_dt.date(2026, 5, 13),
                    underwriter="한국투자증권"),
        IpoSchedule(no="1003", name="공모주C",
                    subscribe_start=_dt.date(2026, 5, 20),
                    subscribe_end=_dt.date(2026, 5, 21),
                    underwriter=""),
    ]
    print(build_monthly_digest_message(fake_items, 2026, 5))

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("\n실제 텔레그램으로 발송 시도...")
        ok = send_telegram(build_message(fake_detail, fake_result, phase=PHASE_DAY1))
        print("발송 결과:", ok)
    else:
        print("\n.env 미설정 → 실제 발송은 스킵")
