"""
38커뮤니케이션 크롤러.

두 종류의 페이지를 다룬다.
1) 청약일정 목록 페이지 → (종목번호, 종목명, 청약시작일, 청약종료일, 주관사) 리스트
2) 종목 상세 페이지 → 4대 지표(경쟁률/의무보유/유통물량/공모가위치) + 부가정보

38커뮤는 HTML 테이블에 라벨/값 쌍으로 데이터를 박아두는 구조라
"라벨 td 텍스트로 찾고 → 같은 행(또는 다음 형제) td에서 값 읽기" 패턴을 쓴다.
인코딩은 EUC-KR이므로 response.encoding 명시 필수.

각 지표 파싱은 개별 try/except로 감싸 한 항목이 깨져도 다른 항목은 살린다.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import requests
import socket
import urllib3
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# IPv4 강제 (라즈베리파이/한국 통신사에서 IPv6 fallback 지연 회피)
# 38커뮤가 IPv6 미지원이라 IPv6 시도 → 실패 → IPv4 retry로 매 요청마다 5~10초 추가 대기.
# 시작 시 한 번만 IPv4 only로 강제하면 즉시 응답.
# ---------------------------------------------------------------------------
urllib3.util.connection.HAS_IPV6 = False

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _orig_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_only_getaddrinfo

from config import (
    DETAIL_URL_FMT,
    HTTP_HEADERS,
    LISTED_URL_FMT,
    REQUEST_DELAY_SEC,
    SCHEDULE_URL,
    SITE_ENCODING,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------
@dataclass
class IpoSchedule:
    """청약일정 목록의 한 줄."""
    no: str                   # 38커뮤 종목번호 (URL의 ?no= 값)
    name: str                 # 종목명
    subscribe_start: Optional[date]  # 청약 시작일
    subscribe_end: Optional[date]    # 청약 종료일
    underwriter: str = ""     # 주관사 (목록에서 보이는 만큼만)


@dataclass
class ListedStock:
    """신규상장 결과 한 줄 (첫날 수익률 포함)."""
    no: str
    name: str
    listing_date: Optional[date]
    offering_price: Optional[int]    # 공모가
    open_price: Optional[int]        # 첫날 시초가
    close_price: Optional[int]       # 첫날 종가
    current_price: Optional[int]     # 현재가 (페이지 시점)

    @property
    def return_pct(self) -> Optional[float]:
        """첫날 종가 기준 수익률 (%) = (종가 - 공모가) / 공모가 * 100"""
        if self.offering_price and self.close_price and self.offering_price > 0:
            return (self.close_price - self.offering_price) / self.offering_price * 100.0
        return None

    @property
    def open_return_pct(self) -> Optional[float]:
        """첫날 시초가 기준 수익률 (%)"""
        if self.offering_price and self.open_price and self.offering_price > 0:
            return (self.open_price - self.offering_price) / self.offering_price * 100.0
        return None


@dataclass
class IpoDetail:
    """종목 상세 페이지에서 파싱한 4대 지표 + 부가정보."""
    no: str
    name: str
    competition_ratio: Optional[float] = None     # 기관 수요예측 경쟁률 (xxxx.xx)
    lockup_ratio: Optional[float] = None          # 의무보유확약 비율 (%)
    float_ratio: Optional[float] = None           # 상장일 유통가능물량 비율 (%)
    price_position: Optional[str] = None          # above_band/upper/middle/lower/below_band
    band_low: Optional[int] = None                # 희망공모가 하단
    band_high: Optional[int] = None               # 희망공모가 상단
    final_price: Optional[int] = None             # 확정공모가
    underwriter: str = ""                         # 주관사
    subscribe_start: Optional[date] = None
    subscribe_end: Optional[date] = None
    raw_errors: list[str] = field(default_factory=list)  # 파싱 실패 항목들


# ---------------------------------------------------------------------------
# HTTP
# 38커뮤가 가끔 응답이 느려 타임아웃이 나는 경우가 있어 retry 정책 + 긴 timeout 사용.
# ---------------------------------------------------------------------------
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """requests.Session 싱글톤 + 자동 retry (네트워크 일시 장애 흡수)."""
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.5,   # 1.5s, 3s, 6s 간격으로 재시도
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
    return _session


def _fetch(url: str) -> BeautifulSoup:
    """38커뮤 페이지를 받아 BeautifulSoup으로 반환. EUC-KR 인코딩 처리."""
    log.debug("GET %s", url)
    session = _get_session()
    # connect 10s, read 30s — 38커뮤 응답 느린 경우 대비
    resp = session.get(url, headers=HTTP_HEADERS, timeout=(10, 30))
    resp.encoding = SITE_ENCODING
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SEC)
    return BeautifulSoup(resp.text, "lxml")


# ---------------------------------------------------------------------------
# 헬퍼: 라벨 td 옆의 값 td 찾기
# ---------------------------------------------------------------------------
def _find_value_by_label(soup: BeautifulSoup, label_pattern: str) -> Optional[str]:
    """
    라벨 텍스트(정규식)에 일치하는 짧은 td/th를 찾고,
    같은 행에서 비어있지 않은 다음 td의 텍스트를 반환.

    38커뮤 새 페이지(2026~)는 "기관경쟁률1486.66:1의무보유확약67.24%" 같이
    여러 라벨/값이 한 td에 합쳐진 셀이 동시에 존재한다.
    그런 셀을 잡으면 next sibling이 비어 있어 None이 나오므로:
      - 라벨 셀 텍스트 길이가 너무 길면(>20) 스킵
      - next sibling이 비어 있으면 다음 후보 라벨 셀로 계속 탐색
    """
    pattern = re.compile(label_pattern)
    for tag in soup.find_all(["td", "th"]):
        text = tag.get_text(strip=True)
        if not text or len(text) > 20:
            continue
        if not pattern.search(text):
            continue
        # 같은 tr 안에서 비어있지 않은 다음 sibling td
        for sib in tag.find_next_siblings(["td", "th"]):
            sib_text = sib.get_text(" ", strip=True)
            if sib_text:
                return sib_text
        # next sibling이 모두 비어있으면 다른 라벨 후보 계속 탐색
    return None


def _parse_date_kr(text: str) -> Optional[date]:
    """'2026.05.07' / '2026-05-07' / '2026.05.07 ~ 2026.05.08' 같은 문자열에서 날짜 하나 뽑기."""
    if not text:
        return None
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_date_range_kr(text: str) -> tuple[Optional[date], Optional[date]]:
    """'2026.05.07 ~ 2026.05.08' → (시작, 종료). 한쪽만 있으면 같은 날 반환."""
    if not text:
        return None, None
    parts = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not parts:
        return None, None
    dates: list[date] = []
    for y, mo, d in parts:
        try:
            dates.append(date(int(y), int(mo), int(d)))
        except ValueError:
            pass
    if not dates:
        return None, None
    if len(dates) == 1:
        return dates[0], dates[0]
    return dates[0], dates[1]


# ---------------------------------------------------------------------------
# 1) 청약일정 목록 파싱
# ---------------------------------------------------------------------------
def _parse_subscribe_period(text: str) -> tuple[Optional[date], Optional[date]]:
    """
    38커뮤 청약일 셀을 파싱.
    - '2026.06.17~06.18'  (같은 달이면 종료일은 MM.DD 약식)
    - '2026.06.30~07.01'  (월 넘어가면 MM.DD)
    - '2026.06.17 ~ 2026.06.18'
    - '2026.06.17'         (단일일)
    """
    if not text:
        return None, None
    text = text.strip()

    # 두 날짜 모두 풀 포맷
    fulls = re.findall(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", text)
    if len(fulls) >= 2:
        try:
            start = date(int(fulls[0][0]), int(fulls[0][1]), int(fulls[0][2]))
            end = date(int(fulls[1][0]), int(fulls[1][1]), int(fulls[1][2]))
            return start, end
        except ValueError:
            return None, None

    # 시작 날짜만 풀 포맷, 종료는 MM.DD 약식
    m = re.match(
        r"\s*(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\s*~\s*(\d{1,2})[./-](\d{1,2})\s*",
        text,
    )
    if m:
        try:
            sy, sm, sd, em, ed = (int(x) for x in m.groups())
            start = date(sy, sm, sd)
            # 종료가 시작보다 작은 월이면 해를 넘긴 것 (예: 12/30~01/02)
            ey = sy + 1 if em < sm else sy
            end = date(ey, em, ed)
            return start, end
        except ValueError:
            return None, None

    # 단일 날짜
    if len(fulls) == 1:
        try:
            d = date(int(fulls[0][0]), int(fulls[0][1]), int(fulls[0][2]))
            return d, d
        except ValueError:
            return None, None

    return None, None


# ---------------------------------------------------------------------------
# 인메모리 캐시 (TTL 기반)
# 라즈베리파이 같은 느린 환경에서 38커뮤 페이지 매번 fetch 안 하게.
# 봇 프로세스 살아있는 동안만 캐시. 재시작 시 비워짐.
# ---------------------------------------------------------------------------
import time as _time

# TTL은 백그라운드 자동 갱신(매시 정각)보다 약간 길게 잡아 갱신 실패 시도 마진 확보
_SCHEDULE_TTL_SEC = 5400    # 청약일정: 90분
_LISTED_TTL_SEC = 7200      # 신규상장 결과: 2시간

_schedule_cache: Optional[tuple[float, list]] = None
_listed_cache: dict[int, tuple[float, list]] = {}


def _is_fresh(ts: float, ttl: float) -> bool:
    return (_time.time() - ts) < ttl


def fetch_schedule_list() -> list[IpoSchedule]:
    """
    공모주 청약일정 페이지(/html/fund/index.htm?o=k)에서
    (종목번호, 종목명, 청약기간, 주관사)를 추출한다.

    각 데이터 tr의 td 배치 (2026년 기준):
        [0] 종목명 (<a href="/html/fund/?o=v&no=XXXX">)
        [1] 청약일 ('2026.06.17~06.18' 형식, 같은 달이면 종료일 MM.DD 약식)
        [2] 환불일
        [3] 희망공모가 밴드
        [5] 주관사

    상세 링크 패턴(`/html/fund/?o=v&no=`)을 가진 tr만 골라내 안정적으로 파싱.

    인메모리 캐시 10분 TTL 적용 — 같은 봇 프로세스에서 짧은 시간 내 재호출 시 즉시 반환.
    """
    global _schedule_cache
    if _schedule_cache and _is_fresh(_schedule_cache[0], _SCHEDULE_TTL_SEC):
        log.debug("schedule cache hit (%d종목)", len(_schedule_cache[1]))
        return _schedule_cache[1]

    soup = _fetch(SCHEDULE_URL)
    results: list[IpoSchedule] = []

    for a in soup.find_all("a", href=re.compile(r"/html/fund/\?o=v.*no=\d+")):
        href = a.get("href", "")
        m = re.search(r"no=(\d+)", href)
        if not m:
            continue
        no = m.group(1)
        name = a.get_text(strip=True)
        if not name:
            continue

        tr = a.find_parent("tr")
        if tr is None:
            continue

        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        # 데이터 행은 보통 td 5개 이상
        if len(tds) < 5:
            continue

        # 청약일 td: 첫 번째로 날짜 패턴이 잡히는 셀
        sub_start: Optional[date] = None
        sub_end: Optional[date] = None
        for cell in tds:
            s, e = _parse_subscribe_period(cell)
            if s is not None:
                sub_start, sub_end = s, e
                break

        # 주관사: '증권/투자/금융' 키워드 셀
        underwriter = ""
        for cell in tds[::-1]:
            if re.search(r"증권|투자|금융|뱅크", cell):
                underwriter = cell
                break

        results.append(IpoSchedule(
            no=no,
            name=name,
            subscribe_start=sub_start,
            subscribe_end=sub_end,
            underwriter=underwriter,
        ))

    # 같은 종목이 표에 여러 번 나올 수 있으므로 no 기준 중복 제거
    seen: set[str] = set()
    deduped: list[IpoSchedule] = []
    for item in results:
        if item.no in seen:
            continue
        seen.add(item.no)
        deduped.append(item)

    log.info("청약일정 페이지에서 %d개 종목 수집", len(deduped))
    _schedule_cache = (_time.time(), deduped)
    return deduped


# ---------------------------------------------------------------------------
# 1-b) 신규상장 결과 페이지 파싱 (첫날 수익률)
# ---------------------------------------------------------------------------
def _parse_listing_row(tds: list[str], no: str, name: str) -> Optional[ListedStock]:
    """
    신규상장 페이지의 데이터 td 배열 → ListedStock.
    컬럼 매핑:
        [0] 종목명
        [1] 상장일 (YYYY/MM/DD)
        [2] 현재가
        [4] 공모가
        [6] 첫날 시초가  ('-' / 빈 = 미상장)
        [8] 첫날 종가    ('예정' / '-' = 미상장)
    """
    if len(tds) < 9:
        return None

    listing_date = _parse_date_kr(tds[1]) if len(tds) > 1 else None
    if listing_date is None:
        return None

    def _pick_int(s: str) -> Optional[int]:
        s = (s or "").strip()
        if not s or s in {"-", "예정"}:
            return None
        return _to_int(s)

    return ListedStock(
        no=no,
        name=name,
        listing_date=listing_date,
        current_price=_pick_int(tds[2]) if len(tds) > 2 else None,
        offering_price=_pick_int(tds[4]) if len(tds) > 4 else None,
        open_price=_pick_int(tds[6]) if len(tds) > 6 else None,
        close_price=_pick_int(tds[8]) if len(tds) > 8 else None,
    )


def fetch_listed_stocks(year: int, max_pages: int = 8) -> list[ListedStock]:
    """
    신규상장 결과 페이지에서 특정 연도(year)에 상장한 종목 모두 가져오기.
    페이지를 순회하며 해당 연도가 더 이상 안 나오면 멈춤.
    상장일이 미래(미상장)인 종목 + 종가가 없는 종목은 제외 가능 → 호출부에서 필터.

    수익률 계산은 ListedStock.return_pct 프로퍼티에서 자동.
    인메모리 캐시 1시간 TTL 적용 — 라즈베리파이에서 큰 페이지 fetch 안 하도록.
    """
    if year in _listed_cache and _is_fresh(_listed_cache[year][0], _LISTED_TTL_SEC):
        cached = _listed_cache[year][1]
        log.debug("listed cache hit for %d (%d종목)", year, len(cached))
        return cached

    results: list[ListedStock] = []
    seen_nos: set[str] = set()

    for page in range(1, max_pages + 1):
        url = LISTED_URL_FMT.format(page=page)
        soup = _fetch(url)

        page_year_set: set[int] = set()
        page_added = 0
        for a in soup.find_all("a", href=re.compile(r"\?o=v.*no=\d+")):
            href = a.get("href", "")
            m = re.search(r"no=(\d+)", href)
            if not m:
                continue
            no = m.group(1)
            if no in seen_nos:
                continue
            name = a.get_text(strip=True)
            if not name or len(name) < 2:
                continue

            tr = a.find_parent("tr")
            if tr is None:
                continue
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 9:
                continue

            stock = _parse_listing_row(tds, no, name)
            if stock is None or stock.listing_date is None:
                continue

            page_year_set.add(stock.listing_date.year)
            if stock.listing_date.year == year:
                results.append(stock)
                seen_nos.add(no)
                page_added += 1

        log.debug("listed page=%d → %d개 추가 (페이지 연도: %s)",
                  page, page_added, sorted(page_year_set))

        # 이 페이지에 더 이상 target year 데이터가 없고, 모두 더 과거 연도면 종료
        if page_year_set and all(y < year for y in page_year_set):
            log.debug("페이지 %d부터 %d년 이전만 나옴 → 페이징 중단", page, year)
            break

    log.info("%d년 상장 종목 %d개 수집", year, len(results))
    _listed_cache[year] = (_time.time(), results)
    return results


# ---------------------------------------------------------------------------
# 2) 종목 상세 페이지 파싱
# ---------------------------------------------------------------------------
# 콤마 있는 경우와 없는 경우 모두 처리
# - "1,486.66" → "1,486.66"
# - "1486.66"  → "1486.66"
# - "12,000"   → "12,000"
_NUM_RE = re.compile(r"(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)")


def _to_float(text: str) -> Optional[float]:
    """문자열에서 첫 번째 숫자를 float로. '1,523.45:1' → 1523.45"""
    if text is None:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _to_int(text: str) -> Optional[int]:
    """'13,000 원' → 13000"""
    if text is None:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return None


def _parse_price_band(text: str) -> tuple[Optional[int], Optional[int]]:
    """'10,000원 ~ 12,000원' → (10000, 12000)"""
    if not text:
        return None, None
    nums = _NUM_RE.findall(text)
    parsed: list[int] = []
    for n in nums:
        try:
            parsed.append(int(float(n.replace(",", ""))))
        except ValueError:
            pass
    if len(parsed) >= 2:
        return parsed[0], parsed[1]
    if len(parsed) == 1:
        return parsed[0], parsed[0]
    return None, None


def _compute_float_ratio_from_lockup_table(soup: BeautifulSoup) -> Optional[float]:
    """
    보호예수표에서 상장후 유통가능 비율 추정.

    38커뮤 보호예수표는 종목별로 컬럼 수가 다르다(폴레드 12, 에스팀 10).
    그러나 데이터 행은 항상 다음 패턴으로 끝남:
        [..., 매각제한 주식수, 매각제한 지분율(%), 유통가능 주식수, 유통가능 지분율, 기간]
    따라서 끝에서 4번째 td(`tds[-4]`)가 매각제한 지분율(%).

    스팩(SPAC)은 보호예수표 자체가 페이지에 없음 → '보호예수 의무 없음 = 거의 100% 유통'
    으로 간주해 100.0 반환 (점수표상 0점이 되지만 등급 산정은 가능).

    표는 있는데 데이터를 못 읽으면 None 반환 → grader가 그 항목만 0점 처리.
    """
    for tbl in soup.find_all("table"):
        head_text = tbl.get_text(" ", strip=True)[:400]
        if "유통가능물량" not in head_text or "매각제한" not in head_text:
            continue

        locked_pct_sum = 0.0
        data_rows = 0
        for tr in tbl.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 6:
                continue

            row_text = " ".join(tds)
            # 소계/합계 행은 중복 카운트 방지로 스킵
            if any(kw in row_text for kw in ["소계", "합계", "합 계"]):
                continue
            # 숫자 없는 헤더 행 스킵
            if not any(re.search(r"\d", t) for t in tds):
                continue

            # 매각제한 지분율 = 끝에서 4번째
            cell = tds[-4]
            m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", cell)
            if not m:
                continue
            pct = float(m.group(1))
            if 0 <= pct <= 100:
                locked_pct_sum += pct
                data_rows += 1

        if data_rows == 0:
            continue  # 다음 표 시도

        if locked_pct_sum > 100.0:
            log.debug("락업 합계가 100%% 초과(%.2f) — 표 분석 보류", locked_pct_sum)
            return None

        return round(max(0.0, 100.0 - locked_pct_sum), 2)

    # 보호예수표 자체가 없는 경우(스팩 등) → 거의 모든 주식 유통가능으로 간주
    log.debug("보호예수표 미발견 → float_ratio=100.0 (스팩 등)")
    return 100.0


def _classify_price_position(low: int, high: int, final: int) -> str:
    """
    희망공모가 밴드(low~high) 대비 확정공모가 위치 분류.
    - final > high  → above_band
    - final == high → upper
    - final == low  → lower
    - final < low   → below_band
    - 그 외 (low < final < high) → middle
    """
    if final > high:
        return "above_band"
    if final < low:
        return "below_band"
    if final == high:
        return "upper"
    if final == low:
        return "lower"
    return "middle"


def fetch_detail(no: str, name: str = "", base: Optional[IpoSchedule] = None) -> IpoDetail:
    """
    종목 상세 페이지에서 4대 지표를 파싱.
    파싱 실패 항목은 None으로 두고 raw_errors에 사유 누적.
    """
    url = DETAIL_URL_FMT.format(no=no)
    soup = _fetch(url)

    detail = IpoDetail(no=no, name=name)
    if base is not None:
        detail.underwriter = base.underwriter
        detail.subscribe_start = base.subscribe_start
        detail.subscribe_end = base.subscribe_end

    # 종목명이 비어 있으면 페이지 타이틀에서 보강 (목록 단계에서 받은 이름 우선)
    if not detail.name:
        title = soup.find("title")
        if title:
            detail.name = re.sub(r"\s*-\s*38커뮤니케이션.*$", "", title.get_text(strip=True))

    # --- 1) 기관 수요예측 경쟁률 ---
    try:
        # '기관경쟁률' 또는 '수요예측 경쟁률' 또는 '기관 경쟁률'
        raw = _find_value_by_label(soup, r"기관\s*경쟁률|수요예측\s*경쟁률")
        detail.competition_ratio = _to_float(raw)
        if detail.competition_ratio is None:
            detail.raw_errors.append(f"competition_ratio 파싱 실패 (raw={raw!r})")
    except Exception as e:  # noqa: BLE001
        detail.raw_errors.append(f"competition_ratio 예외: {e}")

    # --- 2) 의무보유확약 비율 ---
    try:
        raw = _find_value_by_label(soup, r"의무\s*보유\s*확약")
        detail.lockup_ratio = _to_float(raw)
        if detail.lockup_ratio is None:
            detail.raw_errors.append(f"lockup_ratio 파싱 실패 (raw={raw!r})")
    except Exception as e:  # noqa: BLE001
        detail.raw_errors.append(f"lockup_ratio 예외: {e}")

    # --- 3) 상장일 유통가능 물량 비율 ---
    # 38커뮤 2026 신규 페이지는 '상장일 유통가능 비율'을 단일 라벨/값으로 보여주지 않고
    # 보호예수표(주주별 보유주식/매각제한)만 제공한다. 합계 계산이 필요한 항목.
    # 우선 _compute_float_ratio_from_lockup_table()로 합산 추정을 시도하고,
    # 실패 시 None 처리 → grader가 "데이터 부족"으로 분류.
    try:
        detail.float_ratio = _compute_float_ratio_from_lockup_table(soup)
        if detail.float_ratio is None:
            detail.raw_errors.append("float_ratio: 보호예수표 합산 실패 또는 정보 없음")
    except Exception as e:  # noqa: BLE001
        detail.raw_errors.append(f"float_ratio 예외: {e}")

    # --- 4) 공모가 결정 위치: 희망공모가 + 확정공모가 비교 ---
    try:
        band_raw = _find_value_by_label(soup, r"희망\s*공모가")
        final_raw = _find_value_by_label(soup, r"확정\s*공모가|공모가\s*확정")
        low, high = _parse_price_band(band_raw or "")
        final_price = _to_int(final_raw or "")
        detail.band_low = low
        detail.band_high = high
        detail.final_price = final_price
        if low is not None and high is not None and final_price is not None:
            detail.price_position = _classify_price_position(low, high, final_price)
        else:
            detail.raw_errors.append(
                f"price_position 파싱 실패 (band={band_raw!r}, final={final_raw!r})"
            )
    except Exception as e:  # noqa: BLE001
        detail.raw_errors.append(f"price_position 예외: {e}")

    # 주관사는 일정 페이지에서 받아온 값을 그대로 신뢰.
    # 상세 페이지 보호예수표에 '주관사 의무인수' 헤더가 있어 잘못 매칭되므로 fallback 안 함.

    if detail.raw_errors:
        log.warning("[%s/%s] 파싱 경고: %s", no, detail.name, "; ".join(detail.raw_errors))

    return detail


# ---------------------------------------------------------------------------
# 간단 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 콘솔 로그
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== 청약일정 목록 ===")
    schedules = fetch_schedule_list()
    for s in schedules[:10]:
        print(f"  {s.no:>6}  {s.name:<20}  {s.subscribe_start} ~ {s.subscribe_end}  {s.underwriter}")

    if schedules:
        # 첫 번째 종목으로 상세 파싱 점검
        first = schedules[0]
        print(f"\n=== 상세 파싱 테스트: {first.name} (no={first.no}) ===")
        d = fetch_detail(first.no, first.name, base=first)
        print(f"  경쟁률      : {d.competition_ratio}")
        print(f"  의무보유    : {d.lockup_ratio}%")
        print(f"  유통물량    : {d.float_ratio}%")
        print(f"  희망공모가  : {d.band_low} ~ {d.band_high}")
        print(f"  확정공모가  : {d.final_price}")
        print(f"  공모가위치  : {d.price_position}")
        print(f"  주관사      : {d.underwriter}")
        if d.raw_errors:
            print(f"  파싱 경고   : {d.raw_errors}")
