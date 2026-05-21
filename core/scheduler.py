import schedule
import time
import threading
from core.init_stock_data import init_stock_data
from core.index_collector import collect_indices
from scripts.sync_ticker_list import update_stocks

def run_scheduler():
    print("[scheduler] Starting stock update jobs...", flush=True)
    schedule.every(7).days.do(update_stocks)
    print("[scheduler] Starting init_stock_data...", flush=True)
    schedule.every(1).hours.do(init_stock_data)
    print("[scheduler] Registering index collector (every 10 min)...", flush=True)
    schedule.every(10).minutes.do(collect_indices)

    print("[scheduler] Running initial jobs...", flush=True)
    update_stocks()
    init_stock_data()
    collect_indices()

    while True:
        schedule.run_pending()
        time.sleep(60)

def start_stock_update_service():
    print("[scheduler] Starting background stock update service...", flush=True)
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
