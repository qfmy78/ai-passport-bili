"""服务端入口（部署骨架 v0.1.0）。

本阶段只做三件事：加载配置、暴露健康检查、完成设备配对。
推荐流 / 视频转码 / 评论 / 收藏夹 / 私信 在后续阶段补进同一套 /api/ 路由。
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, bili as bili_api
from . import stream as stream_mod
from .stream import SessionError, stream as fav1_stream

LOCK = threading.Lock()
DEVICES: dict[str, dict] = {}
# 配对令牌 -> 待播 bvid。设备点播放时先 POST /api/play 登记，再起 FAV1 会话读取。
PENDING: dict[str, str] = {}

# /api/health 的登录状态缓存：真校验要打 B 站，60 秒一次足够。
LOGIN_STATE = {"ok": False, "name": "", "at": 0.0}

# 一个进程共用一个 B 站客户端（WBI 密钥要复用，否则每次请求都得重新拉 nav）。
_CLIENT: bili_api.BiliClient | None = None
_CLIENT_LOCK = threading.Lock()


def bili() -> bili_api.BiliClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = bili_api.BiliClient(CFG.sessdata, CFG.bili_jct,
                                          CFG.dede_userid, CFG.buvid3)
        return _CLIENT


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_env_file(path: Path) -> None:
    """把 KEY=VALUE 写进 environ（已存在的键不覆盖，保证真实环境变量优先）。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def boot_env() -> None:
    """优先级：真实环境变量 > 显式指定的 env > /etc > ~/.config > defaults.env。

    load_env_file 用的是 setdefault，所以先读的赢；defaults.env 必须放最后，
    否则手动启动时它会把用户配置盖掉（systemd 那边靠 EnvironmentFile 顺序保证同样的优先级）。
    """
    for cand in (
        os.environ.get("PASSPORT_BILI_ENV"),
        "/etc/passport-bili/env",
        str(Path.home() / ".config" / "passport-bili" / "env"),
        str(Path(__file__).resolve().parent.parent / "defaults.env"),
    ):
        if cand:
            load_env_file(Path(cand))


class Config:
    def __init__(self) -> None:
        self.bind = os.environ.get("TV_BIND", "0.0.0.0")
        self.port = int(os.environ.get("TV_PORT", "10745"))
        self.pairing_code = os.environ.get("TV_PAIRING_CODE", "").strip().upper()
        self.pairing_token = os.environ.get("TV_PAIRING_TOKEN", "").strip()
        self.quality = os.environ.get("TV_QUALITY", "standard")
        self.max_sessions = int(os.environ.get("TV_MAX_SESSIONS", "2"))
        self.state_dir = Path(os.environ.get("TV_STATE_DIR", "state")).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sessdata = os.environ.get("BILI_SESSDATA", "").strip()
        self.bili_jct = os.environ.get("BILI_BILI_JCT", "").strip()
        self.dede_userid = os.environ.get("BILI_DEDEUSERID", "").strip()
        self.buvid3 = os.environ.get("BILI_BUVID3", "").strip()

    @property
    def logged_in(self) -> bool:
        return bool(self.sessdata and self.dede_userid)


CFG = Config()


def device_file() -> Path:
    return CFG.state_dir / "paired-devices.json"


def pending_file() -> Path:
    return CFG.state_dir / "pending.json"


def load_pending() -> None:
    """设备只在按 OK 时调一次 /api/play，重试时不会重新登记，
    所以这份映射必须落盘，否则服务一重启设备就再也拉不到流。"""
    try:
        PENDING.update(json.loads(pending_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def save_pending() -> None:
    try:
        tmp = pending_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(PENDING, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(pending_file())
    except OSError as exc:
        print(f"  待播表写入失败: {exc}", flush=True)


def load_devices() -> None:
    try:
        DEVICES.clear()
        DEVICES.update(json.loads(device_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def save_devices() -> None:
    tmp = device_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(DEVICES, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(device_file())


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"PassportBili/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        sys.stdout.write(f"  {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()

    def send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            return {}

    def do_GET(self) -> None:
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path
        query = urllib.parse.parse_qs(raw_query)

        def q(name: str, default: str = "") -> str:
            return (query.get(name) or [default])[0]

        if path == "/api/health":
            # 登录状态要真校验，但 health 是被监控轮询的，不能每次都打 B 站：
            # 60 秒缓存，且校验失败只影响这一项，不拖垮整个健康检查。
            if time.time() - LOGIN_STATE["at"] > 60:
                try:
                    ok, name = bili().login_state()
                except Exception:
                    ok, name = False, ""            # 网络问题按未登录报，宁可误报
                LOGIN_STATE.update({"ok": ok, "name": name, "at": time.time()})
            self.send_json(200, {
                "ok": True,
                "version": __version__,
                "bind": CFG.bind,
                "port": CFG.port,
                "quality": CFG.quality,
                "bili": {
                    "logged_in": LOGIN_STATE["ok"],
                    "name": LOGIN_STATE["name"] or None,
                    "uid": CFG.dede_userid or None,
                },
                "pairing_configured": bool(CFG.pairing_code),
                "devices": len(DEVICES),
            })
        elif path == "/api/feed":
            try:
                page = max(0, int(q("page", "0") or 0))
            except ValueError:
                page = 0
            try:
                items = bili().feed(page, 20)
                self.send_json(200, {"items": items})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/comments":
            try:
                aid = int(q("aid", "0") or 0)
            except ValueError:
                aid = 0
            if not aid:
                self.send_json(400, {"error": "bad_aid"})
                return
            try:
                self.send_json(200, {"items": bili().comments(aid, 1, 12)})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/me":
            try:
                self.send_json(200, bili().me())
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/favorites":
            try:
                self.send_json(200, {"items": bili().fav_folders(5)})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/favorites/items":
            media_id = q("media_id")
            if not media_id.isdigit():
                self.send_json(400, {"error": "bad_media_id"})
                return
            try:
                self.send_json(200, {"items": bili().fav_items(int(media_id), 1, 20)})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/messages":
            try:
                self.send_json(200, {"items": bili().messages(10)})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/api/messages/thread":
            talker = q("talker")
            if not talker.isdigit():
                self.send_json(400, {"error": "bad_talker"})
                return
            try:
                self.send_json(200, {"items": bili().thread(talker, 20)})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})
        elif path == "/":
            self.send_json(200, {"service": "passport-bili", "version": __version__})
        else:
            self.send_json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        # 固件里的 pairing_client.c 是电视项目留下来的，打的是 /social/tv/*；
        # 我们对外叫 /api/tv/*。两边都认，省得为了改路径重新烧一遍固件。
        if path.startswith("/social/tv/"):
            path = "/api/tv/" + path[len("/social/tv/"):]
        payload = self.read_json()

        if path == "/api/tv/activate":
            dev = str(payload.get("device", "")).strip().lower()
            code = str(payload.get("code", "")).strip().upper()
            if len(dev) != 12 or any(c not in "0123456789abcdef" for c in dev):
                self.send_json(400, {"error": "bad_device_id"})
                return
            if not CFG.pairing_code or code != CFG.pairing_code:
                self.send_json(403, {"error": "bad_pairing_code"})
                return
            with LOCK:
                rec = DEVICES.get(dev) or {"ticket": secrets.token_hex(32)}
                rec["last_seen"] = self.date_time_string()
                DEVICES[dev] = rec
                save_devices()
                ticket = rec["ticket"]
            host = self.headers.get("X-Public-Host") or self.headers.get("Host", "")
            self.send_json(200, {
                "host": host.split(":")[0] or CFG.bind,
                "port": CFG.port,
                "ticket": ticket,
            })

        elif path == "/api/tv/config":
            dev = str(payload.get("device", "")).strip().lower()
            rec = DEVICES.get(dev)
            if not rec:
                # 必须是 403，不能是 404。固件的 pairing_client.c 里写死：
                #   status==403 -> ESP_ERR_NOT_FOUND（继续走 activate）
                #   其他非 200 -> ESP_ERR_INVALID_RESPONSE（直接中止配对）
                # 用 404 会让它以为服务器坏了，永远走不到 activate 那一步。
                self.send_json(403, {"error": "device_not_paired"})
                return
            # 固件在已配对时会用这个接口刷新连接信息，所以必须和 activate 返回同一组字段
            # （host/port/ticket），少一个它就拿不到流地址。
            host = self.headers.get("X-Public-Host") or self.headers.get("Host", "")
            self.send_json(200, {
                "host": host.split(":")[0] or CFG.bind,
                "port": CFG.port,
                "ticket": rec["ticket"],
                "quality": CFG.quality,
            })

        elif path == "/api/favorite":
            dev = str(payload.get("device", "")).strip().lower()
            bvid = str(payload.get("bvid", "")).strip()
            if not DEVICES.get(dev):
                self.send_json(404, {"error": "device_not_paired"})
                return
            if not bvid.startswith("BV"):
                self.send_json(400, {"error": "bad_bvid"})
                return
            try:
                folder = bili().fav_add(bvid)
                print(f"  收藏 {bvid} -> 收藏夹 {folder}", flush=True)
                self.send_json(200, {"ok": True, "folder": folder})
            except bili_api.BiliError as exc:
                self.send_json(502, {"error": "bili", "detail": str(exc)})

        elif path == "/api/pause":
            dev = str(payload.get("device", "")).strip().lower()
            rec = DEVICES.get(dev)
            if not rec:
                self.send_json(403, {"error": "device_not_paired"})
                return
            paused = bool(payload.get("paused"))
            stream_mod.set_paused(str(rec.get("ticket", "")), paused)
            print(f"  暂停{'开启' if paused else '关闭'} {dev}", flush=True)
            self.send_json(200, {"ok": True, "paused": paused})

        elif path == "/api/play":
            dev = str(payload.get("device", "")).strip().lower()
            bvid = str(payload.get("bvid", "")).strip()
            rec = DEVICES.get(dev)
            if not rec:
                self.send_json(404, {"error": "device_not_paired"})
                return
            if not bvid.startswith("BV") or not bvid.isalnum():
                self.send_json(400, {"error": "bad_bvid"})
                return
            with LOCK:
                PENDING[rec["ticket"]] = bvid
                save_pending()
            self.send_json(200, {"ok": True, "stream_port": CFG.port + 1})

        else:
            self.send_json(404, {"error": "not_found", "path": path})


# --------------------------------------------------------------------------
# FAV1 媒体通道（端口 = 控制端口 + 1）
# --------------------------------------------------------------------------
def serve_fav1(conn: socket.socket, addr) -> None:
    def resolve(token):
        bvid = PENDING.get(token or "")
        if not bvid:
            raise SessionError("该设备没有待播任务（先调 /api/play）")
        print(f"  FAV1 {addr[0]} 请求 {bvid}", flush=True)
        return bili().playurl(bvid)

    try:
        fav1_stream(conn, resolve, log=print)
    except Exception as exc:                      # 一个会话失败不能拖垮服务
        print(f"  FAV1 {addr}: {exc}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def fav1_loop(bind: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(4)
    print(f"  media       fav1://{bind}:{port}", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=serve_fav1, args=(conn, addr), daemon=True).start()


def main() -> int:
    load_devices()
    load_pending()
    banner = [
        f"AI Passport · Bilibili backend v{__version__}",
        f"  listening   http://{CFG.bind}:{CFG.port}",
        f"  bili account {'已登录 uid=' + CFG.dede_userid if CFG.logged_in else '游客模式'}",
        f"  pairing     {'已配置' if CFG.pairing_code else '未配置（请重跑 install.sh）'}",
        f"  state dir   {CFG.state_dir}",
    ]
    print("\n".join(banner), flush=True)
    threading.Thread(target=fav1_loop, args=(CFG.bind, CFG.port + 1),
                     daemon=True).start()

    httpd = ThreadingHTTPServer((CFG.bind, CFG.port), Handler)
    httpd.daemon_threads = True

    def stop(_sig, _frm):
        print("\n  收到退出信号，关闭中…", flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
