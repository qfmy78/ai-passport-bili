"""绕过 idf.py 的 MSYS 守卫直接调用 main()。

ESP-IDF 的 tools/idf.py 结尾是：

    if 'MSYSTEM' in os.environ:
        print_warning(...)      # 只警告，不调 main()
    elif ...
    else:
        main()

Git Bash 会一直把 MSYSTEM 塞回环境里（unset 也不管用），结果 idf.py 永远走不进
main()，表现为"1 秒退出、什么都没干"。这里直接 import 模块调 main()，绕开守卫。
"""
import os
import sys

# Git Bash 会把 MSYSTEM 塞进每个子进程，unset 之后它还会回来。这里从 python 进程
# 自己的 environ 里摘掉，IDF 各处 'MSYSTEM' in os.environ 的检查就都通过了。
for _k in ('MSYSTEM', 'MSYS', 'MINGW_PREFIX'):
    os.environ.pop(_k, None)

IDF_PATH = os.environ.get('IDF_PATH', r'C:\Espressif\frameworks\esp-idf-v5.5.3')
sys.path.insert(0, os.path.join(IDF_PATH, 'tools'))
sys.argv = ['idf.py'] + sys.argv[1:]

import idf  # noqa: E402  (必须先把 tools/ 放进 sys.path)

idf.main()
