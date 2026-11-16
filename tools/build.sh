#!/usr/bin/env bash
# 编译 FoloToy AI Passport 的 B 站客户端固件。
#
# 为什么不用官方 export.sh / idf.py：
#   1. Git Bash 会一直把 MSYSTEM 塞进环境，而 IDF 的 idf.py 遇到 MSYSTEM 只打印警告、
#      不调用 main()（表现：1 秒退出、什么都没干），idf_tools.py 更是直接报错退出。
#      tools/idfwrap.py 从 os.environ 里摘掉它再调 main()，绕开这个守卫。
#   2. PowerShell 的 cmdlet 会话里改 PATH 传不到孙进程（cmake 看不到工具链），
#      而 Bash 的环境继承是正常的，所以构建统一走 Bash。
#   3. cmake 偶尔找不到 ninja，显式给 -DCMAKE_MAKE_PROGRAM 最稳。
#
# 用法：
#   ./tools/build.sh            # 增量编译
#   ./tools/build.sh fullclean  # 清干净重编
#   ./tools/build.sh menuconfig # 进配置界面
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$HERE/.." && pwd)"
FW_DIR="$PROJECT_ROOT/firmware"

IDF_PATH_WIN='C:\Espressif\frameworks\esp-idf-v5.5.3'
TOOLS_WIN='C:\Users\mm\.espressif'
VENV="$TOOLS_WIN\\python_env\\idf5.5_py3.13_env"
PY="$TOOLS_WIN\\python_env\\idf5.5_py3.13_env\\Scripts\\python.exe"
NINJA="$TOOLS_WIN\\tools\\ninja\\1.12.1\\ninja.exe"

export IDF_PATH="$IDF_PATH_WIN"
export IDF_PYTHON_ENV_PATH="$VENV"
export IDF_TOOLS_PATH="$TOOLS_WIN"
export ESP_ROM_ELF_DIR="$TOOLS_WIN\\tools\\esp-rom-elfs\\20241011"
export IDF_GITHUB_ASSETS='dl.espressif.cn/github_assets'

export PATH="/c/Users/mm/.espressif/python_env/idf5.5_py3.13_env/Scripts\
:/c/Users/mm/.espressif/tools/ninja/1.12.1\
:/c/Users/mm/.espressif/tools/cmake/3.30.2/bin\
:/c/Users/mm/.espressif/tools/ccache/4.12.1/ccache-4.12.1-windows-x86_64\
:/c/Users/mm/.espressif/tools/riscv32-esp-elf/esp-14.2.0_20251107/riscv32-esp-elf/bin\
:$PATH"

cd "$FW_DIR"
ACTION="${1:-build}"
shift || true

"$PY" "$PROJECT_ROOT/tools/idfwrap.py" \
    -DCMAKE_MAKE_PROGRAM="$NINJA" "$ACTION" "$@"
