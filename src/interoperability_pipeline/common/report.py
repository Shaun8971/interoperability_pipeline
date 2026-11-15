"""Volume report and failure detectors, built from the event log and error queue.

What it does: prints how many messages each source sent, by message type and outcome, what is
sitting in the error queue, and three checks that each catch a different kind of failure.
Where it sits in the pipeline: the last stop; run it after traffic to see what happened.
Standard and spec: none (project-specific).
Inputs / outputs: the data directory's events.jsonl / error_queue.jsonl -> text report.
How to extend: add a detector function and add its result to ``build_report``.
Run:  python -m interoperability_pipeline.common.report

The three detectors (each catches what the others cannot):
  UNKNOWN data    -> the error queue: a code, hospital or message type we have no rule for.
  MISSING data    -> the volume checks: a source that went quiet, or visits admitted but never
                     discharged: nothing arrived, so nothing can fail loudly.
  WRONG-BUT-BELIEVABLE data -> the stale-event and balance checks: it looked valid, so only a
                     rule that compares it with what we already hold can catch it.
"""

from collections import Counter, defaultdict
from pathlib import Path

from interoperability_pipeline.common.event_log import (
    ERROR_QUEUE_FILE,
    EVENTS_FILE,
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED,
    OUTCOME_RETRY,
    OUTCOME_STALE,
    OUTCOME_UNKNOWN_SOURCE,
    EventLog,
)
from interoperability_pipeline.common.settings import PROJECT_ROOT, load_settings
from interoperability_pipeline.mapping.sources import load_sources

MESSAGE_ADMIT, MESSAGE_DISCHARGE = "ADT^A01", "ADT^A03"
ALL_OUTCOMES = [
    OUTCOME_ACCEPTED,
    OUTCOME_STALE,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN_SOURCE,
    OUTCOME_RETRY,
]


def build_report(event_log: EventLog, configured_sources: list[str]) -> str:
    """Build the report text.

    Example (shape):
        VOLUME  source / type / outcome counts ...
        DETECTORS  unknown: 3 in queue | missing: ... | wrong-but-believable: 1 stale, 1 refused 835
    """
    events = event_log.read(EVENTS_FILE)
    queue = event_log.read(ERROR_QUEUE_FILE)
    counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for event in events:
        counts[(event["source"], event["message_type"] or "(none)")][event["outcome"]] += 1

    lines = ["VOLUME (messages by source, type and outcome)"]
    header = f"  {'source':<12}{'type':<12}" + "".join(f"{o:>16}" for o in ALL_OUTCOMES)
    lines += [header, "  " + "-" * (len(header) - 2)]
    for (source, message_type), outcomes in sorted(counts.items()):
        lines.append(
            f"  {source:<12}{message_type:<12}"
            + "".join(f"{outcomes[o]:>16}" for o in ALL_OUTCOMES)
        )
    if not counts:
        lines.append("  (no events yet)")

    lines += ["", "ERROR QUEUE (kept with the raw message so it can be fixed and replayed)"]
    for entry in queue:
        lines.append(
            f"  [{entry['kind']}] {entry['source']} {entry['control_id']}: "
            + "; ".join(entry["reasons"])[:110]
        )
    if not queue:
        lines.append("  (empty)")

    unknown = [e for e in queue if e["kind"] in (OUTCOME_REJECTED, OUTCOME_UNKNOWN_SOURCE)]
    stale = [e for e in queue if e["kind"] == OUTCOME_STALE]
    refused_835 = [e for e in queue if e["source"] == "payer"]
    seen_sources = {event["source"] for event in events}
    quiet = [name for name in configured_sources if name not in seen_sources]
    admits = sum(
        1 for e in events if e["message_type"] == MESSAGE_ADMIT and e["outcome"] == OUTCOME_ACCEPTED
    )
    discharges = sum(
        1
        for e in events
        if e["message_type"] == MESSAGE_DISCHARGE and e["outcome"] == OUTCOME_ACCEPTED
    )
    lines += [
        "",
        "DETECTORS",
        f"  UNKNOWN data (error queue)          : {len(unknown)} message(s) with an unrecognised "
        "code, hospital, type or missing field",
        f"  MISSING data (volume checks)        : quiet sources {quiet or 'none'}; "
        f"admits {admits} vs discharges {discharges} (difference = visits still open)",
        f"  WRONG-BUT-BELIEVABLE (compare checks): {len(stale)} stale event(s) not applied, "
        f"{len(refused_835)} unbalanced 835(s) refused",
    ]
    return "\n".join(lines)


def main() -> None:
    settings = load_settings()
    event_log = EventLog(Path(PROJECT_ROOT / settings["paths"]["data_directory"]))
    print(build_report(event_log, [source.name for source in load_sources().values()]))


if __name__ == "__main__":
    main()
