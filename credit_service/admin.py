"""Explicit operator recovery; deliberately not an unauthenticated HTTP endpoint."""

import argparse
import json
from datetime import UTC, datetime

from credit_service.config import get_settings
from credit_service.repository import CreditRepository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "requeue"])
    parser.add_argument("task_id", nargs="?")
    args = parser.parse_args()
    repo = CreditRepository(get_settings())
    try:
        if args.command == "requeue":
            if not args.task_id:
                parser.error("requeue requires a task ID")
            if not repo.requeue_failed(args.task_id):
                parser.exit(1, "Credit missing or not failed; no change made\n")
            print("Credit requeued; dispatcher will publish it")
        else:
            oldest = repo.credits.find_one({"status": "processing"}, sort=[("created_at", 1)])
            print(
                json.dumps(
                    {
                        "processing": repo.credits.count_documents({"status": "processing"}),
                        "failed": repo.credits.count_documents({"status": "failed"}),
                        "oldest_pending_seconds": (
                            (datetime.now(UTC) - oldest["created_at"]).total_seconds()
                            if oldest
                            else 0
                        ),
                    }
                )
            )
    finally:
        repo.close()


if __name__ == "__main__":
    main()
