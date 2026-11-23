#!/bin/sh
set -eu
apt_cache=/var/lib/vllm/prism-ternary/apt
mkdir -p "$apt_cache/lists/partial" "$apt_cache/archives/partial"
if [ ! -f "$apt_cache/.ready" ]; then
    apt-get -o Dir::State::lists="$apt_cache/lists" update
fi
apt-get -o Dir::State::lists="$apt_cache/lists" \
    -o Dir::Cache::archives="$apt_cache/archives" \
    -o APT::Keep-Downloaded-Packages=true install -y --no-install-recommends \
    libcublas-dev-13-0 libcusparse-dev-13-0 libcusolver-dev-13-0
touch "$apt_cache/.ready"
target=$(mktemp -d)
python3 -m pip install --no-deps --break-system-packages --target "$target" \
    https://github.com/fraserprice/bonsai-vllm/archive/e5ed2454a6b06a857a2146ba8cc60b7814c54bdf.tar.gz
export PYTHONPATH="$target${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m vllm.entrypoints.openai.api_server "$@"
