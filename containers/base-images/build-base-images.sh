#!/usr/bin/env bash
# Build the operator CPU and GPU baseline images on a machine that already
# has the two parent images loaded. Writes checksummed archives under data/images/.

set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
FRAMEWORK="$(cd -- "${ROOT}/../.." && pwd -P)"
ARCHIVE_DIR="${SIE_IMAGE_DIR:-${FRAMEWORK}/data/images}"
PROXY_URL="${SIE_BUILD_PROXY:-${https_proxy:-${HTTPS_PROXY:-}}}"
CPU_TAG="${SIE_CPU_BASE_TAG:-sie-cpu-base:0.1.0}"
GPU_TAG="${SIE_GPU_BASE_TAG:-sie-gpu-base:0.1.0-cu128}"
CPU_PARENT="${SIE_CPU_PARENT:-python:3.11-slim-bookworm}"
GPU_PARENT="${SIE_GPU_PARENT:-pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel}"
EXPECTED_GPU_PARENT_ID="sha256:6b99022ca960b0bd57f96f2283f2e8584101b2c72f2723bab6af64d6478e04df"

fail() { echo "[FAIL] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker info >/dev/null 2>&1 || fail "docker daemon is not running"

actual_parent="$(docker image inspect --format '{{.Id}}' "${GPU_PARENT}")" || fail "GPU parent image is not loaded: ${GPU_PARENT}"
[[ "${actual_parent}" == "${EXPECTED_GPU_PARENT_ID}" ]] || fail "GPU parent image ID is ${actual_parent}, expected ${EXPECTED_GPU_PARENT_ID}"
docker image inspect "${CPU_PARENT}" >/dev/null 2>&1 || fail "CPU parent image is not loaded: ${CPU_PARENT}"

build_one() {
    local dockerfile="$1" tag="$2" parent="$3"
    echo "Building ${tag} from ${parent}"
    docker build \
        --network default \
        --build-arg "BASE_IMAGE=${parent}" \
        --build-arg "http_proxy=${PROXY_URL}" \
        --build-arg "https_proxy=${PROXY_URL}" \
        --build-arg "HTTP_PROXY=${PROXY_URL}" \
        --build-arg "HTTPS_PROXY=${PROXY_URL}" \
        --file "${ROOT}/${dockerfile}" \
        --tag "${tag}" \
        "${ROOT}"
}

build_one Dockerfile.cpu "${CPU_TAG}" "${CPU_PARENT}"
build_one Dockerfile.gpu "${GPU_TAG}" "${GPU_PARENT}"

echo "Running offline import checks"
docker run --rm --network none --entrypoint python3 "${CPU_TAG}" /opt/sie/selftest.py
docker run --rm --network none --entrypoint python3 "${GPU_TAG}" /opt/sie/selftest.py --gpu

mkdir -p "${ARCHIVE_DIR}"
save_archive() {
    local tag="$1" name="$2"
    local archive="${ARCHIVE_DIR}/${name}"
    local tmp="${archive}.tmp.$$"
    echo "Saving ${archive}"
    if command -v pigz >/dev/null 2>&1; then
        docker save "${tag}" | pigz -1 > "${tmp}"
    else
        docker save "${tag}" | gzip -1 > "${tmp}"
    fi
    local digest
    digest="$(sha256sum "${tmp}" | awk '{print $1}')"
    mv -f "${tmp}" "${archive}"
    printf '%s  %s\n' "${digest}" "${name}" > "${archive}.sha256"
    echo "${tag} $(docker image inspect --format '{{.Id}}' "${tag}") archive=${digest}"
}

save_archive "${CPU_TAG}" "sie-cpu-base-0.1.0-linux-amd64.tar.gz"
save_archive "${GPU_TAG}" "sie-gpu-base-0.1.0-cu128-linux-amd64.tar.gz"

python3 - "${ROOT}/manifest.json" "${CPU_TAG}" "${GPU_TAG}" "${CPU_PARENT}" "${GPU_PARENT}" "${EXPECTED_GPU_PARENT_ID}" "${ARCHIVE_DIR}" <<'PY'
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
manifest_path, cpu_tag, gpu_tag, cpu_parent, gpu_parent, gpu_parent_id, archive_dir = sys.argv[1:]
archive_dir = Path(archive_dir)

def image_id(tag):
    return subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True).strip()

def archive_record(name):
    path = archive_dir / name
    digest = (path.with_name(path.name + ".sha256")).read_text().split()[0]
    return {"file": "data/images/" + name, "sha256": digest, "bytes": path.stat().st_size}

manifest = {
    "schema_version": "1.0",
    "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "images": [
        {
            "role": "cpu-base",
            "tag": cpu_tag,
            "id": image_id(cpu_tag),
            "parent": cpu_parent,
            "archive": archive_record("sie-cpu-base-0.1.0-linux-amd64.tar.gz"),
        },
        {
            "role": "gpu-base",
            "tag": gpu_tag,
            "id": image_id(gpu_tag),
            "parent": gpu_parent,
            "parent_id": gpu_parent_id,
            "cuda": "12.8",
            "pytorch": "2.8.0+cu128",
            "archive": archive_record("sie-gpu-base-0.1.0-cu128-linux-amd64.tar.gz"),
        },
    ],
}
Path(manifest_path).write_text(json.dumps(manifest, indent=2) + "\n")
print("Wrote", manifest_path)
PY
echo "[PASS] Baseline images built"
