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
    if result.get("candidate_verified") is not True:
        raise RuntimeError("Candidate has not passed independent technical qualification")
    if result.get("artifact_status") != "validated-awaiting-owner-review":
        raise RuntimeError("Candidate is not awaiting owner source and playback review")
    if result.get("deployment_approved") is True or result.get("ready_to_deploy") is True:
        raise RuntimeError("Technical validation must not claim owner deployment acceptance")

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
    verify_archive_config(archive, image, image_id)

    helper_digest = None
    if result.get("project") == "memories":
        helper = job / "memories-helper.tar.gz"
        if helper.is_symlink() or not helper.is_file() or not tarfile.is_tarfile(helper):
            raise RuntimeError("Memories release requires its matching Nextcloud helper")
        recorded = result.get("helper_sha256")
        if not isinstance(recorded, str) or not re.fullmatch("[a-f0-9]{64}", recorded):
            raise RuntimeError("Memories helper requires a recorded SHA256")
        with helper.open("rb") as handle:
            helper_digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if helper_digest != recorded:
            raise RuntimeError("Memories helper checksum changed since candidate qualification")
    return {"image": image, "image_id": image_id,
            "archive": str(archive), "sha256": actual,
            "bytes": archive.stat().st_size,
            "helper": str(job / "memories-helper.tar.gz") if result.get("project") == "memories" else None,
            "helper_sha256": helper_digest}


def verify_archive_config(archive, image, image_id):
    # Docker save includes a manifest mapping repo tags to layers/config.
    # Inspect that manifest to reject an unrelated or malformed tar export.
    try:
        with tarfile.open(archive, "r:") as saved:
            info = saved.getmember("manifest.json")
            stream = saved.extractfile(info)
            if stream is None or info.size > 2_000_000:
                raise RuntimeError("Docker export has invalid manifest")
            manifests = json.load(stream)
            if not isinstance(manifests, list):
                raise RuntimeError("Docker export has invalid manifest entries")
            names = [entry.name for entry in saved.getmembers()]
            if len(names) != len(set(names)):
                raise RuntimeError("Docker export contains ambiguous duplicate members")
            matches = [entry for entry in manifests if isinstance(entry, dict)
                       and isinstance(entry.get("RepoTags"), list)
                       and image in entry["RepoTags"]]
            if len(matches) != 1:
                raise RuntimeError("Docker export must contain exactly one matching candidate tag")
            config_name = matches[0].get("Config")
            if (not isinstance(config_name, str) or
                    config_name not in {image_id[7:] + ".json", "blobs/sha256/" + image_id[7:]}):
                raise RuntimeError("Docker export config does not match the tested image or has an invalid path")
            configuration = saved.getmember(config_name)
            if not configuration.isfile() or configuration.size <= 0 or configuration.size > 16_000_000:
                raise RuntimeError("Docker export has invalid image config size")
            config_stream = saved.extractfile(configuration)
            if config_stream is None:
                raise RuntimeError("Docker export lacks image config")
            config_bytes = config_stream.read()
            config_digest = hashlib.sha256(config_bytes).hexdigest()
            if config_digest != image_id.removeprefix("sha256:"):
                raise RuntimeError("Docker export config does not match hardware-tested image ID")
            config = json.loads(config_bytes)
            rootfs = config.get("rootfs", {}) if isinstance(config, dict) else {}
            diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
            layers = matches[0].get("Layers")
            if (config.get("architecture") != "arm64" or rootfs.get("type") != "layers"
                    or not isinstance(layers, list) or not isinstance(diff_ids, list)
                    or len(layers) != len(diff_ids)):
                raise RuntimeError("Docker export layers disagree with ARM64 config")
            for path, expected_digest in zip(layers, diff_ids):
                if (not isinstance(path, str) or path.startswith("/")
                        or ".." in path.split("/") or
                        not isinstance(expected_digest, str) or
                        IMAGE_ID.fullmatch(expected_digest) is None):
                    raise RuntimeError("Docker export has invalid layer path/digest")
                member = saved.getmember(path)
                if not member.isfile() or member.size <= 0:
                    raise RuntimeError("Docker export has missing/empty layer: " + path)
                source = saved.extractfile(member)
                if source is None:
                    raise RuntimeError("Docker export has unreadable layer: " + path)
                digest = hashlib.sha256()
                while block := source.read(1024 * 1024):
                    digest.update(block)
                if digest.hexdigest() != expected_digest.removeprefix("sha256:"):
                    raise RuntimeError("Docker export layer checksum mismatch: " + path)
    except (tarfile.TarError, OSError, json.JSONDecodeError, KeyError) as error:
        raise RuntimeError("Docker export is invalid: " + str(error)) from error
