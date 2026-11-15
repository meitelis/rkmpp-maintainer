"""Single fail-closed delivery contract for unattended RKMPP release images.

The release watcher must never consider a version complete just because its
build command returned zero. Only a reviewed, hardware-qualified Docker export
of the SAME candidate image may be advertised as ready.
"""
import hashlib
import json
import re
import tarfile
from pathlib import Path


IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


def verify_release_image(job, result):
    """Validate the finished export; return its immutable delivery metadata.

    This validation checks the on-disk archive directly, not an LLM assertion,
    a Docker tag alone, or the result of a historical/debug build.
    """
    job = Path(job)
    if result.get("kind") != "release-candidate":
        raise RuntimeError("Historical/test builds are not release deliveries")
    if result.get("agent_review", {}).get("review_complete") is not True:
        raise RuntimeError("Release candidate has no completed LLM source review")
    if result.get("ready_to_deploy") is not True:
        raise RuntimeError("Candidate is not approved for manual deployment")
    if result.get("artifact_status") != "ready-to-deploy":
        raise RuntimeError("Candidate has not passed the release delivery gate")

    image_id = result.get("image_id")
    image = result.get("image")
    if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
        raise RuntimeError("Missing immutable candidate image ID")
    if (not isinstance(image, str) or re.fullmatch(
            r"local/(?:immich|memories)-rkmpp:candidate-\d+\.\d+\.\d+-[a-f0-9]{8}", image) is None):
        raise RuntimeError("Unexpected candidate image tag")
    hw = result.get("hardware_qualification")
    if not isinstance(hw, dict) or hw.get("ok") is not True or hw.get("image_id") != image_id:
        raise RuntimeError("Release image differs from passing hardware qualification")
    if hw.get("arch") != "arm64":
        raise RuntimeError("Hardware qualification did not validate an ARM64 image")

    archive = job / "image.tar"
    checksum = job / "image.tar.sha256"
    for path in (archive, checksum):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Missing or unsafe Docker export: " + path.name)
    if archive.stat().st_size < 1024:
        raise RuntimeError("Docker export archive is empty or truncated")
    expected = checksum.read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}  image\.tar", expected):
        raise RuntimeError("Malformed candidate archive SHA256 sidecar")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if expected[:64] != actual:
        raise RuntimeError("Candidate Docker export checksum mismatch")
    # Docker save includes a manifest mapping repo tags to layers/config.
    # Inspect that manifest to reject an unrelated or malformed tar export.
    try:
        with tarfile.open(archive, "r:") as saved:
            info = saved.getmember("manifest.json")
            stream = saved.extractfile(info)
            if stream is None or info.size > 2_000_000:
                raise RuntimeError("Docker export has invalid manifest")
            manifests = json.load(stream)
            if not isinstance(manifests, list) or not any(
                    isinstance(entry, dict) and image in entry.get("RepoTags", [])
                    for entry in manifests):
                raise RuntimeError("Docker export does not contain the qualified candidate tag")
    except (tarfile.TarError, OSError, json.JSONDecodeError, KeyError) as error:
        raise RuntimeError("Docker export is invalid: " + str(error)) from error

    if result.get("project") == "memories":
        helper = job / "memories-helper.tar.gz"
        if helper.is_symlink() or not helper.is_file() or not tarfile.is_tarfile(helper):
            raise RuntimeError("Memories release requires its matching Nextcloud helper")
    return {"image": image, "image_id": image_id,
            "archive": str(archive), "sha256": actual,
            "bytes": archive.stat().st_size,
            "helper": str(job / "memories-helper.tar.gz") if result.get("project") == "memories" else None}
