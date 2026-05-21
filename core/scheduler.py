import schedule
import time
import threading
from core.init_stock_data import init_stock_data
from core.index_collector import collect_indices
from scripts.sync_ticker_list import update_stocks


def _run_in_thread(func):
    """스케줄 작업을 별도 스레드에서 실행 (블로킹 방지)"""
    t = threading.Thread(target=func, daemon=True)
    t.start()


def run_scheduler():
    print("[scheduler] Registering jobs...", flush=True)
    schedule.every(7).days.do(_run_in_thread, update_stocks)
    schedule.every(1).hours.do(_run_in_thread, init_stock_data)
    schedule.every(10).minutes.do(_run_in_thread, collect_indices)

    print("[scheduler] Running initial jobs...", flush=True)
    _run_in_thread(update_stocks)
    _run_in_thread(init_stock_data)
    _run_in_thread(collect_indices)

    while True:
        schedule.run_pending()
        time.sleep(60)


def start_stock_update_service():
    print("[scheduler] Starting background stock update service...", flush=True)
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
