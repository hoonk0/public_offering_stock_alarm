"""
텔레그램 봇 명령어 폴링.

사용자가 봇에 보내는 메시지를 long polling으로 받아 처리한다.

지원 명령어:
    /5, /5월, /05      → 해당 월(현재 연도)의 청약 예정 종목 다이제스트
    /이번달            → 이번 달
    /다음달            → 다음 달
    /start, /help      → 사용법 안내

보안:
    TELEGRAM_CHAT_ID와 일치하는 사용자에게만 응답.
    다른 사람이 봇을 찾아 메시지를 보내도 무시 (로그만 남김).

월간 다이제스트 호출은 DB(monthly_digests)를 건드리지 않는다.
이건 사용자가 직접 요청하는 on-demand 조회이고,
매월 1일 자동 발송과는 별개이기 때문.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import date, timedelta
from typing import Optional

import requests

from config import (
    DETAIL_URL_FMT,
    GRADE_HISTORICAL_RETURNS,
    HTTP_HEADERS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from crawler import (
    ListedStock,
    fetch_detail,
    fetch_listed_stocks,
    fetch_schedule_list,
)
from db import get_cached_grade, init_db, set_cached_grade
from grader import grade as compute_grade
from notifier import build_monthly_digest_message, send_telegram

log = logging.getLogger(__name__)

GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"

# 명령어 패턴
MONTH_CMD_RE = re.compile(r"^/(\d{1,2})(?:월)?$")
MONTH_RETURN_RE = re.compile(r"^/(\d{1,2})월?수익률$")     # /5월수익률, /5수익률
THIS_MONTH_ALIASES = {"/이번달", "/this", "/now"}
NEXT_MONTH_ALIASES = {"/다음달", "/next"}
THIS_WEEK_ALIASES = {"/이번주", "/이주", "/thisweek"}
NEXT_WEEK_ALIASES = {"/다음주", "/nextweek"}
HELP_ALIASES = {"/start", "/help", "/도움말"}

# 등급별 수익률 명령 (/1등급 ~ /5등급)
GRADE_RETURN_CMDS = {
    "/1등급": "1등급",
    "/2등급": "2등급",
    "/3등급": "3등급",
    "/4등급": "4등급",
    "/5등급": "5등급",
    # 영문 별칭
    "/grade1": "1등급",
    "/grade2": "2등급",
    "/grade3": "3등급",
    "/grade4": "4등급",
    "/grade5": "5등급",
}

# 지원 연도 (수익률 조회 기본). today.year로 동적으로 잡기.
import datetime as _dt
DEFAULT_RETURN_YEAR = _dt.date.today().year

HELP_TEXT = (
    "🤖 <b>공모주 알림봇</b>\n\n"
    "<b>📅 청약 예정 조회</b>\n"
    "• <code>/이번주</code> — 이번 주 청약 종목 + 등급 + 예상 수익률\n"
    "• <code>/다음주</code> — 다음 주 청약 종목\n"
    "• <code>/5</code> 또는 <code>/5월</code> — 5월 청약 예정 종목\n"
    "• <code>/이번달</code> — 이번 달 청약 예정 종목\n"
    "• <code>/다음달</code> — 다음 달 청약 예정 종목\n\n"
    "<b>📈 첫날 수익률 조회 (올해 상장)</b>\n"
    "• <code>/3월수익률</code> — 3월에 상장한 종목들의 첫날 수익률\n"
    "• <code>/1등급</code> 💎 (최상위, 평균 +247%) — 거의 따상 확실\n"
    "• <code>/2등급</code> 🔥 (평균 +201%) — 풀비례 청약 강추\n"
    "• <code>/3등급</code> ✅ (평균 +87%) — 비례 청약\n"
    "• <code>/4등급</code> ⚖️ (평균 +49%) — 균등배정만\n"
    "• <code>/5등급</code> ❌ (평균 +12%) — 청약 비추천\n\n"
    "• <code>/help</code> — 이 도움말\n\n"
    "<b>🔔 자동 알림</b>\n"
    "• 매월 1일 09:00 — 그 달의 청약 예정 종목 안내\n"
    "• 청약 첫날 08:30 — 종목별 4대 지표 + 등급\n"
    "• 청약 둘째날 15:00 — 마감 임박 리마인더"
)


# ---------------------------------------------------------------------------
# 명령어 → 응답 메시지
# ---------------------------------------------------------------------------
def _digest_for_month(year: int, month: int) -> str:
    """해당 연-월의 청약 종목 다이제스트 메시지 생성 (DB 기록 없음)."""
    schedules = fetch_schedule_list()
    last_day = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, last_day)
    items = [
        s for s in schedules
        if s.subscribe_start is not None and start <= s.subscribe_start <= end
    ]
    return build_monthly_digest_message(items, year, month)


def _next_year_month(today: date) -> tuple[int, int]:
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def _week_range(today: date, offset_weeks: int = 0) -> tuple[date, date]:
    """
    '이번주(offset=0)' = 오늘 ~ 다가오는 일요일.
    '다음주(offset=1)' = 다음 월요일 ~ 그 일요일.
    이미 마감된 청약을 안 보여주려고 today부터 시작.
    """
    if offset_weeks == 0:
        sunday = today + timedelta(days=(6 - today.weekday()))
        return today, sunday
    monday = today + timedelta(days=(7 - today.weekday())) + timedelta(weeks=offset_weeks - 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _grade_emoji(grade: str) -> str:
    return {"1등급": "💎", "2등급": "🔥", "3등급": "✅", "4등급": "⚖️", "5등급": "❌"}.get(grade, "❓")


def _expected_return_line(grade: str, offering_price: Optional[int]) -> str:
    """
    등급의 과거 평균 시초가 수익률 + 1주당 예상 수익액.
    데이터 부족 등급은 빈 문자열.
    """
    stat = GRADE_HISTORICAL_RETURNS.get(grade)
    if not stat:
        return ""
    avg = stat["avg_pct"]
    n = int(stat["n"])
    if offering_price:
        gain = int(round(offering_price * avg / 100))
        target = offering_price + gain
        return (
            f"📈 <b>{grade} 평균 시초가 +{avg:.1f}%</b> "
            f"(과거 {n}개 기준)\n"
            f"   → 1주당 예상 +{gain:,}원 "
            f"(공모가 {offering_price:,} → 약 {target:,})"
        )
    return f"📈 <b>{grade} 평균 시초가 +{avg:.1f}%</b> (과거 {n}개 기준)"


def _build_weekly_message(start: date, end: date, label: str) -> str:
    """
    start~end (월~일) 사이 청약 시작일 종목 → 4대 지표 + 등급 + 예상 수익.
    수요예측 미공개 종목은 등급 산정 안 되고 데이터부족 표시.
    """
    schedules = fetch_schedule_list()
    # 청약 기간이 [start, end] 범위와 겹치는 종목 (마감 안 지난 것만)
    items = [
        s for s in schedules
        if s.subscribe_start is not None and s.subscribe_end is not None
        and s.subscribe_start <= end and s.subscribe_end >= start
    ]
    items.sort(key=lambda s: s.subscribe_start or date.max)

    period_str = f"{start.month}/{start.day}({_WEEKDAY_KR[start.weekday()]}) ~ {end.month}/{end.day}"
    header = f"📅 <b>{label} 공모주 청약</b>\n기간: {_esc_simple(period_str)}\n총 <b>{len(items)}</b>개"

    if not items:
        suggestion = "\n\n청약 예정 종목이 없습니다."
        if label == "이번주":
            suggestion += " <code>/다음주</code>를 확인해보세요."
        return header + suggestion

    blocks: list[str] = []
    for s in items:
        try:
            d = fetch_detail(s.no, s.name, base=s)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] detail 실패: %s", s.no, e)
            blocks.append(f"❓ <b>{_esc_simple(s.name)}</b>  (상세 페이지 로딩 실패)")
            continue

        result = compute_grade(d)
        emoji = _grade_emoji(result.grade) if not result.insufficient else "❓"

        url = DETAIL_URL_FMT.format(no=s.no)
        name_link = f'<a href="{url}">{_esc_simple(s.name)}</a>'

        period = _esc_simple(
            f"{s.subscribe_start.month}/{s.subscribe_start.day}"
            f"({_WEEKDAY_KR[s.subscribe_start.weekday()]})"
            + (f" ~ {s.subscribe_end.month}/{s.subscribe_end.day}"
               if s.subscribe_end and s.subscribe_end != s.subscribe_start else "")
        )
        underwriter = _esc_simple(s.underwriter or "-")

        if result.insufficient:
            block = (
                f"❓ {name_link}  <b>(데이터 부족: {_esc_simple(', '.join(result.missing_fields))})</b>\n"
                f"   📅 {period} · 🏦 {underwriter}\n"
                f"   수요예측 결과 발표 후 등급 산정 가능"
            )
        else:
            comp = f"{d.competition_ratio:,.0f}:1" if d.competition_ratio is not None else "-"
            lock = f"{d.lockup_ratio:.0f}%" if d.lockup_ratio is not None else "-"
            pos_kr = {
                "above_band": "상단초과", "upper": "상단",
                "middle": "중간", "lower": "하단", "below_band": "하단미만",
            }.get(d.price_position or "", "-")
            final_price = (f"{d.final_price:,}원" if d.final_price is not None
                           else f"{d.band_low:,}~{d.band_high:,}원" if d.band_low and d.band_high
                           else "-")

            block = (
                f"{emoji} {name_link}  <b>{result.grade} ({result.total_score}/12)</b>\n"
                f"   📅 {period} · 🏦 {underwriter} · 💰 공모가 {_esc_simple(final_price)}\n"
                f"   📊 경쟁률 {_esc_simple(comp)} · 의무보유 {_esc_simple(lock)} · 공모가 {pos_kr}\n"
                f"   {_expected_return_line(result.grade, d.final_price)}"
            )
        blocks.append(block)

    body = "\n\n".join(blocks)
    footer = (
        "\n\n💡 <i>예상 수익률은 2025-2026 상장 119종목의 등급별 과거 시초가 평균.\n"
        "실제 수익률은 시장 상황에 따라 다를 수 있습니다.</i>"
    )
    return f"{header}\n\n{body}{footer}"


def _esc_simple(text: object) -> str:
    """텔레그램 HTML 이스케이프."""
    import html as _html
    if text is None:
        return "-"
    return _html.escape(str(text), quote=False)


def _format_listed_row(s: ListedStock) -> str:
    """
    수익률 메시지의 한 줄.
    한국 IPO는 시초가 매도가 평균적으로 종가보다 유리(2026년 17종목 중 11종목 장중 하락).
    그래서 시초가 수익률을 메인으로 표시하고 종가 수익률을 괄호 안에 보조 표시.
        '• 03/06 에스팀  +260.6% (종가 +300.0%)  10/12'
    """
    op = s.open_return_pct
    cp = s.return_pct
    if op is not None:
        main = f"{op:+.1f}%"
    elif cp is not None:
        main = f"{cp:+.1f}%"
    else:
        main = "—"
    extra = f" (종가 {cp:+.1f}%)" if cp is not None and op is not None else ""
    listing = (s.listing_date.strftime("%m/%d") if s.listing_date else "??")
    url = DETAIL_URL_FMT.format(no=s.no)
    name_link = f'<a href="{url}">{s.name}</a>'
    return f"• {listing}  {name_link}  <b>{main}</b>{extra}"


def _summary_line(stocks: list[ListedStock]) -> str:
    """시초가/종가 통계 한 줄 요약."""
    open_returns = [s.open_return_pct for s in stocks if s.open_return_pct is not None]
    close_returns = [s.return_pct for s in stocks if s.return_pct is not None]
    if not open_returns:
        return ""

    def stats(rs: list[float]) -> tuple[float, float]:
        avg = sum(rs) / len(rs)
        sr = sorted(rs)
        mid = sr[len(sr)//2] if len(sr) % 2 else (sr[len(sr)//2 - 1] + sr[len(sr)//2]) / 2
        return avg, mid

    o_avg, o_mid = stats(open_returns)
    c_avg, c_mid = stats(close_returns) if close_returns else (0.0, 0.0)
    return (
        f"📊 <b>시초가 매도</b>: 평균 <b>{o_avg:+.1f}%</b>  "
        f"중앙 {o_mid:+.1f}%  "
        f"최고 {max(open_returns):+.1f}%  최저 {min(open_returns):+.1f}%\n"
        f"📊 종가 보유:    평균 {c_avg:+.1f}%  중앙 {c_mid:+.1f}%"
    )


def _build_month_returns_message(year: int, month: int) -> str:
    """N월 상장 종목들의 첫날 수익률 메시지."""
    stocks = fetch_listed_stocks(year)
    items = [
        s for s in stocks
        if s.listing_date and s.listing_date.year == year and s.listing_date.month == month
        and s.close_price is not None and s.offering_price
    ]
    items.sort(key=lambda s: s.listing_date or date.min)

    header = f"📈 <b>{year}년 {month}월 상장 종목 첫날 수익률</b>\n총 <b>{len(items)}</b>개\n"

    if not items:
        return header + f"\n{year}년 {month}월에 상장된 종목이 없습니다 (또는 아직 미상장)."

    body = "\n".join(_format_listed_row(s) for s in items)
    summary = _summary_line(items)
    return f"{header}\n{summary}\n\n{body}"


def _ensure_grade_for(no: str, name: str) -> Optional[tuple[str, int, bool]]:
    """
    종목의 등급을 반환 (캐시에 있으면 사용, 없으면 detail 파싱 후 캐싱).
    실패 시 None.
    """
    cached = get_cached_grade(no)
    if cached is not None:
        return cached
    try:
        detail = fetch_detail(no, name)
    except Exception as e:  # noqa: BLE001
        log.exception("[%s/%s] grade detail fetch 실패: %s", no, name, e)
        return None
    result = compute_grade(detail)
    set_cached_grade(no, name, result.grade, result.total_score, result.insufficient)
    return result.grade, result.total_score, result.insufficient


def _build_grade_returns_message(target_grade: str, year: int) -> str:
    """등급별 첫날 수익률 메시지. 미등록 종목은 detail 파싱으로 채움."""
    init_db()  # 캐시 테이블 보장
    stocks = fetch_listed_stocks(year)
    listed = [s for s in stocks if s.close_price is not None and s.offering_price]
    log.info("등급별 수익률(%s) — 후보 %d종목", target_grade, len(listed))

    matched: list[tuple[ListedStock, int]] = []
    for s in listed:
        info = _ensure_grade_for(s.no, s.name)
        if info is None:
            continue
        gname, gscore, insufficient = info
        if insufficient:
            continue
        if gname == target_grade:
            matched.append((s, gscore))

    matched.sort(key=lambda t: (t[0].listing_date or date.min))

    emoji_for = {"1등급": "💎", "2등급": "🔥", "3등급": "✅", "4등급": "⚖️", "5등급": "❌"}
    emoji = emoji_for.get(target_grade, "")
    header = (
        f"{emoji} <b>{year}년 {target_grade} 종목 첫날 수익률</b>\n"
        f"총 <b>{len(matched)}</b>개"
    )

    if not matched:
        return header + (
            f"\n\n{year}년 상장 종목 중 {target_grade}으로 분류된 종목이 없습니다.\n"
            f"(상장 후 수요예측 결과가 사라졌거나 데이터 부족으로 등급 산정이 안 된 종목 제외)"
        )

    body = "\n".join(
        _format_listed_row(s) + f"  <i>{score}/12</i>"
        for s, score in matched
    )
    summary = _summary_line([s for s, _ in matched])
    return f"{header}\n{summary}\n\n{body}"


def handle_text(text: str, today: Optional[date] = None) -> Optional[str]:
    """
    수신 텍스트 → 응답 메시지(HTML).
    인식 못 한 명령어는 None 반환 (응답 안 함).
    """
    if today is None:
        today = date.today()
    text = text.strip()

    if text in HELP_ALIASES:
        return HELP_TEXT

    if text in THIS_MONTH_ALIASES:
        return _digest_for_month(today.year, today.month)

    if text in NEXT_MONTH_ALIASES:
        ny, nm = _next_year_month(today)
        return _digest_for_month(ny, nm)

    if text in THIS_WEEK_ALIASES:
        start, end = _week_range(today, 0)
        return _build_weekly_message(start, end, "이번주")

    if text in NEXT_WEEK_ALIASES:
        start, end = _week_range(today, 1)
        return _build_weekly_message(start, end, "다음주")

    # 등급별 수익률
    if text in GRADE_RETURN_CMDS:
        return _build_grade_returns_message(GRADE_RETURN_CMDS[text], today.year)

    # 월별 수익률 (/N월수익률)
    m = MONTH_RETURN_RE.match(text)
    if m:
        month = int(m.group(1))
        if not (1 <= month <= 12):
            return f"⚠️ 월은 1~12 사이여야 합니다."
        return _build_month_returns_message(today.year, month)

    # 청약 예정 (/N, /N월)
    m = MONTH_CMD_RE.match(text)
    if m:
        month = int(m.group(1))
        if not (1 <= month <= 12):
            return f"⚠️ 월은 1~12 사이 숫자여야 합니다 (입력: <code>{m.group(1)}</code>)"
        return _digest_for_month(today.year, month)

    if text.startswith("/"):
        return f"⚠️ 모르는 명령어입니다.\n\n{HELP_TEXT}"

    return None


# ---------------------------------------------------------------------------
# 폴링 루프
# ---------------------------------------------------------------------------
def _get_updates(offset: Optional[int], timeout: int = 25) -> list[dict]:
    """Telegram getUpdates long-polling 호출."""
    url = GET_UPDATES_URL.format(token=TELEGRAM_BOT_TOKEN)
    params: dict[str, int] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    # long polling이라 HTTP timeout은 telegram timeout보다 약간 길게
    resp = requests.get(url, params=params, headers=HTTP_HEADERS,
                        timeout=timeout + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates ok=false: {data}")
    return data.get("result", [])


def run_polling(stop_check: Optional[callable] = None) -> None:
    """
    봇 메시지 폴링 루프 (블로킹).
    Ctrl+C 또는 stop_check()가 True 반환 시 종료.
    """
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN 미설정 → 폴링 시작 불가")
        return

    chat_filter = str(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None
    if not chat_filter:
        log.warning("TELEGRAM_CHAT_ID 미설정 → 모든 사용자에게 응답함 (보안 주의)")

    log.info("텔레그램 봇 폴링 시작 (chat_filter=%s)", chat_filter or "OFF")
    offset: Optional[int] = None

    while True:
        if stop_check is not None and stop_check():
            log.info("stop_check 신호 → 폴링 종료")
            return
        try:
            updates = _get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                _process_update(update, chat_filter)
        except requests.RequestException as e:
            log.error("폴링 요청 예외: %s — 5초 후 재시도", e)
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt → 폴링 종료")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("폴링 루프 예외: %s — 5초 후 재시도", e)
            time.sleep(5)


def _process_update(update: dict, chat_filter: Optional[str]) -> None:
    """update 1개 처리: 권한 체크 → 명령어 해석 → 응답."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = msg.get("text", "") or ""

    # 권한 필터
    if chat_filter and chat_id != chat_filter:
        log.warning("권한 없는 챗(%s)에서 메시지 수신 — 무시: %r", chat_id, text[:50])
        return

    if not text:
        return

    log.info("[수신/%s] %s", chat_id, text)

    # 등급별 수익률은 detail 파싱이 많아 시간이 오래 걸림(첫 조회 시 ~30초).
    # 캐시 적중률 모를 때 ack 메시지 먼저 보내 사용자가 응답을 기다리는 동안 안 답답하게.
    if text.strip() in GRADE_RETURN_CMDS:
        send_telegram(
            f"⏳ {GRADE_RETURN_CMDS[text.strip()]} 종목 분석 중입니다… "
            "처음 조회는 30초~1분 정도 걸릴 수 있어요.",
            chat_id=chat_id,
        )

    try:
        reply = handle_text(text)
    except Exception as e:  # noqa: BLE001
        log.exception("명령어 처리 실패: %s", e)
        err_str = str(e).lower()
        if "timeout" in err_str or "timed out" in err_str or "connection" in err_str:
            send_telegram(
                "⏳ 38커뮤 응답이 느립니다. 잠시 후 다시 시도해주세요.",
                chat_id=chat_id,
            )
        else:
            send_telegram(f"⚠️ 처리 중 오류: <code>{e}</code>", chat_id=chat_id)
        return

    if reply is None:
        return  # 응답 없음 (일반 텍스트)
    send_telegram(reply, chat_id=chat_id)


# ---------------------------------------------------------------------------
# 간단 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # 명령어 파싱 단위 점검 (네트워크 호출 없는 케이스만)
    today = date(2026, 5, 3)
    cases = ["/start", "/help", "/도움말", "/13", "/abc", "안녕"]
    for c in cases:
        out = handle_text(c, today=today)
        preview = (out[:60] + "...") if out and len(out) > 60 else out
        print(f"  {c:<10} → {preview!r}")

    # 실제 폴링은 .env 셋팅된 경우에만
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("\n실제 폴링 시작 (Ctrl+C로 종료). 텔레그램에서 봇에 /help 보내보세요.")
        run_polling()
    else:
        print("\n.env 미설정 → 실제 폴링 스킵")
