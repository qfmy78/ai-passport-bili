"""用 GitHub REST API 建一个提交，绕开 git 协议。

为什么需要它：本机到 github.com 的 TLS 连接不稳定（git push 卡死不动，
同时 api.github.com 完全正常）。Git Data API 走 api.github.com。

关键点：Trees API 支持在条目里直接给 `content`，GitHub 会隐式建 blob。
所以整棵树一次 POST 就够，不必先发 92 个 blob —— 在抖动的网络上，
少 91 次往返就是几分钟和几秒的差别。

用法：
    GH_TOKEN=xxx python tools/publish_api.py qfmy78 ai-passport-bili
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ["GH_TOKEN"]
OWNER, REPO = sys.argv[1], sys.argv[2]
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
BRANCH = "main"
# 一次提交的文件数上限：单次请求体太大容易在弱网下失败，分几批链接成一条链。
BATCH = 40


def call(method: str, url: str, body: dict | None = None, retries: int = 5):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, method=method, data=data, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-passport-bili",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = f"{exc.status} {detail}"
            if exc.status < 500 and exc.status not in (429,):
                raise RuntimeError(f"{method} {url} -> {last}") from exc
        except Exception as exc:
            last = str(exc)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{method} {url} 重试耗尽: {last}")


def head_sha() -> str:
    """拿到 main 当前提交。

    空仓库不能建 blob/tree（409 Git Repository is empty），所以先用 Contents API
    落一个占位文件把仓库"点着"，之后整棵树会覆盖掉它。
    """
    try:
        return call("GET", f"{API}/git/ref/heads/{BRANCH}")["object"]["sha"]
    except RuntimeError:
        print("空仓库，先用 Contents API 建首个提交…")
        import base64
        call("PUT", f"{API}/contents/.gitkeep", {
            "message": "初始化仓库",
            "content": base64.b64encode(b"").decode("ascii"),
            "branch": BRANCH,
        })
        return call("GET", f"{API}/git/ref/heads/{BRANCH}")["object"]["sha"]


def files_to_publish() -> list[str]:
    """已提交到本地 git 的文件清单——.gitignore 已经把产物和凭据排掉了。"""
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    parent = head_sha()
    paths = files_to_publish()
    print(f"共 {len(paths)} 个文件，分 {(len(paths) + BATCH - 1) // BATCH} 批建树…")

    tree_sha = None
    for start in range(0, len(paths), BATCH):
        chunk = paths[start:start + BATCH]
        entries = []
        for path in chunk:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                text = fh.read()
            entries.append({
                "path": path,
                "mode": "100755" if path.endswith(".sh") else "100644",
                "type": "blob",
                "content": text,
            })
        body = {"tree": entries}
        if tree_sha:
            body["base_tree"] = tree_sha          # 链式累加
        tree_sha = call("POST", f"{API}/git/trees", body)["sha"]
        print(f"  {min(start + BATCH, len(paths))}/{len(paths)}")

    message = subprocess.run(["git", "log", "-1", "--pretty=%B"],
                             capture_output=True, text=True, check=True).stdout.strip()
    print("创建提交…")
    commit = call("POST", f"{API}/git/commits", {
        "message": message or "初始提交",
        "tree": tree_sha,
        "parents": [parent],
    })

    print("指向 main…")
    call("PATCH", f"{API}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
    print(f"完成：https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
