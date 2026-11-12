#!/usr/bin/env bash
# One-time migration of the legacy home-directory Compose deployment.
# Run as root only after the current maintenance job has finished.
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
target_dir=/armbian/rkmpp-maintainer
builder_name=rkmpp-maintainer-builder-1
cache_volume=rkmpp-maintainer_builder-data

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail "Run with sudo bash scripts/migrate-to-portainer.sh."
[[ "$source_dir" == /home/meitelis/rkmpp-maintainer ]] || fail "Expected legacy checkout /home/meitelis/rkmpp-maintainer; refusing to move another directory."
[[ -d /armbian && ! -e "$target_dir" && ! -L "$target_dir" ]] || fail "Need existing /armbian and absent /armbian/rkmpp-maintainer; nothing was moved."
[[ -d "$source_dir/.git" && -f "$source_dir/portainer-stack.yml" && -d "$source_dir/data" ]] || fail "Legacy checkout or downloaded workspace is missing."
for tool in docker flock cp du df awk; do command -v "$tool" >/dev/null || fail "Missing command: $tool"; done
[[ $(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$builder_name") == rkmpp-maintainer ]] || fail "Unexpected builder project."
[[ $(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' "$builder_name") == "$source_dir/data" ]] || fail "Builder workspace does not match the legacy checkout."
docker_root=$(docker info -f '{{.DockerRootDir}}')
cache_dir=$(docker volume inspect -f '{{.Mountpoint}}' "$cache_volume")
[[ "$cache_dir" == "$docker_root/volumes/$cache_volume/_data" && -d "$cache_dir" ]] || fail "Unexpected builder cache path."
[[ ! -e "$source_dir/builder" ]] || fail "A builder directory already exists in the checkout; refusing to overwrite it."

# The running replay/smoke owns this lock. Refuse to interrupt it or copy live state.
exec 9>"$source_dir/data/maintainer.lock"
flock -n 9 || fail "A maintainer job is still running. Let it finish, then run this migration again."
while IFS= read -r container; do
  [[ "$container" == "$builder_name" ]] || fail "Another maintainer container is running ($container); finish that job before migration."
done < <(docker ps --filter label=com.docker.compose.project=rkmpp-maintainer --format '{{.Names}}')

required_kib=$(du -sk "$source_dir" "$cache_dir" | awk '{total += $1} END {print total}')
available_kib=$(df -Pk /armbian | awk 'END {print $4}')
(( available_kib > required_kib + 1048576 )) || fail "Insufficient free space under /armbian to preserve the checkout and builder cache."

printf '%s\n' "Stopping only the legacy rkmpp-maintainer project; models, jobs, images and volumes are retained."
docker compose --project-name rkmpp-maintainer --project-directory "$source_dir" -f "$source_dir/compose.yml" down
mv -T -- "$source_dir" "$target_dir"
mkdir -- "$target_dir/builder"
# Host GNU cp runs as root, preserving Docker overlay metadata and xattrs.
cp -a -- "$cache_dir/." "$target_dir/builder/"
cd -- "$target_dir"
docker build -t local/rkmpp-maintainer:prototype-3-portainer .
docker compose -f portainer-stack.yml config --quiet
printf '%s\n' "Prepared /armbian/rkmpp-maintainer for Portainer." \
  "In Portainer deploy stack rkmpp-maintainer from main, compose path portainer-stack.yml." \
  "Keep MAINTAINER_ACTION=idle and AUTO_UPDATE=false initially." \
  "The original Docker cache volume is retained as a rollback copy. This script does not deploy a CLI-managed stack."
