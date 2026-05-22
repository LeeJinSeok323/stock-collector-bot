import os
import requests
from config.db_config import get_db_connection
from dotenv import load_dotenv

load_dotenv()

def update_stocks():
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    headers = {"User-Agent": os.getenv("SEC_USER_AGENT", "MyBot/1.0 (contact@example.com)")}

    try:
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json()

        rows = data['data']
        active_tickers = []
        records = []

        exclude_exchanges = ["OTC", "CBOE", "UNKNOWN", ""]
        trash_suffixes = ['-P', '.P', '-W', '.W', '-R', '.R', '-U', '.U', '+']

        for row in rows:
            ticker = str(row[2]).upper().strip()
            exchange = (row[3] or "UNKNOWN").upper().strip()

            if exchange in exclude_exchanges:
                continue

            if len(ticker) > 5:
                continue

            if any(s in ticker for s in trash_suffixes):
                continue

            # 구분자 없는 워런트(W), 권리(R), 유닛(U) 제거 (예: CMIIW, BACCR)
            if len(ticker) >= 3 and ticker[-1] in ('W', 'R', 'U') and ticker[-2].isalpha():
                continue

            ticker = ticker.replace(' ', '-').replace('.', '-')

            if not all(c.isalnum() or c == '-' for c in ticker):
                continue

            active_tickers.append(ticker)
            records.append((ticker, str(row[0]), row[1], exchange))

        if not records:
            return

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                sql_upsert = """
                    INSERT INTO stocks (ticker, cik, company_name, exchange, status, delisted_at)
                    VALUES (%s, %s, %s, %s, 'ACTIVE', NULL)
                    ON DUPLICATE KEY UPDATE
                        cik = VALUES(cik),
                        company_name = VALUES(company_name),
                        exchange = VALUES(exchange),
                        status = IF(delisted_at IS NOT NULL, 'DELISTED', 'ACTIVE'),
                        delisted_at = delisted_at
                """
                cursor.executemany(sql_upsert, records)

                if active_tickers:
                    sql_delist = """
                        UPDATE stocks 
                        SET status = 'DELISTED', delisted_at = NOW() 
                        WHERE status = 'ACTIVE' 
                          AND ticker NOT IN %s
                    """
                    cursor.execute(sql_delist, (tuple(active_tickers),))

                conn.commit()
                print(f"[batch] Update complete. {len(records)} stocks are now ACTIVE.", flush=True)

        except Exception as e:
            conn.rollback()
            print(f"[batch] DB Error: {e}", flush=True)
        finally:
            conn.close()

    except Exception as e:
        print(f"[batch] Fetching Error: {e}", flush=True)

if __name__ == "__main__":
    update_stocks()