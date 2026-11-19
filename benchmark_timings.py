"""Summarize recorded inference turns in one job, including failed jobs."""
import argparse
import json
from pathlib import Path


def summarize(records):
    calls = replayed = measured = 0
    totals = dict(prompt_n=0, predicted_n=0, prompt_ms=0, predicted_ms=0)
    for record in records:
        if record.get("reused_read"):
            replayed += 1
            continue
        calls += 1
        timings = record.get("timings") or {}
        if not all(isinstance(timings.get(k), (int, float)) and timings[k] >= 0
                   for k in totals):
            continue
        measured += 1
        for key in totals:
            totals[key] += timings[key]
    report = {"recorded_calls": calls, "replayed_turns": replayed, "timed_calls": measured}
    for phase in ("prompt", "predicted"):
        ms = totals[phase + "_ms"]
        report[phase + "_tokens_per_second"] = totals[phase + "_n"] * 1000 / ms if ms else None
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", nargs="+", type=Path)
    args = parser.parse_args()

    def records():
        for path in args.transcripts:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    yield json.loads(line)

    print(json.dumps(summarize(records()), indent=2))


if __name__ == "__main__":
    main()
