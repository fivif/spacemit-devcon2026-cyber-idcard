#!/usr/bin/env bash
# 装置 5 · 赛博开发者证件照 —— 一键部署（幂等、可重复执行）
#
# 干什么：把一份 app/ 源码同步到 /opt/devcon/app → 装 MediaMTX 与 systemd 单元
#          → 重启服务 → 探活。
#
# ★ 安全底线（现场数据不许被一键抹掉）：
#   同步**之前**先把 data/ 里的 records.jsonl 与 counter.json 复制到
#   /opt/devcon/backup/<时间戳>/data/ ，并额外打包整个 data/ 与当前代码树。
#   同步**永不**删 data/（--exclude data），也永不碰 assets/models
#   （模型单独放在 /opt/devcon/assets/models，本脚本不负责搬模型）。
#
# 在设备上（推荐，现场就这么干）：
#   bash /opt/devcon/app/tools/deploy.sh --source /opt/devcon/staging/app --install-unit
# 在有 ssh + rsync 的开发机上（Windows 上用仓库自带的 k3.py 助手更省事）：
#   bash tools/deploy.sh --ssh root@192.0.2.10 --source ./app --install-unit
#
# 常用参数：
#   --source DIR   源码目录（不给就默认用脚本所在仓库的 app/）
#   --target DIR   目标目录（默认 /opt/devcon/app）
#   --prune        rsync --delete（删掉目标里源码已没有的文件；默认不删）
#   --no-restart   只同步，不重启服务
#   --no-backup    不做备份（**不要**在现场用；只在确认数据无关时用）
#   --dry-run      只打印要做什么
#   -h / --help    帮助

set -euo pipefail

SOURCE=""
TARGET="/opt/devcon/app"
BACKUP_DIR="/opt/devcon/backup"
STAGING_REMOTE="/opt/devcon/staging/app"
SSH_HOST=""
INSTALL_UNIT=0
PRUNE=0
RESTART=1
BACKUP=1
DRY_RUN=0
PORT="${BOOTH_PORT:-8090}"
SERVICE="devcon-booth.service"
MEDIA_SERVICE="devcon-mediamtx.service"
MEDIA_ROOT="/opt/devcon/mediamtx"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn  ]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail  ]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '  [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

have()  { command -v "$1" >/dev/null 2>&1; }
fsize() { if [[ -f "$1" ]]; then stat -c %s "$1"; else echo "?"; fi; }

usage() {
  cat <<'USAGE'
装置 5 · 赛博开发者证件照 —— 一键部署（幂等、可重复执行）

干什么：把一份 app/ 同步到目标目录 → 装 MediaMTX 与 systemd 单元 → 重启服务 → 探活。

★ 安全底线（现场数据不许被一键抹掉）：
  同步**之前**先把 data/ 里的 records.jsonl 与 counter.json 复制到
  /opt/devcon/backup/<时间戳>/data/，并额外打包整个 data/ 与当前代码树。
  同步**永不**删 data/（--exclude data），也永不碰 assets/models。

在设备上：
  bash /opt/devcon/app/tools/deploy.sh --source /opt/devcon/staging/app --install-unit
在有 ssh + rsync 的开发机上：
  bash tools/deploy.sh --ssh root@192.0.2.10 --source ./app --install-unit

参数：
  --source DIR    源码目录（不给就默认用脚本所在仓库的 app/）
  --target DIR    目标目录（默认 /opt/devcon/app）
  --backup-dir D  备份根目录（默认 /opt/devcon/backup）
  --ssh HOST      开发机模式：[user@]host，先 rsync 到设备，再由设备侧脚本干真活
  --install-unit  强制重装 systemd 单元（默认：不存在或有差异时才装）
  --prune         同步时 rsync --delete（默认不删）
  --no-restart    只同步，不重启服务
  --no-backup     跳过备份（现场慎用）
  --dry-run       只打印要做什么
  -h / --help     本帮助

退出码：0 = 同步/重启/探活都过了；非 0 = 中间有一步没过。
USAGE
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)     SOURCE="${2:?--source 需要一个目录}"; shift 2 ;;
    --target)     TARGET="${2:?--target 需要一个目录}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?}"; shift 2 ;;
    --ssh)        SSH_HOST="${2:?--ssh 需要 user@host}"; shift 2 ;;
    --install-unit) INSTALL_UNIT=1; shift ;;
    --prune)      PRUNE=1; shift ;;
    --no-restart) RESTART=0; shift ;;
    --no-backup)  BACKUP=0; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage ;;
    *)            die "不认识的参数：$1（--help 看用法）" ;;
  esac
done

if [[ -z "${SOURCE}" ]]; then
  if [[ -f "${REPO_APP_DIR}/booth_server.py" ]]; then SOURCE="${REPO_APP_DIR}"
  else die "没给 --source，而且 ${REPO_APP_DIR} 里也没有 booth_server.py"; fi
fi
[[ -d "${SOURCE}" ]] || die "源码目录不存在：${SOURCE}"
SOURCE="$(cd "${SOURCE}" && pwd)"

check_source() {
  [[ -f "${SOURCE}/booth_server.py" ]] || die "${SOURCE} 不像 app/（缺 booth_server.py）"
  [[ -f "${SOURCE}/tools/preflight.py" ]] || warn "${SOURCE}/tools/preflight.py 不在，预检会装不上"
  [[ -f "${SOURCE}/deploy/devcon-booth.service" ]] || warn "${SOURCE}/deploy/devcon-booth.service 不在，无法安装单元"
  log "源码 ${SOURCE} → 目标 ${TARGET}"
}
backup_everything() {
  # ★ 同步前保护：data/ 是现场唯一的不可再生资产（已拍的照片、编号、同意记录）。
  [[ "${BACKUP}" == "1" ]] || { warn "--no-backup：跳过备份（现场慎用！）"; return 0; }
  STAMP="$(date +%Y%m%d-%H%M%S)"
  SNAP="${BACKUP_DIR}/${STAMP}"
  log "同步前备份 → ${SNAP}"
  run mkdir -p "${SNAP}/data"

  local names=(records.jsonl counter.json)
  if [[ -d "${TARGET}/data" ]]; then
    for name in "${names[@]}"; do
      if [[ -f "${TARGET}/data/${name}" ]]; then
        run cp -a "${TARGET}/data/${name}" "${SNAP}/data/${name}"
        log "  保住了 data/${name}（$(fsize "${TARGET}/data/${name}") B）"
      fi
    done
    # 整包一遍（含 photos/ thumbs/ captures/ masks/ qr/），出问题能整棵还原
    run tar czf "${SNAP}/data.tar.gz" -C "${TARGET}" data
    log "  整包 $(fsize "${SNAP}/data.tar.gz") B → data.tar.gz"
  else
    warn "  ${TARGET}/data 还不存在（首次部署？）"
  fi

  # 顺手把当前代码树也存一份，用于「新版本有问题时退回上一版」
  if [[ -f "${TARGET}/booth_server.py" ]]; then
    run tar czf "${SNAP}/app-code.tar.gz" \
      --exclude=data --exclude=__pycache__ --exclude='*.pyc' \
      -C "${TARGET}" .
    log "  旧代码 $(fsize "${SNAP}/app-code.tar.gz") B → app-code.tar.gz"
  fi
  if [[ -d "${SNAP}" ]]; then
    ( cd "${SNAP}" && sha256sum data/*.json data/*.jsonl 2>/dev/null > SHA256SUMS || true )
    printf '%s\n' "${SNAP}" > "${BACKUP_DIR}/LAST" 2>/dev/null || true
  fi
  log "备份完成（回滚命令见本轮输出末尾）"
}

sync_tree() {
  # 代码同步。data/ 与 assets/models 永远排除在外。
  local excludes=(--exclude=__pycache__ --exclude='*.pyc' --exclude=data
                  --exclude='assets/models' --exclude='.git' --exclude='.DS_Store')
  run mkdir -p "${TARGET}"
  if have rsync; then
    local flags=(-a --human-readable)
    [[ "${PRUNE}" == "1" ]] && flags+=(--delete)
    run rsync "${flags[@]}" "${excludes[@]}" "${SOURCE}/" "${TARGET}/"
  else
    warn "没有 rsync，改用 tar 直传（不支持 --prune）"
    local tar_ex=(--exclude=__pycache__ --exclude='*.pyc' --exclude=data
                  --exclude='assets/models' --exclude=.git)
    run bash -c "tar -C '${SOURCE}' -cf - ${tar_ex[*]} . | tar -C '${TARGET}' -xf -"
  fi
  log "同步完成"
}

verify_after_sync() {
  [[ -f "${TARGET}/booth_server.py" ]] || die "同步后 ${TARGET}/booth_server.py 不见了 —— 立刻回滚"
  [[ -f "${TARGET}/data/counter.json" || ! -d "${TARGET}/data" || "${DRY_RUN}" == "1" ]] \
    || warn "data/counter.json 没了（理论上不该发生，请检查备份）"
  local files total
  files="$(find "${TARGET}" -name '*.py' -not -path '*/__pycache__/*' | wc -l)"
  total="$(du -sh "${TARGET}" 2>/dev/null | cut -f1)"
  log "目标现状：${files} 个 .py · 共 ${total} · booth_server.py sha256=$(sha256sum "${TARGET}/booth_server.py" | cut -c1-16)…"
}

install_unit() {
  local src="${SOURCE}/deploy/devcon-booth.service"
  local dst="/etc/systemd/system/${SERVICE}"
  [[ -f "${src}" ]] || { warn "没有 ${src}，跳过单元安装"; return 0; }
  if [[ ! -f "${dst}" ]] || ! cmp -s "${src}" "${dst}" || [[ "${INSTALL_UNIT}" == "1" ]]; then
    log "安装 systemd 单元 → ${dst}"
    run install -m 0644 -o root -g root "${src}" "${dst}"
    run systemctl daemon-reload
  else
    log "systemd 单元已是最新（${dst}）"
  fi
  if ! systemctl is-enabled --quiet "${SERVICE}" 2>/dev/null; then
    log "设为开机自启：systemctl enable ${SERVICE}"
    run systemctl enable "${SERVICE}"
  fi

  local preview_src="${SOURCE}/deploy/k3-preview-smooth.conf"
  local preview_dir="/etc/systemd/system/${SERVICE}.d"
  local preview_dst="${preview_dir}/zzzzz-devcon-webrtc.conf"
  if [[ -f "${preview_src}" ]]; then
    run mkdir -p "${preview_dir}"
    if [[ ! -f "${preview_dst}" ]] || ! cmp -s "${preview_src}" "${preview_dst}"; then
      log "安装 K3 WebRTC 高分辨率覆盖 → ${preview_dst}"
      run install -m 0644 -o root -g root "${preview_src}" "${preview_dst}"
      run systemctl daemon-reload
    fi
  fi
}

install_media_unit() {
  local media_src="${SOURCE}/deploy"
  local config_src="${media_src}/mediamtx.yml"
  local service_src="${media_src}/${MEDIA_SERVICE}"
  [[ -f "${config_src}" && -f "${service_src}" ]] || die "缺少 MediaMTX 配置或 systemd 单元"

  if [[ ! -x "${MEDIA_ROOT}/mediamtx" ]]; then
    [[ -f "${media_src}/install-mediamtx.sh" ]] || die "缺少 install-mediamtx.sh"
    log "K3 未安装 MediaMTX，按 spacemit-mediamtx 官方 riscv64 包安装"
    run bash "${media_src}/install-mediamtx.sh"
    return 0
  fi

  run mkdir -p "${MEDIA_ROOT}"
  local changed=0
  if [[ ! -f "${MEDIA_ROOT}/mediamtx.yml" ]] || ! cmp -s "${config_src}" "${MEDIA_ROOT}/mediamtx.yml"; then
    run install -m 0644 -o root -g root "${config_src}" "${MEDIA_ROOT}/mediamtx.yml"
    changed=1
  fi
  if [[ ! -f "/etc/systemd/system/${MEDIA_SERVICE}" ]] || ! cmp -s "${service_src}" "/etc/systemd/system/${MEDIA_SERVICE}"; then
    run install -m 0644 -o root -g root "${service_src}" "/etc/systemd/system/${MEDIA_SERVICE}"
    run systemctl daemon-reload
    changed=1
  fi
  if ! systemctl is-enabled --quiet "${MEDIA_SERVICE}" 2>/dev/null; then
    run systemctl enable "${MEDIA_SERVICE}"
  fi
  if ! systemctl is-active --quiet "${MEDIA_SERVICE}" 2>/dev/null; then
    run systemctl start "${MEDIA_SERVICE}"
  elif [[ "${changed}" == "1" && "${RESTART}" == "1" ]]; then
    run systemctl restart "${MEDIA_SERVICE}"
  fi
}

restart_service() {
  [[ "${RESTART}" == "1" ]] || { warn "--no-restart：服务未重启（改动要等下次重启才生效）"; return 0; }
  log "重启 ${SERVICE}"
  run systemctl restart "${SERVICE}"
  run systemctl --no-pager --lines=0 status "${SERVICE}" || true
  if [[ "${DRY_RUN}" != "1" ]] && ! systemctl is-active --quiet "${SERVICE}"; then
    warn "${SERVICE} 不是 active —— 看 journalctl -u ${SERVICE} -n 60 --no-pager"
  fi
}

wait_for_port() {
  local i
  for i in $(seq 1 40); do
    if (exec 3<>/dev/tcp/127.0.0.1/"${PORT}") 2>/dev/null; then
      exec 3<&- 2>/dev/null || true
      log "${PORT} 已监听（等了 ${i} 次探测）"
      return 0
    fi
    sleep 1
  done
  warn "${PORT} 40s 内没起来，看日志：journalctl -u ${SERVICE} -n 80 --no-pager"
  return 1
}

probe_service() {
  # 认门牌再认人：8090 上响应的**必须**是本服务，否则「探活成功」就是假阳性
  # （现场真踩过：另一个进程 camd.py 占着 8090，curl 一样会返回 JSON）。
  have curl || { warn "没有 curl，跳过探活"; return 0; }
  local health
  if ! health="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null)"; then
    warn "探活失败：/api/health 取不到 —— 看 journalctl -u ${SERVICE} -n 80 --no-pager"
    return 1
  fi
  case "${health}" in
    *devcon5*) log "探活 OK（/api/health）：${health}" ;;
    *) warn "端口 ${PORT} 上响应的不是本服务：${health:0:160}"
       warn "  很可能被别的进程占着：ss -ltnp | grep ${PORT}"
       return 1 ;;
  esac
  local state
  state="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/api/state" 2>/dev/null || true)"
  [[ -n "${state}" ]] && log "状态：$(printf '%s' "${state}" | head -c 300)"
  return 0
}

deploy_remote() {
  have ssh   || die "--ssh 模式需要本机有 ssh"
  have rsync || die "--ssh 模式需要本机有 rsync"
  log "开发机模式：${SOURCE}/ → ${SSH_HOST}:${STAGING_REMOTE}/"
  run ssh "${SSH_HOST}" "mkdir -p '${STAGING_REMOTE}'"
  run rsync -az --delete --exclude=__pycache__ --exclude='*.pyc' --exclude=data \
      --exclude='assets/models' -e ssh "${SOURCE}/" "${SSH_HOST}:${STAGING_REMOTE}/"
  # 真正干活的是设备上那份 deploy.sh（备份 / 同步 / 装单元 / 重启 全在设备侧跑）
  local opts="--source '${STAGING_REMOTE}' --target '${TARGET}'"
  [[ "${INSTALL_UNIT}" == "1" ]] && opts+=" --install-unit"
  [[ "${PRUNE}" == "1" ]]        && opts+=" --prune"
  [[ "${RESTART}" == "0" ]]      && opts+=" --no-restart"
  [[ "${BACKUP}" == "0" ]]       && opts+=" --no-backup"
  [[ "${DRY_RUN}" == "1" ]]      && opts+=" --dry-run"
  run ssh "${SSH_HOST}" "bash '${STAGING_REMOTE}/tools/deploy.sh' ${opts}"
}

summary() {
  echo
  log "部署完成"
  echo "  预检：/usr/bin/python3 ${TARGET}/tools/preflight.py"
  echo "  日志：journalctl -u ${SERVICE} -f"
  echo "  大屏：chromium --kiosk --app=http://127.0.0.1:${PORT}/stage"
  if [[ -n "${SNAP}" ]]; then
    echo "  回滚代码：tar xzf ${SNAP}/app-code.tar.gz -C ${TARGET} && systemctl restart ${SERVICE}"
    echo "  回滚数据：tar xzf ${SNAP}/data.tar.gz -C ${TARGET}"
    echo "  只回记录：cp -a ${SNAP}/data/records.jsonl ${SNAP}/data/counter.json ${TARGET}/data/"
  fi
}

main() {
  check_source
  if [[ -n "${SSH_HOST}" ]]; then
    deploy_remote
    return 0
  fi
  have systemctl || die "没有 systemctl：本脚本是给设备（systemd 主机）用的"
  if [[ "${DRY_RUN}" != "1" && "${EUID}" != "0" ]]; then
    die "请用 root 运行（要写 ${TARGET} 与 /etc/systemd/system）"
  fi
  backup_everything
  sync_tree
  verify_after_sync
  install_media_unit
  install_unit
  restart_service
  if [[ "${RESTART}" == "1" && "${DRY_RUN}" != "1" ]]; then
    wait_for_port || true
    probe_service || true
  fi
  summary
}

SNAP=""
STAMP=""
main "$@"
