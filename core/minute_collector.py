import time
import random
import datetime
import pandas as pd
from curl_cffi import requests
import yfinance as yf
from config.db_config import get_db_connection, get_clickhouse_client

MINUTE_BATCH_SIZE = 50


def _get_last_trading_day():
    """
    마지막 미국 거래일 반환.
    서버 TZ = KST(UTC+9), 스케줄러는 05:30 KST 실행
    → 05:30 KST = 16:30 ET(EDT) → 당일 미국 장 마감 후
    → 타겟 = 오늘 KST - 1일 (주말이면 직전 금요일)
    """
    target = datetime.date.today() - datetime.timedelta(days=1)
    while target.weekday() >= 5:  # 5=토, 6=일
        target -= datetime.timedelta(days=1)
    return target


def _already_collected(target_date):
    """해당 날짜 분봉 데이터가 이미 적재됐는지 확인"""
    ch = get_clickhouse_client()
    result = ch.execute(
        "SELECT count() FROM stocker.ohlcv_minute WHERE toDate(datetime) = %(d)s",
        {'d': target_date}
    )
    return result[0][0] > 0


def _collect_minute_bars(conn, session, target_date):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT ticker FROM stocks
            WHERE status = 'ACTIVE' AND last_price_date IS NOT NULL
            ORDER BY ticker ASC
        """)
        tickers = [row['ticker'] for row in cursor.fetchall()]

    if not tickers:
        print("[minute] No tickers.", flush=True)
        return

    ch = get_clickhouse_client()
    total_batches = (len(tickers) + MINUTE_BATCH_SIZE - 1) // MINUTE_BATCH_SIZE
    total_rows = 0
    print(f"[minute] {target_date} | {len(tickers)} tickers | {total_batches} batches", flush=True)

    for i in range(0, len(tickers), MINUTE_BATCH_SIZE):
        batch = tickers[i:i + MINUTE_BATCH_SIZE]
        batch_num = i // MINUTE_BATCH_SIZE + 1

        try:
            yf.shared._ERRORS.clear()
            data = yf.download(
                tickers=batch,
                period="2d",
                interval="1m",
                auto_adjust=False,
                prepost=False,
                threads=True,
                group_by="ticker",
                session=session,
                progress=False
            )

            if data.empty:
                continue

            if yf.shared._ERRORS:
                err = str(yf.shared._ERRORS).lower()
                if "rate limit" in err or "429" in err:
                    print(f"[minute] Rate limited at batch {batch_num}. Sleeping 15 min.", flush=True)
                    time.sleep(900)
                    continue

            val_list = []
            for ticker in batch:
                try:
                    td = data[ticker] if len(batch) > 1 else data
                    if isinstance(td.columns, pd.MultiIndex):
                        td.columns = td.columns.get_level_values(0)
                    td = td.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

                    for dt, row in td.iterrows():
                        # UTC 기준 날짜 추출 (정규장 9:30~16:00 ET = 13:30~20:00 UTC → 날짜 동일)
                        if hasattr(dt, 'tz_convert'):
                            dt_utc = dt.tz_convert('UTC').replace(tzinfo=None)
                        else:
                            dt_utc = dt.replace(tzinfo=None) if hasattr(dt, 'replace') else dt

                        if dt_utc.date() != target_date:
                            continue

                        val_list.append((
                            ticker,
                            dt_utc,
                            float(row['Open']),
                            float(row['High']),
                            float(row['Low']),
                            float(row['Close']),
                            int(row['Volume'])
                        ))
                except Exception as e:
                    print(f"[minute] Parse error {ticker}: {e}", flush=True)

            if val_list:
                ch.execute(
                    'INSERT INTO stocker.ohlcv_minute '
                    '(ticker, datetime, open, high, low, close, volume) VALUES',
                    val_list
                )
                total_rows += len(val_list)

            print(f"[minute] Batch {batch_num}/{total_batches}: {len(val_list)} rows", flush=True)
            time.sleep(random.uniform(3.0, 7.0))

        except Exception as e:
            print(f"[minute] Batch {batch_num} error: {e}", flush=True)
            time.sleep(30)

    print(f"[minute] Collection done. Total {total_rows} rows for {target_date}", flush=True)


def _aggregate_to_daily(target_date):
    """
    분봉 → 일봉 집계 후 ohlcv_daily 적재.
    ohlcv_daily 는 ReplacingMergeTree 이므로 중복 INSERT 는 자동 처리됨.
    """
    ch = get_clickhouse_client()
    print(f"[minute] Aggregating {target_date} → ohlcv_daily...", flush=True)

    ch.execute("""
        INSERT INTO stocker.ohlcv_daily (ticker, date, open, high, low, close, volume)
        SELECT
            ticker,
            toDate(datetime)          AS date,
            argMin(open,   datetime)  AS open,
            max(high)                 AS high,
            min(low)                  AS low,
            argMax(close,  datetime)  AS close,
            sum(volume)               AS volume
        FROM stocker.ohlcv_minute
        WHERE toDate(datetime) = %(d)s
        GROUP BY ticker, date
    """, {'d': target_date})

    print(f"[minute] Aggregation done for {target_date}", flush=True)


def _update_last_price_date(conn, target_date):
    """MySQL stocks 테이블 last_price_date 일괄 갱신"""
    with conn.cursor() as cursor:
        cursor.execute("""
            UPDATE stocks
            SET last_price_date = %s, last_fetched_at = NOW()
            WHERE status = 'ACTIVE'
              AND (last_price_date IS NULL OR last_price_date < %s)
        """, (target_date, target_date))
        conn.commit()
    print(f"[minute] last_price_date updated to {target_date}", flush=True)


def collect_and_aggregate():
    target_date = _get_last_trading_day()
    print(f"[minute] Target date: {target_date}", flush=True)

    if _already_collected(target_date):
        print(f"[minute] {target_date} already collected. Skipping.", flush=True)
        return

    conn = get_db_connection()
    session = requests.Session(impersonate="chrome120")
    try:
        _collect_minute_bars(conn, session, target_date)
        _aggregate_to_daily(target_date)
        _update_last_price_date(conn, target_date)
    finally:
        conn.close()
