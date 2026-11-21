"""Run one daily upload batch from the queue (cron / Task Scheduler entry point).

Usage:
    python scripts/daily_upload.py           # upload up to YOUTUBE_DAILY_UPLOAD_LIMIT
    python scripts/daily_upload.py --force   # ignore the paused flag (still respects quota)
    python scripts/daily_upload.py --loop    # keep running as a daemon instead
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from amberclaw.queue_store import UploadQueue  # noqa: E402
from amberclaw.scheduler import UploadScheduler  # noqa: E402


def main():
    path = os.environ.get("UPLOAD_QUEUE_PATH", str(ROOT / "data" / "jobs" / "upload_queue.json"))
    queue = UploadQueue(path)
    scheduler = UploadScheduler(queue)
    print(f"queue={path}")
    print(f"state={queue.state()}")
    print(f"counts={queue.counts()}")

    if "--loop" in sys.argv:
        scheduler.start()
        print("scheduler running; press Ctrl+C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            scheduler.stop()
        return

    result = scheduler.run_once(force="--force" in sys.argv)
    print(f"result={result}")
    print(f"counts={queue.counts()}")


if __name__ == "__main__":
    main()
