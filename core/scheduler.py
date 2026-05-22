import schedule
import time
import threading
from core.init_stock_data import init_stock_data
from core.index_collector import collect_indices
from core.minute_collector import collect_and_aggregate
from core.sec_collector import collect_sec_filings
from scripts.sync_ticker_list import update_stocks


def _run_in_thread(func):
    """스케줄 작업을 별도 스레드에서 실행 (블로킹 방지)"""
    t = threading.Thread(target=func, daemon=True)
    t.start()


def _data_pipeline():
    """
    Phase 1 → Phase 2 자동 전환 파이프라인.
    - 갭 있음: init_stock_data (Phase 1) 실행
    - 갭 없음: collect_and_aggregate (Phase 2) 실행
    _already_collected() 체크로 당일 중복 수집 방지.
    """
    had_work = init_stock_data()
    if not had_work:
        print("[pipeline] Phase 1 complete → triggering Phase 2", flush=True)
        collect_and_aggregate()


def run_scheduler():
    print("[scheduler] Registering jobs...", flush=True)
    schedule.every(7).days.do(_run_in_thread, update_stocks)
    schedule.every(1).hours.do(_run_in_thread, _data_pipeline)
    schedule.every(10).minutes.do(_run_in_thread, collect_indices)
    schedule.every().day.at("22:00").do(_run_in_thread, collect_sec_filings)

    print("[scheduler] Running initial jobs...", flush=True)
    _run_in_thread(update_stocks)
    _run_in_thread(_data_pipeline)
    _run_in_thread(collect_indices)

    while True:
        schedule.run_pending()
        time.sleep(60)


def start_stock_update_service():
    print("[scheduler] Starting background stock update service...", flush=True)
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
