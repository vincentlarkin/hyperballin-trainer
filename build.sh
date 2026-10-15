#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
engine="${CONTAINER_ENGINE:-podman}"
image=localhost/hyperballin-trainer-build
"$engine" build -t "$image" -f Containerfile .
container_id="$("$engine" create "$image")"
trap '"$engine" rm "$container_id" >/dev/null' EXIT
mkdir -p dist/licenses
"$engine" cp "$container_id:/src/dist/hyperballin-trainer" dist/hyperballin-trainer
"$engine" cp "$container_id:/src/dist/LICENSE" dist/LICENSE
"$engine" cp "$container_id:/src/dist/licenses/." dist/licenses
chmod +x dist/hyperballin-trainer
tar --owner=0 --group=0 --numeric-owner -C dist -czf dist/hyperballin-trainer-linux-x86_64.tar.gz hyperballin-trainer LICENSE licenses
(cd dist && sha256sum hyperballin-trainer-linux-x86_64.tar.gz > SHA256SUMS)
