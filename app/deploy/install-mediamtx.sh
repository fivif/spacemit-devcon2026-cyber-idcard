#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="${MEDIAMTX_VERSION:-v1.20.1}"
TAG="${MEDIAMTX_TAG:-${VERSION}-riscv64}"
BASE_URL="https://github.com/Fitz8863/spacemit-mediamtx/releases/download/${TAG}"
ARCHIVE="mediamtx_${VERSION}_linux_riscv64.tar.gz"
ROOT="/opt/devcon/mediamtx"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ "$(id -u)" != "0" ]]; then
  echo "请使用 root 运行。" >&2
  exit 1
fi

mkdir -p "$ROOT"
curl -fL --retry 3 --connect-timeout 10 "$BASE_URL/$ARCHIVE" -o "$TMP/$ARCHIVE"
tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
install -m 0755 "$TMP/mediamtx_${VERSION}_linux_riscv64/mediamtx" "$ROOT/mediamtx"
install -m 0644 "$(dirname "$0")/mediamtx.yml" "$ROOT/mediamtx.yml"
install -m 0644 "$(dirname "$0")/devcon-mediamtx.service" /etc/systemd/system/devcon-mediamtx.service

systemctl daemon-reload
systemctl enable --now devcon-mediamtx.service
systemctl is-active --quiet devcon-mediamtx.service
echo "MediaMTX 已启动：RTSP 8554，WebRTC 8889，媒体 UDP/TCP 8189。"
