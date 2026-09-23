"""Local actual-event trace, never a fabricated provider transcript."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone

class SchemaFailure(ValueError): pass
class SourceFailure(ValueError): pass
class SemanticRejection(ValueError): pass
class BudgetExhausted(ValueError): pass


def emit(service, task_id, event, **details):
    state = service.runs[task_id]
    record = {"at": datetime.now(timezone.utc).isoformat(), "run_id": state.run_id,
              "task_id": task_id, "mode": state.mode, "event": event, **details}
    encoded = json.dumps(record, ensure_ascii=False, default=str)
    for name, value in os.environ.items():
        if value and ("API_KEY" in name or name.endswith("TOKEN")):
            encoded = encoded.replace(value, "[REDACTED]")
    directory = Path(os.getenv("AI_TRACE_DIR", "data/traces"))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{state.run_id}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
