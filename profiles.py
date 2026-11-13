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


def sync_contracts(project, root, old, new, old_go=None, new_go=None):
    """Update named release contracts, never arbitrary source/tests or evidence."""
    root = Path(root)
    names = ["README.md", "compose.yml"]
    if project == "memories":
        names += ["go-vod/Dockerfile.rkmpp", "go-vod/Dockerfile.rkmpp-test",
                  "go-vod/README.md", "go-vod/cmd/govod-health/main.go",
                  "scripts/build-v11.sh", "scripts/deploy-v11.sh", "scripts/install-player-v11.sh",
                  "scripts/test-v11-deploy-preflight.sh", "scripts/test-v11-rkmpp.sh",
                  ".github/workflows/check.yml", ".github/workflows/v11-hardware-image.yml"]
    else:
        names += ["Dockerfile.rkmpp", "scripts/build.sh"]
    for name in names:
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise RuntimeError("Release contract refuses symlinks: " + name)
        text = path.read_text()
        updated = text
        if name == "README.md":
            updated = updated.replace("Nextcloud Memories " + old, "Nextcloud Memories " + new, 1) if project == "memories" else updated.replace("Immich " + old, "Immich " + new, 1)
            if project == "memories":
                updated = re.sub(r"(Nextcloud Memories " + re.escape(new) + r"[^\n]*?/ go-vod )" + re.escape(old_go),
                                 lambda m:m[1] + new_go, updated, count=1)
        if project == "memories":
            updated = updated.replace("memories-" + old + "-govod-" + old_go,
                                      "memories-" + new + "-govod-" + new_go)
            for before, after in [("Expected Memories " + old + ",", "Expected Memories " + new + ","),
                                  ('"$version" = ' + old + ' ]', '"$version" = ' + new + ' ]'),
                                  ("MEMORIES_TEST_VERSION=" + old + " ", "MEMORIES_TEST_VERSION=" + new + " "),
                                  ("Memories " + old + " is connected to go-vod " + old_go,
                                   "Memories " + new + " is connected to go-vod " + new_go)]:
                updated = updated.replace(before, after)
            if name == "go-vod/cmd/govod-health/main.go":
                updated = updated.replace('health.Version != "' + old_go + '"', 'health.Version != "' + new_go + '"')
                updated = updated.replace("go-vod " + old_go + " healthy", "go-vod " + new_go + " healthy")
            if name.startswith(".github/workflows/"):
                updated = updated.replace('const VERSION = "' + old_go + '"', 'const VERSION = "' + new_go + '"')
            if name in {".github/workflows/v11-hardware-image.yml", "scripts/test-v11-rkmpp.sh"}:
                updated = updated.replace("go-vod " + old_go, "go-vod " + new_go)
            if name == "go-vod/README.md":
                updated = updated.replace("# go-vod " + old_go, "# go-vod " + new_go, 1)
            if name == "scripts/test-v11-deploy-preflight.sh":
                updated = re.sub(r"^for version in (.*); do$", lambda m: "for version in " +
                    " ".join(dict.fromkeys([v for v in m[1].split() if v != new] + [old])) + "; do", updated, flags=re.M)
        else:
            updated = re.sub(r"rkmpp-" + re.escape(old) + r"(?=-r\d)", "rkmpp-" + new, updated)
        if updated != text:
            path.write_text(updated)
    if project == "memories":
        for name, pattern in [
            ("memories_rkmpp_v11/lib/Listener/PlayerScriptListener.php", r"in_array\(\$version, (\[[^\]]+\])"),
            ("scripts/test-v11-compat.php", r"foreach \((\[[^\]]+\]) as \$supported\)")]:
            path = root / name
            if not path.exists():
                continue
            text = path.read_text()
            match = re.search(pattern, text)
            if not match:
                raise RuntimeError("Version support contract changed: " + name)
            supported = match[1]
            if "'" + new + "'" not in supported:
                supported = supported[:-1] + ", '" + new + "']"
            updated = text[:match.start(1)] + supported + text[match.end(1):]
            if name == "scripts/test-v11-compat.php":
                unsupported = re.search(r"foreach \((\[[^\]]+\]) as \$unsupported\)", updated)
                if unsupported:
                    values = re.findall(r"'([^']+)'", unsupported[1])
                    values = [v for v in values if v != new]
                    replacement = "[" + ", ".join(repr(v) for v in values) + "]"
                    updated = updated[:unsupported.start(1)] + replacement + updated[unsupported.end(1):]
                    count = 6 * len(re.findall(r"'[^']+'", supported)) + len(values)
                    updated = re.sub(r"listener: \d+ cases", f"listener: {count} cases", updated)
            path.write_text(updated)


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
    sync_contracts(project, root, old, new, old_go, new_go)
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
