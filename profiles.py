"""Project-specific reconciliation and checks, outside the model's control."""
import json
import re
import tempfile
from pathlib import Path
from core import command, image_digest, save_json

PROJECTS = {
    "memories": {"fork": "meitelis/memories-rkmpp", "upstream": "pulsejet/memories", "build": "scripts/build-v11.sh"},
    "immich": {"fork": "meitelis/immich-rkmpp", "upstream": "immich-app/immich", "build": "scripts/build.sh"},
}


def current_version(project, root):
    root = Path(root)
    manifest = root / ("upstream-version.json" if project == "memories" else "upstream.json")
    if manifest.exists():
        return json.loads(manifest.read_text())["version"]
    match = re.search(r"Nextcloud Memories (\d+\.\d+\.\d+)", (root / "README.md").read_text())
    if not match:
        raise RuntimeError("Cannot identify the current official Memories baseline")
    return match[1]


def git(root, *args):
    return command(["git", "-C", str(root), *args], timeout=900)


def upstream_file(upstream, ref, name):
    return git(upstream, "show", ref + ":" + name)


def active_files(root):
    for name in git(root, "ls-files").splitlines():
        if any(p in {"history", "verification", "upstream", "automation", "LICENSES"} for p in Path(name).parts):
            continue
        path = Path(root) / name
        if path.is_symlink():
            raise RuntimeError("Tracked symlinks require manual review")
        if path.is_file() and path.stat().st_size < 300000:
            try:
                yield path, path.read_text()
            except UnicodeError:
                continue


def reconcile(project, root, upstream, old, new, old_ref, new_ref):
    """Try three-way source merges, then synchronize known version/pin contracts."""
    root, upstream = Path(root), Path(upstream)
    changes = git(upstream, "diff", "--name-only", old_ref, new_ref).splitlines()
    notes = []
    if project == "memories":
        old_go = re.search(r'const VERSION = "([^"]+)"', upstream_file(upstream, old_ref, "go-vod/main.go"))[1]
        new_go = re.search(r'const VERSION = "([^"]+)"', upstream_file(upstream, new_ref, "go-vod/main.go"))[1]
        paths = [p for p in changes if p.startswith("go-vod/")]
    else:
        old_go = new_go = None
        paths = [p for p in changes if (root / "overlays" / p).exists()]
    for name in paths:
        relative = name if project == "memories" else "overlays/" + name
        target = root / relative
        if target.is_symlink():
            raise RuntimeError("Upstream reconciliation refuses symlinks")
        before = after = None
        try:
            before = upstream_file(upstream, old_ref, name)
        except RuntimeError:
            pass
        try:
            after = upstream_file(upstream, new_ref, name)
        except RuntimeError:
            pass
        if before is None and after is not None and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after + "\n")
            continue
        if after is None:
            raise RuntimeError("Upstream deleted an integrated file; manual review required: " + name)
        if before is None or not target.exists():
            raise RuntimeError("Ambiguous upstream file addition: " + name)
        with tempfile.TemporaryDirectory() as temp:
            base, other = Path(temp) / "base", Path(temp) / "other"
            base.write_text(before + "\n")
            other.write_text(after + "\n")
            # merge-file updates target and leaves explicit markers on conflicts.
            try:
                command(["git", "merge-file", str(target), str(base), str(other)], timeout=30)
            except RuntimeError:
                notes.append("Resolve upstream merge conflict: " + relative)
    for path, text in active_files(root):
        if path.suffix == ".md":
            # Update the advertised target only; never relabel historical measurements.
            if path == root / "README.md":
                updated = text.replace(old, new, 1)
            else:
                continue
        elif path.name == "NOTICE":
            continue
        else:
            updated = text.replace(old, new)
        relative = path.relative_to(root).as_posix()
        protocol_file = (relative == "go-vod/main.go" or relative.startswith(("go-vod/api/", "go-vod/core/", "go-vod/cmd/")) or relative.startswith(("scripts/", ".github/workflows/")) or path.name.startswith("Dockerfile") or relative == "compose.yml")
        if old_go and old_go != new_go and protocol_file:
            updated = updated.replace(old_go, new_go)
        if project == "memories" and path.name == "test-v11-deploy-preflight.sh":
            # The new current version must not remain in the negative test table.
            updated = re.sub(r"^for version in (.*); do$", lambda m: "for version in " +
                " ".join(dict.fromkeys([v for v in m[1].split() if v != new] + [old])) + "; do", updated, flags=re.M)
        if project == "memories" and path.name in {"PlayerScriptListener.php", "test-v11-compat.php"}:
            # Preserve older verified player hooks while adding the new one.
            updated = re.sub(r"\['11\.0\.2'[^\]]*\]", lambda m: m[0][:-1] +
                (", '" + old + "'" if "'" + old + "'" not in m[0] else "") + "]", updated)
            if path.name == "test-v11-compat.php":
                supported = re.search(r"foreach \((\[[^\]]+\]) as \$supported\)", updated)
                unsupported = re.search(r"foreach \((\[[^\]]+\]) as \$unsupported\)", updated)
                if supported and unsupported:
                    n = 6 * len(re.findall(r"'[^']+'", supported[1])) + len(re.findall(r"'[^']+'", unsupported[1]))
                    updated = re.sub(r"listener: \d+ cases", f"listener: {n} cases", updated)
        if updated != text:
            path.write_text(updated)
    if project == "memories":
        save_json(root / "upstream-version.json", {"repository": PROJECTS[project]["upstream"],
            "version": new, "tag": "v" + new, "commit": new_ref, "goVodVersion": new_go})
        info = root / "memories_rkmpp_v11/appinfo/info.xml"
        text = info.read_text()
        match = re.search(r"<version>(\d+)\.(\d+)\.(\d+)</version>", text)
        if not match:
            raise RuntimeError("Helper has no version")
        text = text.replace(match[0], f"<version>{match[1]}.{match[2]}.{int(match[3])+1}</version>", 1)
        info.write_text(text)
    else:
        manifest_path = root / "upstream.json"
        manifest = json.loads(manifest_path.read_text())
        previous_commit, previous_image = manifest["commit"], manifest["runtimeImage"]
        runtime = image_digest("immich-app/immich-server", "v" + new)
        manifest.update(version=new, tag="v" + new, commit=new_ref, runtimeImage=runtime)
        save_json(manifest_path, manifest)
        dockerfile = root / "Dockerfile.rkmpp"
        text = dockerfile.read_text().replace(previous_commit, new_ref).replace(previous_image, runtime)
        official_docker = upstream_file(upstream, new_ref, "server/Dockerfile")
        base_image = re.search(r"^FROM (ghcr[.]io/immich-app/base-server-dev:[^ ]+@sha256:[a-f0-9]{64}) AS builder$", official_docker, re.M)
        if not base_image:
            raise RuntimeError("Official build layout changed; manual review required")
        text = re.sub(r"^FROM [^ ]+ AS builder$", "FROM " + base_image[1] + " AS builder", text, count=1, flags=re.M)
        # Official pnpm pin changes are allowed, but all build/test commands stay fixed.
        package = json.loads(upstream_file(upstream, new_ref, "package.json"))
        manager = package["packageManager"]
        if not re.fullmatch(r"pnpm@\d+\.\d+\.\d+", manager):
            raise RuntimeError("Unsupported upstream package manager; manual review needed")
        text = re.sub(r"pnpm@\d+\.\d+\.\d+", manager, text)
        dockerfile.write_text(text)
        prepare = root / "scripts/prepare-source.mjs"
        prepare.write_text(re.sub(r"pnpm@\d+\.\d+\.\d+", manager, prepare.read_text()))
    return notes, changes


def validate(root):
    for name in git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines():
        path = Path(root) / name
        if path.is_symlink():
            raise RuntimeError("Candidate contains a symlink")
        if path.is_file() and path.stat().st_size < 1000000:
            try:
                text = path.read_text()
            except UnicodeError:
                continue
            if re.search(r"^(<<<<<<< .+|>>>>>>> .+)$", text, re.M):
                raise RuntimeError("Unresolved merge conflict: " + name)
    git(root, "diff", "--check")
