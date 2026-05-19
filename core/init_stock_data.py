import time
import random
import pandas as pd
from curl_cffi import requests
import yfinance as yf
from config.db_config import get_db_connection

def init_stock_data(limit=50):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT ticker FROM stocks 
                WHERE last_fetched_at IS NULL 
                ORDER BY ticker ASC LIMIT %s
            """, (limit,))
            tickers = [row['ticker'] for row in cursor.fetchall()]

        if not tickers:
            print("[batch] All stocks fetched. Nothing to do.", flush=True)
            return

        session = requests.Session(impersonate="chrome120")
        count = 0

        for ticker in tickers:
            try:
                if ticker.endswith(("-WT", "-UN", "-PR", "-P")):
                    print(f"[batch] Skipping special stock: {ticker}", flush=True)
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE stocks SET last_fetched_at = NOW() WHERE ticker = %s", (ticker,))
                        conn.commit()
                    continue

                print(f"[batch] Fetching {ticker}...", flush=True)
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
                        print(f"[batch] Rate limited on {ticker}. Sleeping for 15 minutes.", flush=True)
                        time.sleep(900)
                        continue
                    elif "delisted" in error_msgs or "no timezone found" in error_msgs or "no price data" in error_msgs or "yftzmissingerror" in error_msgs or "period" in error_msgs or "invalid" in error_msgs:
                        print(f"[batch] Delisted detected for {ticker}.", flush=True)

                        with conn.cursor() as cursor:
                            cursor.execute("UPDATE stocks SET status = 'DELISTED', delisted_at = NOW() WHERE ticker = %s", (ticker,))
                            conn.commit()

                    else:
                        print(f"[batch] Download warning/error on {ticker}: {yf.shared._ERRORS}", flush=True)
                        time.sleep(60)
                        continue

                if not data.empty:
                    val_list = []

                    if isinstance(data.columns, pd.MultiIndex):
                        data.columns = data.columns.get_level_values(0)

                    data = data.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])

                    for date, row in data.iterrows():
                        val_list.append((
                            ticker,
                            date.strftime('%Y-%m-%d'),
                            float(row['Open']),
                            float(row['High']),
                            float(row['Low']),
                            float(row['Close']),
                            int(row['Volume'])
                        ))

                    with conn.cursor() as cursor:
                        sql = """
                            INSERT INTO stock_price_history (ticker, date, open, high, low, close, volume)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE 
                                open = VALUES(open), high = VALUES(high), 
                                low = VALUES(low), close = VALUES(close), 
                                volume = VALUES(volume)
                        """
                        if val_list:
                            cursor.executemany(sql, val_list)
                        cursor.execute("UPDATE stocks SET last_fetched_at = NOW() WHERE ticker = %s", (ticker,))
                        conn.commit()

                    print(f"[batch] {ticker} success. Inserted {len(val_list)} rows.", flush=True)

                else:
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE stocks SET last_fetched_at = NOW() WHERE ticker = %s", (ticker,))
                        conn.commit()
                    print(f"[batch] {ticker} really has no data.", flush=True)

                count += 1
                if count % 50 == 0:
                    macro_sleep = random.uniform(120.0, 300.0)
                    print(f"[batch] Coffee break! Sleeping for {macro_sleep:.2f} seconds...", flush=True)
                    time.sleep(macro_sleep)
                else:
                    time.sleep(random.uniform(5.0, 15.0))

            except Exception as e:
                conn.rollback()
                print(f"[batch] Unexpected Error on {ticker}: {e}", flush=True)
                time.sleep(60)

    finally:
        conn.close()