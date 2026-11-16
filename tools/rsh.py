#!/usr/bin/env python3
"""极简远程执行器。

密码只从环境变量 SSHPW 读取，不落盘、不写日志。
用法：
    SSHPW='***' python tools/rsh.py 'uptime; free -m'
    SSHPW='***' python tools/rsh.py -t 600 -f local_script.sh
"""
from __future__ import annotations

import argparse
import os
import sys

import paramiko


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", help="要执行的命令")
    ap.add_argument("-f", "--file", help="执行本地脚本文件")
    ap.add_argument("-t", "--timeout", type=int, default=300)
    ap.add_argument("--host", default=os.environ.get("SSHHOST"),
                    help="目标主机；不给就用环境变量 SSHHOST，都没有则报错")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SSHPORT", "22")))
    ap.add_argument("--user", default=os.environ.get("SSHUSER", "root"))
    args = ap.parse_args()

    if not args.host:
        print("缺少目标主机：用 --host 或环境变量 SSHHOST 指定", file=sys.stderr)
        return 2

    pw = os.environ.get("SSHPW")
    if not pw:
        print("缺少环境变量 SSHPW", file=sys.stderr)
        return 2

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            cmd = fh.read()
    elif args.command:
        cmd = args.command
    else:
        cmd = sys.stdin.read()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, port=args.port, username=args.user,
                   password=pw, timeout=25,
                   look_for_keys=False, allow_agent=False)
    try:
        _in, out, err = client.exec_command(cmd, timeout=args.timeout,
                                            get_pty=False)
        for chunk in iter(lambda: out.read(4096), b""):
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
        tail = err.read().decode("utf-8", "replace").strip()
        rc = out.channel.recv_exit_status()
        if tail:
            print("[stderr]", tail[-2000:], file=sys.stderr)
        return rc
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
