"""Validate immutable synthetic RK3588 hardware qualification reports.

This validation is deliberately independent of the local coding model and the
candidate image. An image build or a successful process exit alone is not enough.
"""
import json
from pathlib import Path


def required_immich_cases():
    cases = {f"sdr-{height}-{codec}" for height in (480, 720, 1080, 1440, 2160)
             for codec in ("hevc", "h264")}
    cases.update(f"seek-{height}-{codec}" for height in (1080, 2160)
                 for codec in ("hevc", "h264"))
    cases.update(f"swdecode-720-{codec}" for codec in ("hevc", "h264"))
    cases.update(f"memories-{kind}-{height}-{codec}"
                 for kind, height in (("paced", 1080), ("seek", 2160))
                 for codec in ("hevc", "h264"))
    cases.update(f"rotation-1080-{codec}" for codec in ("hevc", "h264"))
    cases.update(f"audio-{kind}-{codec}"
                 for kind in ("aac-mono-44100", "seek-aac-mono-44100",
                              "pcm-stereo", "flac-stereo")
                 for codec in ("hevc", "h264"))
    cases.update(f"{kind}-{codec}" for kind in
                 ("sdr10-fullsize", "small-fullsize", "hdr-mali", "hdr-cpu")
                 for codec in ("hevc", "h264"))
    assert len(cases) == 38
    return cases


def validate_immich_report(scratch, expected_label=None):
    """Fail closed on missing, incomplete, duplicate or unsuccessful hardware cases."""
    scratch = Path(scratch)
    reports = list(scratch.glob("run-*/report.json"))
    if len(reports) != 1:
        raise RuntimeError(f"Expected exactly one RKMPP hardware report, got {len(reports)}")
    report = reports[0]
    if report.is_symlink() or not report.is_file():
        raise RuntimeError("Hardware report must be a regular file")
    data = json.loads(report.read_text())
    if data.get("schemaVersion") != 1 or data.get("quick") is not True:
        raise RuntimeError("Unexpected RKMPP hardware report schema or test mode")
    if expected_label is not None and data.get("label") != expected_label:
        raise RuntimeError("RKMPP hardware report label mismatch")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise RuntimeError("RKMPP hardware report has no cases")
    names = [case.get("name") for case in cases if isinstance(case, dict)]
    if len(names) != len(cases) or len(names) != len(set(names)):
        raise RuntimeError("RKMPP hardware report has duplicate or invalid cases")
    missing = required_immich_cases().difference(names)
    if missing:
        raise RuntimeError("RKMPP hardware report missing cases: " + ", ".join(sorted(missing)))
    if any(case.get("ok") is not True for case in cases):
        raise RuntimeError("RKMPP hardware report contains failing cases")
    if data.get("ok") is not True or data.get("failed") != 0 or data.get("passed") != len(cases):
        raise RuntimeError("RKMPP hardware report totals are inconsistent")
    return {"ok": True, "passed": len(cases), "failed": 0,
            "label": data.get("label"),
            "report": str(report.relative_to(scratch)),
            "required_cases": len(required_immich_cases())}
