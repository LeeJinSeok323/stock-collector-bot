"""
SEC 공시 실시간 감시
- alarm 테이블 DISTINCT ticker 감시 (1분마다 목록 갱신)
- EDGAR RSS 5초 폴링으로 신규 공시 감지
- 새 공시 발견 시 파이프라인 스레드 트리거
"""
import os
import time
import threading
import requests
import xml.etree.ElementTree as ET
from config.db_config import get_db_connection

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "Personal Project (jinseoki10@gmail.com)")
POLL_INTERVAL = 5
WATCHLIST_REFRESH = 60
RSS_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=&dateb=&owner=include&count=40&output=atom"
)

EDGAR_NS = {
    'atom': 'http://www.w3.org/2005/Atom',
    'edgar': 'https://www.sec.gov/Archives/edgar'
}

_lock = threading.Lock()
_cik_to_ticker: dict[str, str] = {}   # cik(str, no leading zeros) → ticker
_seen: set[str] = set()               # 처리 완료 accession_no


def _load_watchlist():
    """alarm 테이블 기준 감시 티커 → CIK 매핑 로드"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT DISTINCT a.ticker, s.cik
                FROM alarm a
                JOIN stocks s ON s.ticker = a.ticker
                WHERE s.cik IS NOT NULL AND s.cik != 0
                  AND s.status = 'ACTIVE'
            """)
            rows = c.fetchall()
        mapping = {str(row['cik']): row['ticker'] for row in rows}
        with _lock:
            _cik_to_ticker.clear()
            _cik_to_ticker.update(mapping)
        print(f"[sec_monitor] watchlist 갱신: {len(mapping)}개 티커", flush=True)
    finally:
        conn.close()


def _load_seen_from_db():
    """서버 재시작 시 최근 24시간 accession_no를 seen에 추가 (중복 알림 방지)"""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT accession_no FROM sec_filing
                WHERE notified_at IS NOT NULL
                  AND created_at >= NOW() - INTERVAL 1 DAY
            """)
            rows = c.fetchall()
        with _lock:
            for r in rows:
                _seen.add(r['accession_no'])
    finally:
        conn.close()


def _poll_rss() -> list[dict]:
    """EDGAR RSS에서 최신 공시 목록 파싱"""
    headers = {"User-Agent": SEC_USER_AGENT}
    resp = requests.get(RSS_URL, headers=headers, timeout=10)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    filings = []

    for entry in root.findall('atom:entry', EDGAR_NS):
        # accession-number
        acc_el = entry.find('edgar:accession-number', EDGAR_NS)
        if acc_el is None:
            continue
        acc_no = acc_el.text.strip()

        # CIK
        cik_el = entry.find('.//edgar:cik-number', EDGAR_NS)
        cik = cik_el.text.strip().lstrip('0') if cik_el is not None else None

        # form type
        type_el = entry.find('edgar:filing-type', EDGAR_NS)
        form_type = type_el.text.strip() if type_el is not None else ''

        # filing date
        date_el = entry.find('edgar:filing-date', EDGAR_NS)
        filing_date = date_el.text.strip() if date_el is not None else ''

        filings.append({
            'accession_no': acc_no,
            'cik': cik,
            'form_type': form_type,
            'filing_date': filing_date,
        })

    return filings


def _on_new_filing(ticker: str, filing: dict):
    """신규 공시 감지 시 처리 파이프라인 (별도 스레드에서 실행)"""
    acc_no = filing['accession_no']
    print(
        f"[sec_monitor] 신규 공시 감지 | {ticker} | {filing['form_type']} | {acc_no}",
        flush=True
    )
    # TODO: 본문 파싱 → Gemini 분석 → 텔레그램 전송
    # pipeline.process(ticker, filing)


def _monitor_loop():
    _load_seen_from_db()
    _load_watchlist()

    last_watchlist_refresh = time.time()

    while True:
        try:
            # watchlist 주기적 갱신
            if time.time() - last_watchlist_refresh >= WATCHLIST_REFRESH:
                _load_watchlist()
                last_watchlist_refresh = time.time()

            filings = _poll_rss()

            with _lock:
                cik_map = dict(_cik_to_ticker)

            for f in filings:
                acc_no = f['accession_no']
                cik = f['cik']

                if not cik or cik not in cik_map:
                    continue

                with _lock:
                    if acc_no in _seen:
                        continue
                    _seen.add(acc_no)

                ticker = cik_map[cik]
                t = threading.Thread(
                    target=_on_new_filing,
                    args=(ticker, f),
                    daemon=True
                )
                t.start()

        except requests.RequestException as e:
            print(f"[sec_monitor] RSS 요청 실패: {e}", flush=True)
        except Exception as e:
            print(f"[sec_monitor] 오류: {e}", flush=True)

        time.sleep(POLL_INTERVAL)


def start_sec_monitor():
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    print("[sec_monitor] 감시 시작", flush=True)
