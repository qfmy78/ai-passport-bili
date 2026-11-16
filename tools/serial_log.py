"""抓设备的串口日志，用来定位重启原因。

ESP32-C3 的原生 USB 在 panic 重启时会重新枚举，端口会短暂消失，
所以这里循环重连，直到 deadline 或用户中断。

用法：
    python tools/serial_log.py COM8 180
"""
from __future__ import annotations

import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

deadline = time.time() + SECONDS
log = open("serial_log.txt", "a", encoding="utf-8", errors="replace")


def emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
    log.write(text)
    log.flush()


emit(f"\n=== 开始抓取 {PORT}，{SECONDS:.0f} 秒 ===\n")
while time.time() < deadline:
    try:
        with serial.Serial(PORT, 115200, timeout=1) as dev:
            emit(f"\n=== {time.strftime('%H:%M:%S')} 端口已打开 ===\n")
            while time.time() < deadline:
                chunk = dev.read(4096)
                if chunk:
                    emit(chunk.decode("utf-8", "replace"))
    except Exception as exc:                      # 重启期间端口消失是正常的
        emit(f"\n=== {time.strftime('%H:%M:%S')} 端口断开: {exc} ===\n")
        time.sleep(1.0)
emit("\n=== 抓取结束 ===\n")
log.close()
