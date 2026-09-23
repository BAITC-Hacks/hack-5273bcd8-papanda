"""Append-only in-memory trace of one run."""
from datetime import datetime, timezone


class Trace:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[dict] = []

    def add(self, event: str, **fields) -> dict:
        row = {"time": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
               "event": event, **fields}
        self.events.append(row)
        return row

    emit = add
