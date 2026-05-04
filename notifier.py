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
    ASSUMED_RETAIL_COMPETITION,
    DETAIL_URL_FMT,
    GRADE_HISTORICAL_RETURNS,
    MARGIN_LOAN,
    MARGIN_LOAN_RECOMMENDATION,
    SCHEDULE_URL,
    TELEGRAM_API_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from crawler import IpoDetail, IpoSchedule
from grader import GradeResult

# 발송 단계 (db.PHASE_DAY1/DAY2와 같은 값을 사용)
PHASE_DAY1 = "day1"
PHASE_DAY2 = "day2"

log = logging.getLogger(__name__)


def _esc(text: object) -> str:
    """HTML 파싱 모드용 이스케이프. None은 빈 문자열."""
    if text is None:
        return "-"
    return html.escape(str(text), quote=False)


def _margin_loan_section(grade: str, offering_price: Optional[int]) -> str:
    """
    마통(마이너스통장) 청약 손익 분석 — 7천만원 마통 시나리오.

    계산 흐름:
      1. 마통 7천만원 → 신청 가능 주식 = 7천만원 / (공모가 × 50%)
      2. 등급별 가정 청약경쟁률로 비례 배정 추정
         + 균등 배정 1주 가정
      3. 배정 주식 × 1주당 평균 차익 = 예상 차익
      4. 마통 5일 이자 차감 = 순익 추정
    """
    amount = int(MARGIN_LOAN["scenario_amount"])
    rate = MARGIN_LOAN["annual_rate"]
    days = int(MARGIN_LOAN["subscription_days"])
    deposit_rate = MARGIN_LOAN["deposit_rate"]

    interest = int(round(amount * rate * days / 365))
    rec, comment = MARGIN_LOAN_RECOMMENDATION.get(grade, ("-", ""))
    stat = GRADE_HISTORICAL_RETURNS.get(grade)

    if not offering_price or not stat:
        return (
            f"<b>🏦 마통 {amount//10_000_000}천만원 시나리오</b> "
            f"(연 {int(rate*100)}%, {days}일)\n"
            f"• 5일 이자: -{interest:,}원\n"
            f"• 권장: {_esc(rec)} — {_esc(comment)}"
        )

    # 1) 신청 가능 주식 (50% 증거금)
    deposit_per_share = max(1, int(round(offering_price * deposit_rate)))
    apply_shares = amount // deposit_per_share

    # 2) 등급별 청약경쟁률 가정 → 비례 배정 + 균등 1주
    competition = ASSUMED_RETAIL_COMPETITION.get(grade, 500)
    proportional = apply_shares // competition
    equal_share = 1  # 균등 배정 1주 가정 (단순화)
    allocated = proportional + equal_share

    # 3) 1주당 차익 + 총 차익
    avg_return_pct = stat["avg_pct"]
    gain_per_share = int(round(offering_price * avg_return_pct / 100))
    total_gain = gain_per_share * allocated

    # 4) 순익
    net = total_gain - interest

    return (
        f"<b>🏦 마통 {amount//10_000_000}천만원 시나리오</b> "
        f"(연 {int(rate*100)}%, {days}일)\n"
        f"• 신청 가능: <b>{apply_shares:,}주</b> "
        f"(공모가 {offering_price:,}원 × 50% 증거금)\n"
        f"• 추정 배정: <b>{allocated:,}주</b> "
        f"(청약경쟁률 {competition:,}:1 가정 + 균등 1주)\n"
        f"• 예상 차익: +{total_gain:,}원 "
        f"(1주당 +{gain_per_share:,}원 × {allocated:,}주)\n"
        f"• 5일 마통 이자: -{interest:,}원\n"
        f"• <b>순익 추정: {'+' if net >= 0 else ''}{net:,}원</b>\n"
        f"• 권장: {_esc(rec)} — {_esc(comment)}"
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
        phase_header = "⏰ <b>오늘 청약 마감!</b>\n\n"
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
    margin_section = _margin_loan_section(result.grade, detail.final_price)
    return (
        f"{phase_header}"
        f"{result.emoji} <b>[{name}] {result.grade}</b>  "
        f"(<b>{result.total_score}/12</b>점)\n"
        f"\n"
        f"📅 청약일: {_esc(sub_period)}\n"
        f"🏦 주관사: {underwriter}\n"
        f"\n"
        f"<b>📊 4대 지표</b>\n"
        f"• 기관 경쟁률: <b>{_esc(comp)}</b>  ({bd['competition']}/3)\n"
        f"• 의무보유확약: <b>{_esc(lockup)}</b>  ({bd['lockup']}/6)\n"
        f"• 유통가능물량: <b>{_esc(floatr)}</b>  ({bd['float']}/3)\n"
        f"• 공모가: {_esc(band_line)}  ({bd['price']}/3)\n"
        f"\n"
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
