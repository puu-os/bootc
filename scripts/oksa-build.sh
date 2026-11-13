#!/usr/bin/env bash

# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

set -euo pipefail

: "${BOARD_DIR:?BOARD_DIR must be set}"
: "${HOST_DIR:?HOST_DIR must be set}"
: "${BUILDROOT_DL_DIR:?BUILDROOT_DL_DIR must be set}"
: "${PUU_ARCH:?PUU_ARCH must be set (amd64 or arm64)}"
: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH must be set}"
export SOURCE_DATE_EPOCH TZ=UTC

script_dir=$(cd "${BASH_SOURCE%/*}" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
oksa_src="${repo_root}/oksa"
state_helper="${script_dir}/state.py"
state_file="${BOARD_DIR}/stamp/orchestration/oksa.json"
overlay_dir="${BOARD_DIR}/oksa-overlay"
dl_dir="${BUILDROOT_DL_DIR}/oksa"
oksa_version="1.0"

shopt -s nullglob
python_pkg_dirs=("${BOARD_DIR}"/per-package/python-*/target)
if ((${#python_pkg_dirs[@]} == 0)); then
  echo "error: no python-* per-package target directories found in ${BOARD_DIR}/per-package" >&2
  exit 1
fi

runtime_packages=()
for dir in "${python_pkg_dirs[@]}"; do
   pkg_dir="${dir%/target}"
  runtime_packages+=("${pkg_dir##*/}")
done
IFS=' ' read -r -a runtime_packages <<< "$(printf '%s\n' "${runtime_packages[@]}" | LC_ALL=C sort | tr '\n' ' ')"
runtime_label="${runtime_packages[*]}"

inputs=(
  --exclude __pycache__ --exclude '*.pyc' --exclude dist
  --value "arch=${PUU_ARCH}"
  --value "epoch=${SOURCE_DATE_EPOCH}"
  --value "version=${oksa_version}"
  --value "runtime_packages=${runtime_label}"
  --path "${BASH_SOURCE[0]}"
  --path "${state_helper}"
  --path "${oksa_src}"
)
for tool in sloci-image skopeo; do
  tool_path="${HOST_DIR}/bin/${tool}"
  resolved=$(realpath -e "${tool_path}")
  inputs+=(--path "${tool_path}")
  [ "${tool_path}" = "${resolved}" ] || inputs+=(--path "${resolved}")
done
for dir in "${python_pkg_dirs[@]}"; do
  inputs+=(--path "${dir}")
done

key=$(python3 "${state_helper}" key "${inputs[@]}")
outputs=(
  --metadata-path "${overlay_dir}/usr/share/puu/k3s/images/oksa.tar"
  --path "${overlay_dir}/usr/share/puu/k3s/manifests/46-oksa.yaml"
)
if python3 "${state_helper}" check "${state_file}" --key "${key}" "${outputs[@]}"; then
  echo "oksa overlay is up to date"
  exit 0
else
  status=$?
  [ "${status}" = 1 ] || exit "${status}"
fi

mkdir -p "${dl_dir}" "${BOARD_DIR}/stamp/orchestration"
work=$(mktemp -d "${BOARD_DIR}/oksa-build.XXXXXX")
trap 'rm -rf -- "${work}"' EXIT
trap 'exit 1' HUP INT TERM

stage="${work}/overlay"
mkdir -p \
  "${stage}/usr/libexec/puu" \
  "${stage}/usr/share/puu/k3s/crds" \
  "${stage}/usr/share/puu/k3s/manifests" \
  "${stage}/usr/share/puu/k3s/images" \
  "${stage}/usr/share/puu/vllm/catalog.d" \
  "${stage}/usr/share/puu/vllm/runtime"

while IFS=$'\t' read -r dest_name src_name sha256 url; do
  [ -n "${dest_name}" ] || continue
  cached="${dl_dir}/${src_name}"
  if [ ! -f "${cached}" ] || ! echo "${sha256}  ${cached}" | sha256sum --status -c -; then
    "${script_dir}/download.sh" "${url}" "${sha256}" "${cached}"
  fi
  install -D -m 0644 "${cached}" "${stage}/usr/share/puu/k3s/crds/${dest_name}"
done < "${oksa_src}/crds.manifest"

for file in "${oksa_src}"/bin/*; do
  [[ -f "${file}" ]] || continue
  name="${file##*/}"
  if [[ "${name}" == oksa_*.py || "${name}" == oksa_*.css ]]; then
    install -D -m 0644 "${file}" "${stage}/usr/libexec/puu/${name}"
  else
    install -D -m 0755 "${file}" "${stage}/usr/libexec/puu/${name}"
  fi
done

install -D -m 0644 "${oksa_src}/dbus/io.github.puu_os.Oksa.conf" \
  "${stage}/usr/share/dbus-1/system.d/io.github.puu_os.Oksa.conf"
install -D -m 0644 "${oksa_src}/dbus/io.github.puu_os.Oksa.service" \
  "${stage}/usr/share/dbus-1/system-services/io.github.puu_os.Oksa.service"
install -D -m 0644 "${oksa_src}/oksa.conf" \
  "${stage}/usr/lib/tmpfiles.d/oksa-services.conf"

for manifest in "${oksa_src}"/manifests/*.yaml; do
  install -D -m 0644 "${manifest}" "${stage}/usr/share/puu/k3s/manifests/${manifest##*/}"
done
for catalog in "${oksa_src}"/vllm/catalog.d/*.env; do
  install -D -m 0644 "${catalog}" "${stage}/usr/share/puu/vllm/catalog.d/${catalog##*/}"
done
for runtime in "${oksa_src}"/vllm/runtime/*; do
  install -D -m 0644 "${runtime}" "${stage}/usr/share/puu/vllm/runtime/${runtime##*/}"
done
for unit in \
  puu-k3s-addons.service \
  puu-model-serving-labeler.service \
  puu-model-serving-labeler.timer \
  puu-vllm-planner.service \
  puu-vllm-planner.timer \
  puu-vllm-preseed.service \
  puu-postgresql.service \
  puu-oksa.service \
  puu-oksa-proxy.service; do
  install -D -m 0644 "${oksa_src}/systemd/${unit}" \
    "${stage}/usr/lib/systemd/system/${unit}"
done
install -D -m 0644 "${oksa_src}/systemd/postgresql.service.d/puu.conf" \
  "${stage}/usr/lib/systemd/system/postgresql.service.d/puu.conf"

web_rootfs="${work}/web-rootfs"
mkdir -p "${web_rootfs}"
for pkg in "${runtime_packages[@]}"; do
  rsync -a "${BOARD_DIR}/per-package/${pkg}/target/" "${web_rootfs}/"
done
rm -f "${web_rootfs}/THIS_IS_NOT_YOUR_ROOT_FILESYSTEM"
rm -rf \
  "${web_rootfs}/usr/include" \
  "${web_rootfs}/usr/lib/pkgconfig" \
  "${web_rootfs}/usr/lib/cmake" \
  "${web_rootfs}/usr/share/gir-1.0" \
  "${web_rootfs}/usr/share/doc" \
  "${web_rootfs}/usr/share/man" \
  "${web_rootfs}/etc/init.d" \
  "${web_rootfs}/etc/systemd" \
  "${web_rootfs}/usr/lib/systemd" \
  "${web_rootfs}/usr/lib/tmpfiles.d" \
  "${web_rootfs}/usr/share/dbus-1"
if [ -d "${web_rootfs}/usr/lib" ]; then
  find "${web_rootfs}/usr/lib" -type f \( -name '*.a' -o -name '*.la' \) -delete
fi
install -d -m 0755 "${web_rootfs}/usr/libexec/puu"
install -m 0755 "${stage}/usr/libexec/puu/oksa-web" "${web_rootfs}/usr/libexec/puu/"
install -m 0644 "${stage}/usr/libexec/puu"/oksa_*.py "${stage}/usr/libexec/puu"/oksa_*.css \
  "${web_rootfs}/usr/libexec/puu/"

tar -C "${web_rootfs}" --sort=name --mtime="@${SOURCE_DATE_EPOCH}" \
  --owner=0 --group=0 --numeric-owner -cf "${work}/web-rootfs.tar" .
gzip -n -f "${work}/web-rootfs.tar"

"${HOST_DIR}/bin/sloci-image" --arch "${PUU_ARCH}" --user 1000:1000 \
  --env PATH=/usr/bin:/bin --env HOME=/tmp --working-dir /tmp \
  --env PYTHONDONTWRITEBYTECODE=1 --port 8088 \
  --entrypoint /usr/bin/python3 --entrypoint /usr/libexec/puu/oksa-web \
  --label "io.github.puu_os.oksa.runtime-packages=${runtime_label}" \
  "${work}/web-rootfs.tar.gz" "${work}/web-oci:${oksa_version}"

"${HOST_DIR}/bin/skopeo" --insecure-policy copy --preserve-digests \
  "oci:${work}/web-oci:${oksa_version}" \
  "oci:${work}/web-export:localhost/puu/oksa:${oksa_version}"

tar -C "${work}/web-export" --sort=name --mtime="@${SOURCE_DATE_EPOCH}" \
  --owner=0 --group=0 --numeric-owner \
  -cf "${stage}/usr/share/puu/k3s/images/oksa.tar" blobs index.json oci-layout

digest=$("${HOST_DIR}/bin/skopeo" inspect --format '{{.Digest}}' "oci:${work}/web-oci:${oksa_version}")
sed -i "s|image: localhost/puu/oksa:.*|image: localhost/puu/oksa@${digest}|" \
  "${stage}/usr/share/puu/k3s/manifests/46-oksa.yaml"

rm -rf -- "${overlay_dir}"
mv -T "${stage}" "${overlay_dir}"
python3 "${state_helper}" save "${state_file}" --key "${key}" "${outputs[@]}"
