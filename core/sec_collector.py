"""
SEC 공시 일일 수집기
- EDGAR 분기 인덱스에서 당일 제출 CIK 추출 (요청 1회)
- stocks 테이블 CIK와 교차 → 해당 종목만 submissions API 호출
- 신규 공시 메타데이터를 sec_filing에 저장
"""
import os
import time
import datetime
import requests
from config.db_config import get_db_connection

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "Personal Project (jinseoki10@gmail.com)")
RATE_LIMIT_SLEEP = 0.12


def _quarter(d: datetime.date) -> int:
    return (d.month - 1) // 3 + 1


def _fetch_today_ciks(target_date: datetime.date) -> set:
    """EDGAR 분기 인덱스에서 target_date에 제출된 CIK 집합 반환"""
    year = target_date.year
    qtr  = _quarter(target_date)
    url  = f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/company.idx"
    headers = {"User-Agent": SEC_USER_AGENT}

    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    date_str = target_date.strftime("%Y-%m-%d")
    ciks = set()
    for line in r.text.splitlines():
        # 고정폭: CIK는 뒤에서 두 번째 컬럼 (Date Filed은 뒤에서 두 번째)
        # 형식: CompanyName(62) FormType(12) CIK(12) DateFiled(12) Filename
        if date_str in line:
            parts = line.split()
            if len(parts) >= 3:
                # CIK는 Date Filed 바로 앞 컬럼
                date_idx = next((i for i, p in enumerate(parts) if p == date_str), -1)
                if date_idx >= 1:
                    cik_str = parts[date_idx - 1].lstrip("0")
                    if cik_str.isdigit():
                        ciks.add(cik_str)
    return ciks


def _get_tracked_cik_map(conn) -> dict:
    """stocks 테이블: {cik(str, lstrip 0): ticker}"""
    with conn.cursor() as c:
        c.execute("""
            SELECT ticker, cik FROM stocks
            WHERE status = 'ACTIVE' AND cik IS NOT NULL AND cik != 0
        """)
        return {str(row['cik']).lstrip("0"): row['ticker'] for row in c.fetchall()}


def _fetch_submissions(cik10: str) -> dict:
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    r = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=15)
    r.raise_for_status()
    return r.json()


def _parse_new_filings(data: dict, ticker: str, cik10: str, since: datetime.date) -> list:
    recent = data.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    if not accessions:
        return []

    forms     = recent.get("form", [])
    dates     = recent.get("filingDate", [])
    primaries = recent.get("primaryDocument", [])
    accepted  = recent.get("acceptanceDateTime", [])
    cik_raw   = str(data.get("cik", "")).lstrip("0")
    since_str = since.strftime("%Y-%m-%d")

    rows = []
    for i, acc_no in enumerate(accessions):
        filing_date = dates[i] if i < len(dates) else None
        if not filing_date or filing_date < since_str:
            break  # filingDate DESC 정렬 → 이후는 모두 오래된 것

        form_type   = forms[i]     if i < len(forms)     else None
        primary_doc = primaries[i] if i < len(primaries) else None
        acc_at_raw  = accepted[i]  if i < len(accepted)  else None

        accepted_at = None
        if acc_at_raw:
            accepted_at = acc_at_raw.replace("T", " ").replace("Z", "")[:19]

        acc_no_clean = acc_no.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_no_clean}"

        rows.append((
            cik10,
            ticker,
            acc_no,
            form_type,
            filing_date,
            accepted_at,
            primary_doc,
            f"{base}/{primary_doc}" if primary_doc else None,
            f"{base}/{acc_no_clean}.txt",
        ))
    return rows


def _bulk_insert(conn, rows: list) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO sec_filing
          (cik10, ticker, accession_no, form_type, filing_date,
           accepted_at, primary_doc, filing_html_url, filing_txt_url, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
        ON DUPLICATE KEY UPDATE
          form_type       = VALUES(form_type),
          filing_date     = VALUES(filing_date),
          accepted_at     = VALUES(accepted_at),
          primary_doc     = VALUES(primary_doc),
          filing_html_url = VALUES(filing_html_url),
          filing_txt_url  = VALUES(filing_txt_url)
    """
    with conn.cursor() as c:
        c.executemany(sql, rows)
    conn.commit()
    return len(rows)


def collect_sec_filings():
    target = datetime.date.today()
    print(f"[sec] 수집 대상일: {target}", flush=True)

    conn = get_db_connection()
    try:
        # 1) 당일 제출 CIK 목록 (EDGAR 인덱스)
        try:
            today_ciks = _fetch_today_ciks(target)
        except Exception as e:
            print(f"[sec] 인덱스 fetch 실패: {e}", flush=True)
            return

        print(f"[sec] EDGAR 당일 제출 CIK: {len(today_ciks)}개", flush=True)

        # 2) 추적 중인 종목과 교차
        tracked = _get_tracked_cik_map(conn)
        targets = {cik: tracked[cik] for cik in today_ciks if cik in tracked}
        print(f"[sec] 추적 종목 중 당일 제출: {len(targets)}개", flush=True)

        if not targets:
            print("[sec] 당일 신규 공시 없음.", flush=True)
            return

        # 3) 각 종목 submissions 호출 → 신규 공시 저장
        total_rows = 0
        since = target - datetime.timedelta(days=1)

        for cik_raw, ticker in targets.items():
            try:
                cik10 = cik_raw.zfill(10)
                data  = _fetch_submissions(cik10)
                rows  = _parse_new_filings(data, ticker, cik10, since)
                total_rows += _bulk_insert(conn, rows)
                time.sleep(RATE_LIMIT_SLEEP)
            except Exception as e:
                print(f"[sec] {ticker} 오류: {e}", flush=True)
                time.sleep(1)

        print(f"[sec] 완료. {len(targets)}개 종목, {total_rows}건 저장.", flush=True)

    finally:
        conn.close()
