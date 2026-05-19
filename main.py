import sys
import time
import traceback
from dotenv import load_dotenv

print("[main] Starting application...", flush=True)
load_dotenv()

try:
    from core.scheduler import start_stock_update_service
except Exception as e:
    print(f"[main] Import Error: {e}")
    traceback.print_exc()
    sys.exit(1)

def main():
    start_stock_update_service()

    print("[main] Collector running. Discord bot disabled.", flush=True)
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()