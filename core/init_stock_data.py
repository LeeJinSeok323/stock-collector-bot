import time
import random
import datetime
import pandas as pd
from curl_cffi import requests
import yfinance as yf
from config.db_config import get_db_connection, get_clickhouse_client

INCREMENTAL_BATCH_SIZE = 15
INCREMENTAL_PERIOD = "60d"


def _initial_load(conn, session):
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT ticker FROM stocks
            WHERE last_price_date IS NULL
            ORDER BY ticker ASC
        """)
        tickers = [row['ticker'] for row in cursor.fetchall()]

    if not tickers:
        return False

    print(f"[initial] {len(tickers)} tickers remaining for initial load.", flush=True)
    count = 0

    for ticker in tickers:
        try:
            if ticker.endswith(("-WT", "-UN", "-PR", "-P")):
                print(f"[initial] Skipping special stock: {ticker}", flush=True)
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE stocks SET last_fetched_at = NOW() WHERE ticker = %s", (ticker,))
                    conn.commit()
                continue

            print(f"[initial] Fetching {ticker}...", flush=True)
            yf.shared._ERRORS.clear()

            data = yf.download(
                tickers=ticker,
                start="1970-01-01",
                interval="1d",
                auto_adjust=False,
                prepost=False,
                threads=False,
                session=session
            )

            if yf.shared._ERRORS:
                error_msgs = str(yf.shared._ERRORS).lower()
                if "rate limit" in error_msgs or "too many requests" in error_msgs or "429" in error_msgs:
                    print(f"[initial] Rate limited on {ticker}. Sleeping for 15 minutes.", flush=True)
                    time.sleep(900)
                    continue
                elif any(k in error_msgs for k in ("delisted", "no timezone found", "no price data", "yftzmissingerror", "period", "invalid")):
                    print(f"[initial] Delisted detected for {ticker}.", flush=True)
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE stocks SET status = 'DELISTED', delisted_at = NOW() WHERE ticker = %s", (ticker,))
                        conn.commit()
                else:
                    print(f"[initial] Download warning/error on {ticker}: {yf.shared._ERRORS}", flush=True)
                    time.sleep(60)
                    continue

            if not data.empty:
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)
                data = data.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

                val_list = []
                last_date = None
                for date, row in data.iterrows():
                    d = date.date() if hasattr(date, 'date') else datetime.date.fromisoformat(str(date)[:10])
                    val_list.append((ticker, d, float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume'])))
                    if last_date is None or d > last_date:
                        last_date = d

                if val_list:
                    ch = get_clickhouse_client()
                    ch.execute(
                        'INSERT INTO stocker.ohlcv_daily (ticker, date, open, high, low, close, volume) VALUES',
                        val_list
                    )

                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE stocks SET last_fetched_at = NOW(), last_price_date = %s WHERE ticker = %s",
                        (last_date, ticker)
                    )
                    conn.commit()

                print(f"[initial] {ticker} success. Inserted {len(val_list)} rows. Last date: {last_date}", flush=True)
            else:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE stocks SET last_fetched_at = NOW() WHERE ticker = %s", (ticker,))
                    conn.commit()
                print(f"[initial] {ticker} has no data.", flush=True)

            count += 1
            if count % 50 == 0:
                macro_sleep = random.uniform(120.0, 300.0)
                print(f"[initial] Coffee break! Sleeping for {macro_sleep:.2f} seconds...", flush=True)
                time.sleep(macro_sleep)
            else:
                time.sleep(random.uniform(5.0, 15.0))

        except Exception as e:
            conn.rollback()
            print(f"[initial] Unexpected Error on {ticker}: {e}", flush=True)
            time.sleep(60)

    return True


def _incremental_update(conn, session):
    with conn.cursor() as cursor:
        # 14일 이상 밀린 종목만 우선 처리 — 갭 없으면 최신화 필요한 것만
        cursor.execute("""
            SELECT ticker, last_price_date FROM stocks
            WHERE last_price_date IS NOT NULL
              AND status = 'ACTIVE'
              AND last_price_date < CURDATE() - INTERVAL 14 DAY
            ORDER BY last_price_date ASC
        """)
        rows = cursor.fetchall()

    if not rows:
        print("[incremental] No tickers need update (all within 14 days).", flush=True)
        return

    tickers_dates = {row['ticker']: row['last_price_date'] for row in rows}
    tickers = list(tickers_dates.keys())
    total_batches = (len(tickers) + INCREMENTAL_BATCH_SIZE - 1) // INCREMENTAL_BATCH_SIZE

    print(f"[incremental] {len(tickers)} tickers behind by 14+ days ({total_batches} batches).", flush=True)

    for i in range(0, len(tickers), INCREMENTAL_BATCH_SIZE):
        batch = tickers[i:i + INCREMENTAL_BATCH_SIZE]
        batch_num = i // INCREMENTAL_BATCH_SIZE + 1
        try:
            yf.shared._ERRORS.clear()
            data = yf.download(
                tickers=batch,
                period=INCREMENTAL_PERIOD,
                interval="1d",
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
                error_msgs = str(yf.shared._ERRORS).lower()
                if "rate limit" in error_msgs or "too many requests" in error_msgs or "429" in error_msgs:
                    print(f"[incremental] Rate limited at batch {batch_num}. Sleeping for 15 minutes.", flush=True)
                    time.sleep(900)
                    continue

            val_list = []
            updated_tickers = {}

            for ticker in batch:
                last_date = tickers_dates[ticker]
                try:
                    ticker_data = data[ticker] if len(batch) > 1 else data
                    if isinstance(ticker_data.columns, pd.MultiIndex):
                        ticker_data.columns = ticker_data.columns.get_level_values(0)
                    ticker_data = ticker_data.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

                    for date, row in ticker_data.iterrows():
                        d = date.date() if hasattr(date, 'date') else datetime.date.fromisoformat(str(date)[:10])
                        if d > last_date:
                            val_list.append((ticker, d, float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume'])))
                            if ticker not in updated_tickers or d > updated_tickers[ticker]:
                                updated_tickers[ticker] = d
                except Exception as e:
                    print(f"[incremental] Error parsing {ticker}: {e}", flush=True)

            if val_list:
                ch = get_clickhouse_client()
                ch.execute(
                    'INSERT INTO stocker.ohlcv_daily (ticker, date, open, high, low, close, volume) VALUES',
                    val_list
                )

            if updated_tickers:
                with conn.cursor() as cursor:
                    for ticker, new_date in updated_tickers.items():
                        cursor.execute(
                            "UPDATE stocks SET last_price_date = %s, last_fetched_at = NOW() WHERE ticker = %s",
                            (new_date, ticker)
                        )
                    conn.commit()

            print(f"[incremental] Batch {batch_num}/{total_batches}: {len(val_list)} rows inserted, {len(updated_tickers)} tickers updated.", flush=True)
            time.sleep(random.uniform(3.0, 8.0))

        except Exception as e:
            print(f"[incremental] Batch {batch_num} error: {e}", flush=True)
            time.sleep(30)


def init_stock_data():
    conn = get_db_connection()
    session = requests.Session(impersonate="chrome120")
    try:
        has_initial = _initial_load(conn, session)
        if not has_initial:
            _incremental_update(conn, session)
    finally:
        conn.close()
