import datetime
import yfinance as yf
from config.db_config import get_clickhouse_client

INDICES = [
    {'symbol': '^KS11', 'name': 'KOSPI'},
    {'symbol': '^KQ11', 'name': 'KOSDAQ'},
    {'symbol': '^GSPC', 'name': 'S&P 500'},
    {'symbol': '^IXIC', 'name': 'NASDAQ'},
    {'symbol': 'KRW=X', 'name': 'USD/KRW'},
]


def collect_indices():
    print("[index_collector] Collecting market indices...", flush=True)
    rows = []
    now = datetime.datetime.now()

    for idx in INDICES:
        try:
            ticker = yf.Ticker(idx['symbol'])
            info = ticker.fast_info

            price = info.last_price
            prev_close = info.previous_close

            if price is None or prev_close is None:
                print(f"[index_collector] No data for {idx['symbol']}", flush=True)
                continue

            price = float(price)
            prev_close = float(prev_close)
            change_amt = price - prev_close
            change_pct = (change_amt / prev_close * 100) if prev_close else 0.0

            rows.append((idx['symbol'], idx['name'], price, prev_close, change_amt, change_pct, now))
            print(
                f"[index_collector] {idx['name']}: {price:,.2f}  ({change_pct:+.2f}%)",
                flush=True,
            )
        except Exception as e:
            print(f"[index_collector] Error fetching {idx['symbol']}: {e}", flush=True)

    if rows:
        ch = get_clickhouse_client()
        ch.execute(
            'INSERT INTO stocker.market_indices '
            '(symbol, name, price, prev_close, change_amt, change_pct, fetched_at) VALUES',
            rows,
        )
        print(f"[index_collector] Inserted {len(rows)} index rows.", flush=True)
    else:
        print("[index_collector] No data collected.", flush=True)
