#!/usr/bin/env bash
# =============================================================================
#  AI Passport · B 站客户端  —— 服务端一键部署脚本
#
#  支持发行版：Debian/Ubuntu、Fedora、RHEL/CentOS、Arch、openSUSE、Alpine
#  用法：
#     sudo ./deploy/install.sh              # 安装 + 交互式配置 + 注册服务
#     ./deploy/install.sh --setup-only      # 只装依赖和写配置，不启动
#     ./deploy/install.sh --check           # 只体检，不安装
#     ./deploy/install.sh --update          # 从 GitHub 拉最新代码并重启（配置不动）
#     ./deploy/install.sh --show-pairing    # 查看 4 位配对码
#     ./deploy/install.sh --uninstall       # 卸载
#
#  设计原则：不污染系统、不注册后台服务除非你同意、已存在的配置永不覆盖。
# =============================================================================
set -euo pipefail

APP_NAME="passport-bili"
VER="0.1.0"

# ---------- 颜色与日志 -------------------------------------------------------
if [ -t 1 ]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
  C_B=$'\033[36m'; C_D=$'\033[2m'; C_0=$'\033[0m'
else
  C_R=''; C_G=''; C_Y=''; C_B=''; C_D=''; C_0=''
fi
step() { printf '\n%s[%s]%s %s\n' "$C_B" "$1" "$C_0" "$2"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '    %s✗%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

# ---------- 路径 -------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SERVICE_SRC="$REPO_ROOT/service"

if [ "$(id -u)" -eq 0 ]; then
  PREFIX="${PREFIX:-/opt/$APP_NAME}"
  CONF_DIR="${CONF_DIR:-/etc/$APP_NAME}"
else
  PREFIX="${PREFIX:-$HOME/.local/share/$APP_NAME}"
  CONF_DIR="${CONF_DIR:-$HOME/.config/$APP_NAME}"
fi
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 这三个是实测调出来的，不是拍脑袋的默认值，别随手改大：
#   TV_FPS=12        C3 要 inflate 15 个条带再扩成 RGB565 刷屏，12~15 是舒适区，
#                    18 以上设备侧跟不上，表现为画面一顿一顿。
#   TV_ZLIB_LEVEL=3  条带压缩级别。默认 6 时 2 核 VPS 的 CPU 会被压满（每秒 200 多次
#                    zlib），降到 3 速度快一倍、体积只大 10~15%。
#   TV_AUDIO=1       关掉后不下发音频包，但**播放会卡在彩条上**——固件要等音频时钟
#                    建立才肯开画，所以只能做设备端静音，不能停发。
TV_FPS="${TV_FPS:-12}"
TV_ZLIB_LEVEL="${TV_ZLIB_LEVEL:-3}"
TV_AUDIO="${TV_AUDIO:-1}"
MODE="install"; SETUP_ONLY=0; ASSUME_YES=0

usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# ---------- 参数 -------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --check)        MODE=check; shift ;;
    --update)       MODE=update; shift ;;
    --setup-only)   SETUP_ONLY=1; shift ;;
    --show-pairing) MODE=show; shift ;;
    --uninstall)    MODE=uninstall; shift ;;
    --prefix)       PREFIX="$2"; shift 2 ;;
    --conf)         CONF_DIR="$2"; shift 2 ;;
    --mirror)       PIP_INDEX="$2"; shift 2 ;;
    --yes|-y)       ASSUME_YES=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "未知参数: $1（--help 查看用法）" ;;
  esac
done

RUN_USER="${SUDO_USER:-$(id -un)}"
ENV_FILE="$CONF_DIR/env"

# ---------- 交互小工具 -------------------------------------------------------
# ask "提示" "默认值"  -> 回显到 stdout（提示写 stderr，便于 $(...) 捕获）
ask() {
  local prompt="$1" def="${2:-}" ans=""
  if [ -n "$def" ]; then printf '    %s [%s]: ' "$prompt" "$def" >&2
  else printf '    %s: ' "$prompt" >&2; fi
  if [ "$ASSUME_YES" -eq 1 ]; then printf '\n' >&2; return 0; fi
  read -r ans || ans=""
  printf '%s' "${ans:-$def}"
}
# 隐藏输入
ask_secret() {
  local prompt="$1" ans=""
  printf '    %s: ' "$prompt" >&2
  if [ "$ASSUME_YES" -eq 1 ]; then printf '\n' >&2; return 0; fi
  read -rs ans || ans=""
  printf '\n' >&2
  printf '%s' "$ans"
}
confirm() {
  local prompt="$1" def="${2:-n}" ans
  [ "$ASSUME_YES" -eq 1 ] && return 0
  ans=$(ask "$prompt (y/n)" "$def")
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---------- 包管理器 ---------------------------------------------------------
PM=""
detect_pm() {
  local c
  for c in apt-get dnf yum pacman zypper apk; do
    if command -v "$c" >/dev/null 2>&1; then PM="$c"; return 0; fi
  done
  PM=""
}
run_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
pm_install() {
  [ -n "$PM" ] || die "未识别包管理器，请手动安装 python3 / ffmpeg 后重试"
  case "$PM" in
    apt-get) run_root apt-get update -qq
             run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf)     run_root dnf install -y -q "$@" ;;
    yum)     run_root yum install -y -q "$@" ;;
    pacman)  run_root pacman -Sy --noconfirm --needed "$@" ;;
    zypper)  run_root zypper -q install -y "$@" ;;
    apk)     run_root apk add --no-cache "$@" ;;
  esac
}

# RHEL/Fedora/Anolis 源里没有 ffmpeg（专利原因），改装静态编译版
install_ffmpeg_static() {
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    return 0
  fi
  warn "软件源里没有 ffmpeg，改装静态编译版到 /usr/local/bin"
  local tmp url d
  tmp="$(mktemp -d)"
  # 优先 BtbN 的 GPL 构建：johnvansickle 的 7.0.2 静态版在本机的 TLS 失败路径上会
  # 段错误（core dump），排查时只看到"ffmpeg dumped core"，很难定位。
  for url in \
    "https://dl.espressif.cn/github_assets/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
  do
    info "尝试：$url"
    if curl -fsSL --max-time 600 -o "$tmp/ff.tar.xz" "$url"; then break; fi
  done
  if [ ! -s "$tmp/ff.tar.xz" ]; then
    warn "ffmpeg 下载失败，请手动安装后重跑（--check 可体检）"
    rm -rf "$tmp"; return 1
  fi
  tar -xf "$tmp/ff.tar.xz" -C "$tmp" || { rm -rf "$tmp"; return 1; }
  d="$(find "$tmp" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
  if [ -x "$d/ffmpeg" ]; then
    run_root install -m 755 "$d/ffmpeg" "$d/ffprobe" /usr/local/bin/
  else
    run_root install -m 755 "$d/bin/ffmpeg" "$d/bin/ffprobe" /usr/local/bin/
  fi
  rm -rf "$tmp"; hash -r
  # 静态版不认系统证书路径，补一个软链；stream.py 还会用 -ca_file 显式指定。
  if [ -f /etc/pki/tls/certs/ca-bundle.crt ] && [ ! -e /etc/ssl/certs/ca-certificates.crt ]; then
    run_root mkdir -p /etc/ssl/certs
    run_root ln -sf /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt 2>/dev/null || true
  fi
  ok "ffmpeg 静态版安装完成"
}

install_deps() {
  detect_pm
  info "包管理器：${PM:-未识别}"
  case "$PM" in
    apt-get) pm_install python3 python3-venv python3-pip ffmpeg curl ca-certificates git ;;
    dnf|yum) pm_install python3 python3-pip curl ca-certificates git \
               || die "基础依赖安装失败"
             if ! find_py >/dev/null 2>&1; then
               warn "系统 python3 版本低于 3.10，补装 python3.11"
               pm_install python3.11 python3.11-pip python3.11-devel \
                 || warn "python3.11 安装失败，请手动处理"
             fi
             install_ffmpeg_static || true ;;
    pacman)  pm_install python ffmpeg curl ca-certificates git ;;
    zypper)  pm_install python3 python3-pip ffmpeg curl ca-certificates git ;;
    apk)     pm_install python3 py3-pip ffmpeg curl ca-certificates git ;;
    *)       die "不支持的发行版，请手动安装 python3(>=3.10)、ffmpeg、curl" ;;
  esac
}

# ---------- 体检 -------------------------------------------------------------
PY=""
# 依次尝试更高的解释器版本，系统 python3 太旧时自动让位给 python3.11/3.12
find_py() {
  local c
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"
      return 0
    fi
  done
  return 1
}
check_py() {
  find_py || { warn "未找到 >= 3.10 的 Python"; return 1; }
  ok "Python $("$PY" -c 'import platform;print(platform.python_version())')  ($PY)"
}
check_ffmpeg() {
  command -v ffmpeg  >/dev/null 2>&1 || { warn "未找到 ffmpeg"; return 1; }
  command -v ffprobe >/dev/null 2>&1 || { warn "未找到 ffprobe"; return 1; }
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
}
check_venv() {
  "$PY" -m venv --help >/dev/null 2>&1 || { warn "python3-venv 缺失"; return 1; }
  ok "venv 可用"
}
do_check() {
  step "体检" "检查运行环境"
  local bad=0
  check_py || bad=1
  [ "$bad" -eq 0 ] && { check_venv || bad=1; }
  check_ffmpeg || bad=1
  if [ "$bad" -ne 0 ]; then
    warn "环境不完整，可以用 sudo 权限重跑本脚本自动安装"
    return 1
  fi
  ok "检查通过"
}

# ---------- 卸载 / 查看配对码 -------------------------------------------------
do_uninstall() {
  step "卸载" "清理 $APP_NAME"
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl list-unit-files 2>/dev/null | grep -q "^$APP_NAME.service"; then
    run_root systemctl disable --now "$APP_NAME" >/dev/null 2>&1 || true
    run_root rm -f "/etc/systemd/system/$APP_NAME.service"
    run_root systemctl daemon-reload || true
    ok "已移除 systemd 服务"
  fi
  printf '    将删除：%s  和  %s\n' "$PREFIX" "$CONF_DIR"
  confirm "确认删除" n || { info "已取消"; exit 0; }
  run_root rm -rf "$PREFIX" "$CONF_DIR"
  ok "已卸载"
  exit 0
}

do_show_pairing() {
  [ -f "$ENV_FILE" ] || die "未找到配置：$ENV_FILE（先跑一次安装）"
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  printf '\n    配对码：%s%s%s\n' "$C_B" "${TV_PAIRING_CODE:-????}" "$C_0"
  printf '    服务端口：%s\n' "${TV_PORT:-10745}"
  printf '    B 站账号：%s\n\n' "$([ -n "${BILI_SESSDATA:-}" ] && echo 已配置 || echo 游客模式)"
  exit 0
}

# ---------- 生成随机串 -------------------------------------------------------
gen() { "$PY" -c "import secrets,string,sys; n=int(sys.argv[1]); a=sys.argv[2]; print(''.join(secrets.choice(a) for _ in range(n)))" "$1" "$2"; }

# ---------- 写配置 -----------------------------------------------------------
write_env() {
  install -d -m 750 "$CONF_DIR"
  install -d -m 700 "$CONF_DIR/data"
  {
    printf '# AI Passport B站服务端配置 · 由 install.sh 于 %s 生成\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf '# 权限 600，改完重启服务生效\n'
    printf 'BILI_SESSDATA=%s\n'      "${BILI_SESSDATA:-}"
    printf 'BILI_BILI_JCT=%s\n'      "${BILI_BILI_JCT:-}"
    printf 'BILI_DEDEUSERID=%s\n'    "${BILI_DEDEUSERID:-}"
    printf 'BILI_BUVID3=%s\n'        "${BILI_BUVID3:-}"
    printf 'TV_BIND=%s\n'            "$TV_BIND"
    printf 'TV_PORT=%s\n'            "$TV_PORT"
    printf 'TV_PAIRING_CODE=%s\n'    "$TV_PAIRING_CODE"
    printf 'TV_PAIRING_TOKEN=%s\n'   "$TV_PAIRING_TOKEN"
    printf 'TV_QUALITY=%s\n'         "$TV_QUALITY"
    printf 'TV_FPS=%s\n'             "$TV_FPS"
    printf 'TV_ZLIB_LEVEL=%s\n'      "$TV_ZLIB_LEVEL"
    printf 'TV_AUDIO=%s\n'           "$TV_AUDIO"
    printf 'TV_MAX_SESSIONS=2\n'
    printf 'TV_STATE_DIR=%s\n'       "$CONF_DIR/data"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  # 注意：不能写成 `[ ... ] && [ ... ] && chown`。以 root 直连时第二个条件为假，
  # 整条链返回 1，配合 set -e 会让脚本静默退出（踩过一次）。
  if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
    chown "$RUN_USER" "$ENV_FILE"
  fi
  ok "配置写入 $ENV_FILE"
}

# ---------- 交互式配置向导 ---------------------------------------------------
wizard() {
  step "配置" "服务参数"
  TV_BIND=$(ask "监听地址（0.0.0.0 = 局域网可访问）" "0.0.0.0")
  TV_PORT=$(ask "服务端口" "10745")
  case "$(ask "默认画质 smooth/standard/ultra" "standard")" in
    smooth|SMOOTH|s)     TV_QUALITY=smooth ;;
    ultra|ULTRA|u)       TV_QUALITY=ultra ;;
    *)                   TV_QUALITY=standard ;;
  esac
  TV_PAIRING_CODE=$(ask "4 位配对码（回车自动生成）" "")
  [ -n "$TV_PAIRING_CODE" ] || TV_PAIRING_CODE=$(gen 4 "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
  TV_PAIRING_CODE=$(printf '%s' "$TV_PAIRING_CODE" | tr '[:lower:]' '[:upper:]')
  TV_PAIRING_TOKEN=$(gen 64 "0123456789abcdef")

  step "配置" "B 站账号（可选，但收藏夹/私信必须有）"
  info "获取方式：浏览器登录 bilibili.com → F12 → Application → Cookies"
  info "游客模式下推荐/播放/评论可用，收藏、收藏夹、私信会显示“需登录”"
  BILI_SESSDATA=$(ask_secret "SESSDATA（留空 = 游客模式）")
  BILI_SESSDATA=$(printf '%s' "$BILI_SESSDATA" | tr -d '\r\n')
  if [ -n "$BILI_SESSDATA" ]; then
    BILI_BILI_JCT=$(ask_secret   "bili_jct")
    BILI_DEDEUSERID=$(ask_secret "DedeUserID")
    BILI_BUVID3=$(ask_secret     "buvid3（可留空）")
    BILI_BILI_JCT=$(printf '%s' "$BILI_BILI_JCT" | tr -d '\r\n')
    BILI_DEDEUSERID=$(printf '%s' "$BILI_DEDEUSERID" | tr -d '\r\n')
    BILI_BUVID3=$(printf '%s' "$BILI_BUVID3" | tr -d '\r\n')
    [ -n "$BILI_BILI_JCT" ] && [ -n "$BILI_DEDEUSERID" ] \
      || warn "bili_jct / DedeUserID 缺失，收藏类功能可能失败"
  else
    BILI_BILI_JCT=""; BILI_DEDEUSERID=""; BILI_BUVID3=""
  fi

  write_env
  printf '\n'
  info "配对码：${C_B}${TV_PAIRING_CODE}${C_0}（设备配网时填这个，--show-pairing 随时可查）"
}

# ---------- 小内存机器的 swap 兜底 -------------------------------------------
# 1G/2G 的轻量云在 pip 安装阶段极易被 OOM Killer 杀掉，先垫一块 swap。
ensure_swap() {
  local mem_mb
  mem_mb=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 99999)
  if [ "${mem_mb:-99999}" -ge 1600 ]; then
    ok "内存 ${mem_mb}MB，无需 swap"
    return 0
  fi
  warn "内存仅 ${mem_mb}MB，pip 安装时有 OOM 风险"
  if [ -f /swapfile ]; then
    ok "已存在 /swapfile，跳过"
    return 0
  fi
  if ! command -v sudo >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ]; then
    warn "没有 sudo，无法创建 swap；建议手动加 1G swap 再继续"
    return 0
  fi
  confirm "创建 1G swap（/swapfile）" y || return 0
  if ! run_root fallocate -l 1G /swapfile 2>/dev/null; then
    run_root dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none \
      || { warn "创建 swapfile 失败，跳过"; return 0; }
  fi
  run_root chmod 600 /swapfile
  run_root mkswap /swapfile >/dev/null || { warn "mkswap 失败"; return 0; }
  run_root swapon /swapfile || { warn "swapon 失败（容器环境常见，可忽略）"; return 0; }
  grep -q '^/swapfile' /etc/fstab || printf '/swapfile none swap sw 0 0\n' | run_root tee -a /etc/fstab >/dev/null
  ok "swap 已启用并写入 fstab"
}

# ---------- 部署代码与虚拟环境 -----------------------------------------------
deploy_files() {
  step "部署" "复制服务端代码到 $PREFIX"
  [ -f "$SERVICE_SRC/requirements.txt" ] || die "仓库缺少 $SERVICE_SRC/requirements.txt"
  install -d -m 755 "$PREFIX"
  rm -rf "$PREFIX/service"
  cp -r "$SERVICE_SRC" "$PREFIX/service"
  ok "代码已就位：$PREFIX/service"

  step "部署" "创建 Python 虚拟环境"
  if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$PY" -m venv "$PREFIX/venv" || die "venv 创建失败（Debian 系需 apt install python3-venv）"
  else
    info "复用已有虚拟环境"
  fi
  "$PREFIX/venv/bin/python" -m pip install -q --upgrade pip
  info "安装依赖（源：$PIP_INDEX）"
  "$PREFIX/venv/bin/pip" install -q -i "$PIP_INDEX" -r "$PREFIX/service/requirements.txt"
  ok "依赖安装完成"
  if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
    chown -R "$RUN_USER" "$PREFIX"
  fi
}

# ---------- systemd ----------------------------------------------------------
install_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "没有 systemd，跳过服务注册"
    return 1
  fi
  confirm "注册为 systemd 服务（开机自启、崩溃自动重启）" y || return 1
  run_root tee "/etc/systemd/system/$APP_NAME.service" >/dev/null <<EOF
[Unit]
Description=AI Passport Bilibili Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PREFIX/service
EnvironmentFile=$PREFIX/service/defaults.env
EnvironmentFile=$ENV_FILE
ExecStart=$PREFIX/venv/bin/python -m passport_bili
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
  run_root systemctl daemon-reload
  run_root systemctl enable --now "$APP_NAME" >/dev/null 2>&1 || true
  ok "systemd 服务已注册"
  return 0
}

# ---------- 启动与自检 -------------------------------------------------------
start_and_verify() {
  local url="http://127.0.0.1:${TV_PORT}/api/health"
  if command -v systemctl >/dev/null 2>&1 && \
     systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    info "等待服务就绪…"
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then break; fi
      sleep 1
    done
    if curl -fsS --max-time 3 "$url"; then
      printf '\n'; ok "服务已运行"
    else
      warn "服务未响应，查看日志：journalctl -u $APP_NAME -n 50 --no-pager"
    fi
  else
    step "启动" "手动启动命令"
    info "cd $PREFIX/service && $PREFIX/venv/bin/python -m passport_bili"
  fi
}

# ---------- 升级时补齐新增配置项 ---------------------------------------------
# 老版本装的配置里没有 TV_FPS / TV_ZLIB_LEVEL / TV_AUDIO。KEEP_CFG=1 会保留旧配置，
# 于是这些键永远缺失——用户看脚本更新了，实际跑的还是代码里的默认值。
# 这里按 defaults.env 把缺的键补上，已有的值一律不动。
merge_defaults() {
  [ -f "$ENV_FILE" ] || return 0
  local src="$PREFIX/service/defaults.env" line key added=0
  [ -f "$src" ] || return 0
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
      printf '%s\n' "$line" >> "$ENV_FILE"
      added=$((added + 1))
    fi
  done < "$src"
  if [ "$added" -gt 0 ]; then
    ok "已补入 $added 个新增配置项（原有值未改动）"
  fi
}

# ---------- 自动更新 -----------------------------------------------------------
# 用户不想每次重装一遍。这里直接从 GitHub 拉 main 分支的 tarball 覆盖代码目录，
# **配置一律不动**。不依赖 git：很多 VPS 没装，国内拉 GitHub 也未必顺畅。
#   国内网络拉不动时：REPO_MIRROR=https://ghproxy.net ./deploy/install.sh --update
REPO_SLUG="${REPO_SLUG:-qfmy78/ai-passport-bili}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_MIRROR="${REPO_MIRROR:-}"

do_update() {
  [ -f "$ENV_FILE" ] && . "$ENV_FILE" 2>/dev/null || true
  TV_PORT="${TV_PORT:-10745}"

  step "更新" "拉取 $REPO_SLUG@$REPO_BRANCH"
  local work tarball url src
  work="$(mktemp -d)"
  tarball="$work/repo.tar.gz"
  url="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$REPO_BRANCH"
  [ -n "$REPO_MIRROR" ] && url="${REPO_MIRROR%/}/$url"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 15 --max-time 300 "$url" -o "$tarball" || {
      rm -rf "$work"
      die "下载失败。国内网络可试：REPO_MIRROR=https://ghproxy.net sudo $0 --update"
    }
  elif command -v wget >/dev/null 2>&1; then
    wget -q --timeout=30 -O "$tarball" "$url" || { rm -rf "$work"; die "下载失败（wget）"; }
  else
    rm -rf "$work"; die "自动更新需要 curl 或 wget"
  fi
  ok "已下载 $(du -h "$tarball" | cut -f1)"

  tar xzf "$tarball" -C "$work" || { rm -rf "$work"; die "解压失败"; }
  src="$(ls -d "$work"/*/ 2>/dev/null | head -1)"
  if [ -z "$src" ] || [ ! -f "$src/service/passport_bili/__main__.py" ]; then
    rm -rf "$work"; die "包里没有 service/passport_bili —— 仓库或分支不对？"
  fi

  # 先备份再动：起不来就原样退回去，不让用户停在半新半旧的状态
  local backup="$PREFIX/service.bak.$(date +%Y%m%d-%H%M%S)"
  if [ -d "$PREFIX/service" ]; then
    cp -a "$PREFIX/service" "$backup" || die "备份旧代码失败，已中止（未改动任何文件）"
    info "旧代码备份在 $backup"
  fi

  local was_active=0
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    was_active=1
    run_root systemctl stop "$APP_NAME" >/dev/null 2>&1 || true
  fi

  # 状态目录（已配对设备、播放令牌）可能就落在代码目录里 —— 生产环境常见
  # TV_STATE_DIR=state，相对路径解析到 $PREFIX/service/state。直接 rm -rf 会
  # 把配对信息一起抹掉，用户更新完就得重新配网。先搬出来，换完再放回去。
  local state_keep=""
  if [ -d "$PREFIX/service/state" ]; then
    state_keep="$(mktemp -d)"
    cp -a "$PREFIX/service/state" "$state_keep/state" || die "保存配对状态失败，已中止"
  fi

  rm -rf "$PREFIX/service"
  cp -r "$src/service" "$PREFIX/service" || die "复制新代码失败"
  if [ -n "$state_keep" ]; then
    rm -rf "$PREFIX/service/state"
    cp -a "$state_keep/state" "$PREFIX/service/state"
    rm -rf "$state_keep"
    info "已保留设备配对状态（不用重新配网）"
  fi
  if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
    chown -R "$RUN_USER" "$PREFIX/service" 2>/dev/null || true
  fi

  if [ -x "$PREFIX/venv/bin/pip" ]; then
    info "同步依赖"
    "$PREFIX/venv/bin/pip" install -q -i "$PIP_INDEX" -r "$PREFIX/service/requirements.txt"       || warn "依赖安装没成功，服务可能起不来"
  fi

  merge_defaults

  if [ "$was_active" -eq 1 ]; then
    run_root systemctl start "$APP_NAME" >/dev/null 2>&1 || true
    info "等待服务就绪…"
    local i ready=0
    for i in $(seq 1 15); do
      if curl -fsS --max-time 2 "http://127.0.0.1:${TV_PORT}/api/health" >/dev/null 2>&1; then
        ready=1; break
      fi
      sleep 1
    done
    if [ "$ready" -eq 1 ]; then
      curl -fsS --max-time 3 "http://127.0.0.1:${TV_PORT}/api/health"; printf '
'
    else
      warn "新版本没起来，回滚"
      rm -rf "$PREFIX/service"; cp -a "$backup" "$PREFIX/service"
      run_root systemctl restart "$APP_NAME" >/dev/null 2>&1 || true
      rm -rf "$work"
      die "已回滚到上一版。看日志：journalctl -u $APP_NAME -n 50 --no-pager"
    fi
  else
    info "服务当前没在运行，手动启动：cd $PREFIX/service && $PREFIX/venv/bin/python -m passport_bili"
  fi

  rm -rf "$work"
  # 只留最近两份备份，别把磁盘塞满
  ls -1dt "$PREFIX"/service.bak.* 2>/dev/null | tail -n +3 | while read -r old; do rm -rf "$old"; done
  ok "更新完成（配置文件 $ENV_FILE 未改动）"
}

# ---------- 主流程 -----------------------------------------------------------
banner() {
  printf '\n%s==============================================%s\n' "$C_B" "$C_0"
  printf '  AI Passport · B站客户端  服务端部署  v%s\n' "$VER"
  printf '  目标目录：%s\n' "$PREFIX"
  printf '%s==============================================%s\n' "$C_B" "$C_0"
}

main() {
  banner
  case "$MODE" in
    show)      do_show_pairing ;;
    uninstall) do_uninstall ;;
    check)     do_check && exit 0 || exit 1 ;;
    update)    do_update; exit 0 ;;
  esac

  step "依赖" "检查并安装系统依赖"
  if check_py 2>/dev/null && check_venv 2>/dev/null && check_ffmpeg 2>/dev/null; then
    ok "依赖齐全，跳过安装"
  else
    info "缺少组件，开始安装（需要 sudo 权限）"
    install_deps
    do_check || die "依赖安装后仍不完整，请手动处理"
  fi

  if [ -f "$ENV_FILE" ]; then
    step "配置" "检测到已有配置"
    confirm "保留现有配置（不覆盖）" y && KEEP_CFG=1 || KEEP_CFG=0
  else
    KEEP_CFG=0
  fi

  ensure_swap
  deploy_files
  if [ "$KEEP_CFG" -eq 0 ]; then wizard; else
    # shellcheck disable=SC1090
    . "$ENV_FILE"; ok "沿用现有配置与配对码"
  fi

  [ "$SETUP_ONLY" -eq 1 ] && { step "完成" "已按 --setup-only 跳过启动"; exit 0; }

  merge_defaults

  install_systemd || true
  start_and_verify

  step "完成" "接下来怎么做"
  info "1) 设备上电 → 连热点 PassportTV-xxxx → 打开 http://192.168.4.1"
  info "2) 填服务器地址（本机局域网 IP）、端口 $TV_PORT、配对码 $(grep -o 'TV_PAIRING_CODE=.*' "$ENV_FILE" | cut -d= -f2)"
  info "3) 放行防火墙：sudo ufw allow $TV_PORT/tcp（或 firewall-cmd --add-port=$TV_PORT/tcp）"
  info "4) 查配对码：$0 --show-pairing    看日志：journalctl -u $APP_NAME -f"
  info "带宽占用（条带 deflate 压缩后实测）：12fps≈1.5Mbps / 15fps≈2Mbps / 8fps≈1Mbps"
  info "画面卡顿先降帧率：编辑 $ENV_FILE 里的 TV_FPS（12 起步，C3 上限约 15）"
  printf '\n'
}

main "$@"
