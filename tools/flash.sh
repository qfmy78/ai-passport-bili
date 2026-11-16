#!/usr/bin/env bash
# 烧录 B 站客户端固件到 AI Passport（ESP32-C3）。
#
# 用法：
#   ./tools/flash.sh              # 只写应用分区，保留 WiFi 和配对信息（日常迭代用这个）
#   ./tools/flash.sh --full       # 写完整镜像，等于恢复出厂（要重新配网）
#   ./tools/flash.sh COM5         # 指定端口
#   ./tools/flash.sh COM5 --full
#
# 为什么默认只写 app：分区表和基线完全一致，写 0x10000 那一块不动 NVS，
# 设备就还记得 Wi-Fi 和服务器设置，省得每次改代码都要重新配网。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"
BUILD="$ROOT/firmware/build"
APP="$BUILD/FoloToy-AI-Passport.bin"
FULL="$BUILD/FoloToy-AI-Passport-full.bin"
PY='C:\Users\mm\.espressif\python_env\idf5.5_py3.13_env\Scripts\python.exe'

MODE=app
PORT=""
for arg in "$@"; do
    case "$arg" in
        --full) MODE=full ;;
        *) [ -z "$PORT" ] && PORT="$arg" ;;
    esac
done

if [ "$MODE" = full ]; then
    BIN="$FULL"; OFFSET=0x0
    echo "模式：完整镜像（会清空 Wi-Fi 与配对信息，需重新配网）"
else
    BIN="$APP"; OFFSET=0x10000
    echo "模式：仅应用分区（保留 Wi-Fi 与配对信息）"
fi

[ -f "$BIN" ] || { echo "找不到固件：$BIN"; echo "先跑 ./tools/build.sh"; exit 1; }

if [ -z "$PORT" ]; then
    PORT=$(powershell.exe -NoProfile -Command \
        "(Get-CimInstance Win32_PnPEntity | Where-Object { \$_.Name -match '\(COM\d+\)' -and \$_.Name -notmatch '蓝牙' } | Select-Object -First 1).Name" \
        2>/dev/null | tr -d '\r' | grep -oE 'COM[0-9]+' || true)
fi
[ -n "$PORT" ] || { echo "没找到串口设备，请确认板子已插好、驱动正常。"; exit 1; }

echo "端口：$PORT   偏移：$OFFSET"
echo "固件：$BIN"
"$PY" -m esptool --chip esp32c3 -p "$PORT" -b 460800 \
    --before default_reset --after hard_reset \
    write_flash "$OFFSET" "$BIN"
