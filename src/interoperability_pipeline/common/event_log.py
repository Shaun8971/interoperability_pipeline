"""Event log and error queue: a durable record of everything the pipeline did.

What it does: appends one JSON line per processed message to ``events.jsonl`` (what happened,
for which source and message type) and one JSON line per problem message to
``error_queue.jsonl`` (including the raw message, so a person can fix and replay it).
Where it sits in the pipeline: written by the listener after every message; read by
``report.py`` and ``run_journey.py``.
Standard and spec: none (project-specific). JSON Lines format: https://jsonlines.org
Inputs / outputs: event details -> lines in files under the data directory (git-ignored).
How to extend: add a field to ``record``; the report reads the same file. For high volume,
swap the files for a database behind the same two methods.

Why a queue and not just the log: the log says *that* something failed; the queue keeps the
message itself, so nothing unrecognised is ever lost and it can be replayed after a fix.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

EVENTS_FILE = "events.jsonl"
ERROR_QUEUE_FILE = "error_queue.jsonl"

# --- Outcomes (what happened to a message) ----------------------------------------
OUTCOME_ACCEPTED = "accepted"  # stored in FHIR, ACK AA
OUTCOME_STALE = "stale_ignored"  # an older event than the one we hold: not applied, ACK AA
OUTCOME_REJECTED = "rejected"  # the message itself is wrong, ACK AE
OUTCOME_RETRY = "retry_later"  # our side failed (e.g. FHIR down), ACK AR
OUTCOME_UNKNOWN_SOURCE = "unknown_source"  # sending facility has no config file, ACK AE


class EventLog:
    """Appends events and queued errors under one data directory."""

    def __init__(self, data_directory: Path) -> None:
        self._directory = data_directory

    def _append(self, file_name: str, entry: dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
        with (self._directory / file_name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def record(
        self,
        *,
        source: str,
        message_type: str,
        control_id: str,
        outcome: str,
        detail: str = "",
        resources: list[str] | None = None,
    ) -> None:
        """One line per message.

        Example:
            record(source="hospital_a", message_type="ADT^A01", control_id="MSG00001",
                   outcome="accepted", resources=["Patient/1000", "Encounter/1001"])
        """
        self._append(
            EVENTS_FILE,
            {
                "source": source,
                "message_type": message_type,
                "control_id": control_id,
                "outcome": outcome,
                "detail": detail,
                "resources": resources or [],
            },
        )

    def enqueue(
        self, *, kind: str, source: str, control_id: str, reasons: list[str], raw_message: str
    ) -> None:
        """Keep a problem message (raw text included, all synthetic) for review and replay."""
        self._append(
            ERROR_QUEUE_FILE,
            {
                "kind": kind,
                "source": source,
                "control_id": control_id,
                "reasons": reasons,
                "raw_message": raw_message,
            },
        )

    def read(self, file_name: str) -> list[dict[str, Any]]:
        """Read back every entry of one file (empty list if it does not exist yet)."""
        path = self._directory / file_name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
