"""B 站 API 客户端。

要点：
1. 新版接口要 WBI 签名（w_rid），签名密钥从 nav 接口的 wbi_img 派生，12 小时一换。
2. 匿名可用：推荐流、视频详情、评论；收藏夹/私信/收藏操作必须有登录 Cookie。
3. 对外一律返回"瘦 JSON"——设备端 cJSON 解析开销约是原始字节的 4.7 倍，
   320×180 的屏幕上标题也放不下，所以在服务端就截断、就删字段。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from typing import Any

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# WBI 混淆表，官方前端写死的，照抄即可
MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

API = "https://api.bilibili.com"
# 私信/会话接口在另一个域，别混用
VC = "https://api.vc.bilibili.com"
TITLE_MAX = 26          # 320px 屏一行大约放得下这么多汉字
COMMENT_MAX = 60


class BiliError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _clean(value: Any) -> str:
    """签名前必须剔掉 !'()* 这几个字符，否则 w_rid 对不上。"""
    text = str(value)
    return "".join(c for c in text if c not in "!'()*")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BiliClient:
    def __init__(self, sessdata: str = "", bili_jct: str = "",
                 dede_userid: str = "", buvid3: str = "", timeout: float = 10.0):
        self.timeout = timeout
        self.bili_jct = bili_jct.strip()
        self.uid = dede_userid.strip()
        self.sessdata = sessdata.strip()
        self.logged_in = bool(self.sessdata and self.uid)

        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        for key, val in (("SESSDATA", self.sessdata), ("bili_jct", self.bili_jct),
                         ("DedeUserID", self.uid), ("buvid3", buvid3.strip())):
            if val:
                self.s.cookies.set(key, val, domain=".bilibili.com")

        self._mixin: str | None = None
        self._mixin_at = 0.0
        if not self.s.cookies.get("buvid3"):
            self._fetch_buvid()

    # ---------------- 基础 ----------------
    def _fetch_buvid(self) -> None:
        """没有 buvid3 时接口会判定为机器人，先领一个。"""
        try:
            r = self.s.get(f"{API}/x/frontend/finger/spi", timeout=self.timeout)
            data = r.json().get("data") or {}
            if data.get("b_3"):
                self.s.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
            if data.get("b_4"):
                self.s.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except (requests.RequestException, ValueError):
            pass

    def _refresh_mixin(self) -> str:
        if self._mixin and time.time() - self._mixin_at < 6 * 3600:
            return self._mixin
        r = self.s.get(f"{API}/x/web-interface/nav", timeout=self.timeout)
        wbi = (r.json().get("data") or {}).get("wbi_img") or {}
        img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError(-1, "拿不到 WBI 密钥")
        raw = img + sub
        self._mixin = "".join(raw[i] for i in MIXIN_TAB)[:32]
        self._mixin_at = time.time()
        return self._mixin

    def _sign(self, params: dict) -> dict:
        signed = {k: _clean(v) for k, v in params.items()}
        signed["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(signed.items()))
        signed["w_rid"] = hashlib.md5((query + self._refresh_mixin()).encode()).hexdigest()
        return signed

    def _get(self, path: str, params: dict, wbi: bool = True, base: str = API) -> Any:
        if wbi:
            params = self._sign(params)
        try:
            r = self.s.get(f"{base}{path}", params=params, timeout=self.timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise BiliError(-1, f"网络或解析失败: {exc}") from exc
        code = body.get("code", -1)
        if code != 0:
            raise BiliError(code, body.get("message") or body.get("msg") or "未知错误")
        return body.get("data")

    def _post(self, path: str, data: dict) -> Any:
        data = dict(data)
        data["csrf"] = self.bili_jct
        try:
            r = self.s.post(f"{API}{path}", data=data, timeout=self.timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise BiliError(-1, f"网络或解析失败: {exc}") from exc
        if body.get("code", -1) != 0:
            raise BiliError(body.get("code", -1), body.get("message", "未知错误"))
        return body.get("data")

    # ---------------- 推荐流 ----------------
    def feed(self, idx: int = 0, size: int = 20) -> list[dict]:
        data = self._get("/x/web-interface/wbi/index/top/feed/rcmd", {
            "ps": size,
            "fresh_type": 4,
            "feed_version": "V8",
            "homepage_ver": 1,
            "fresh_idx": idx + 1,
            "fresh_idx_1h": idx + 1,
            "brush": idx,
        })
        items = []
        for it in (data or {}).get("item") or []:
            bvid = it.get("bvid") or it.get("id")
            if not bvid or not str(bvid).startswith("BV"):
                continue  # 广告、直播、番剧都跳过，设备端只认普通投稿
            stat = it.get("stat") or {}
            items.append({
                "b": bvid,
                "t": _clip(it.get("title", ""), TITLE_MAX),
                "u": _clip((it.get("owner") or {}).get("name", ""), 12),
                "d": it.get("duration") or 0,
                "v": stat.get("view") or 0,
            })
        return items

    # ---------------- 视频详情 ----------------
    def video(self, bvid: str) -> dict:
        data = self._get("/x/web-interface/view", {"bvid": bvid}, wbi=False) or {}
        stat = data.get("stat") or {}
        pages = data.get("pages") or []
        return {
            "b": data.get("bvid", bvid),
            "a": data.get("aid", 0),
            "t": _clip(data.get("title", ""), TITLE_MAX),
            "u": _clip((data.get("owner") or {}).get("name", ""), 12),
            "d": data.get("duration", 0),
            "v": stat.get("view", 0),
            "f": (stat.get("favorite") or 0),
            "r": stat.get("reply") or 0,
            "desc": _clip(data.get("desc", ""), 120),
            "pages": len(pages),
            "cid": pages[0].get("cid") if pages else data.get("cid", 0),
        }

    # ---------------- 取流 ----------------
    def playurl(self, bvid: str) -> tuple[str, str, str]:
        """返回 (视频URL, 音频URL, 标题)。

        B 站是 DASH，音视频分离，ffmpeg 要吃两个输入。挑带宽最低的一路就行——
        反正最后要缩到 320×180，高码率纯属浪费带宽和解码时间。
        标题回传给设备当频道名，否则播放器横幅会显示成 "?"。
        """
        info = self._get("/x/web-interface/view", {"bvid": bvid}, wbi=False) or {}
        pages = info.get("pages") or []
        cid = pages[0].get("cid") if pages else info.get("cid")
        if not cid:
            raise BiliError(-1, "拿不到 cid")
        data = self._get("/x/player/playurl", {
            "bvid": bvid, "cid": cid, "qn": 16, "fnver": 0, "fnval": 16, "fourk": 0,
        }, wbi=False) or {}
        dash = data.get("dash") or {}
        videos = [v for v in (dash.get("video") or []) if v.get("baseUrl")]
        audios = [a for a in (dash.get("audio") or []) if a and a.get("baseUrl")]
        if not videos:
            raise BiliError(-1, "没有可用视频流（可能需登录或大会员）")
        if not audios:
            raise BiliError(-1, "没有可用音频流")
        v = min(videos, key=lambda x: x.get("bandwidth") or 0)
        a = min(audios, key=lambda x: x.get("bandwidth") or 0)
        return v["baseUrl"], a["baseUrl"], info.get("title", "")

    # ---------------- 评论 ----------------
    def aid_of(self, bvid: str) -> int:
        """bv 号换 av 号。评论、收藏这些老接口只认 aid。"""
        info = self._get("/x/web-interface/view", {"bvid": bvid}, wbi=False) or {}
        aid = info.get("aid")
        if not aid:
            raise BiliError(-1, "拿不到 aid")
        return int(aid)

    def comments(self, aid: int, page: int = 1, size: int = 12) -> list[dict]:
        data = self._get("/x/v2/reply", {
            "type": 1, "oid": aid, "pn": page, "ps": size, "sort": 2,
        }, wbi=False)
        out = []
        for rp in (data or {}).get("replies") or []:
            out.append({
                "u": _clip((rp.get("member") or {}).get("uname", ""), 12),
                "m": _clip((rp.get("content") or {}).get("message", ""), COMMENT_MAX),
                "l": (rp.get("like") or 0),
            })
        return out

    def login_state(self) -> tuple[bool, str]:
        """真实校验 Cookie 还在不在，返回 (是否有效, 昵称)。

        `logged_in` 只说明"配置里填了 SESSDATA"，不等于 Cookie 还有效——
        Cookie 过期后推荐流照样能拉（游客也有推荐），只有 /api/me、收藏、私信会挂。
        之前 health 拿"配了没配"冒充登录状态，排查时白绕一圈。
        """
        try:
            nav = self._get("/x/web-interface/nav", {}, wbi=False) or {}
        except BiliError:
            return False, ""
        if not nav.get("isLogin"):
            return False, ""
        return True, str(nav.get("uname") or "")

    # ---------------- 个人主页 ----------------
    def me(self) -> dict:
        """当前账号概要，供设备个人主页显示。设备屏幕只有 320×240，字段越少越好。"""
        nav = self._get("/x/web-interface/nav", {}, wbi=False) or {}
        if not nav.get("isLogin"):
            raise BiliError(-101, "未登录")
        mid = nav.get("mid") or self.uid
        stat = {}
        try:
            stat = self._get("/x/relation/stat", {"vmid": mid}, wbi=False) or {}
        except BiliError:
            pass
        folders = 0
        try:
            folders = len(self.fav_folders(limit=10))
        except BiliError:
            pass
        vip = nav.get("vipStatus") or nav.get("vip", {}).get("status") if isinstance(nav.get("vip"), dict) else 0
        return {
            "name": _clip(nav.get("uname", ""), 14),
            "level": nav.get("level_info", {}).get("current_level", 0),
            "coins": nav.get("money", 0),
            "following": stat.get("following", 0),
            "follower": stat.get("follower", 0),
            "favs": folders,
            "vip": 1 if vip else 0,
        }

    # ---------------- 收藏夹 ----------------
    def fav_folders(self, limit: int = 5) -> list[dict]:
        if not self.logged_in:
            raise BiliError(-101, "未登录")
        data = self._get("/x/v3/fav/folder/created/list-all",
                         {"up_mid": self.uid, "web_location": 333.1387}, wbi=False)
        folders = (data or {}).get("list") or []
        return [{"id": f.get("id"), "n": _clip(f.get("title", ""), 14),
                 "c": f.get("media_count", 0)} for f in folders[:limit]]

    def fav_add(self, bvid: str) -> int:
        """把视频收藏进第一个收藏夹，返回收藏夹 id。

        设备播放时按菜单里的「收藏」，服务端就调这个。
        """
        if not self.logged_in:
            raise BiliError(-101, "未登录")
        info = self._get("/x/web-interface/view", {"bvid": bvid}, wbi=False) or {}
        aid = info.get("aid")
        if not aid:
            raise BiliError(-1, "拿不到 aid")
        folders = self.fav_folders(1)
        if not folders:
            raise BiliError(-1, "没有可用的收藏夹")
        media_id = folders[0]["id"]
        try:
            r = self.s.post(
                "https://api.bilibili.com/x/v3/fav/resource/deal",
                data={"rid": aid, "type": 2, "add_media_ids": media_id,
                      "del_media_ids": "", "csrf": self.bili_jct},
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
                timeout=self.timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise BiliError(-1, f"网络或解析失败: {exc}") from exc
        code = body.get("code", -1)
        if code != 0:
            raise BiliError(code, body.get("message") or "收藏失败")
        return media_id

    def fav_items(self, media_id: int, page: int = 1, size: int = 20) -> list[dict]:
        if not self.logged_in:
            raise BiliError(-101, "未登录")
        data = self._get("/x/v3/fav/resource/list", {
            "media_id": media_id, "pn": page, "ps": size,
            "keyword": "", "order": "mtime", "type": 0, "tid": 0,
            "platform": "web", "web_location": 333.1387,
        }, wbi=False)
        out = []
        for m in (data or {}).get("medias") or []:
            if m.get("type") != 2:
                continue
            out.append({"b": m.get("bvid"), "t": _clip(m.get("title", ""), TITLE_MAX),
                        "u": _clip((m.get("upper") or {}).get("name", ""), 12),
                        "d": m.get("duration", 0)})
        return out

    def fav_default_id(self) -> int:
        folders = self.fav_folders(limit=5)
        if not folders:
            raise BiliError(-404, "没有可用收藏夹")
        return int(folders[0]["id"])

    def fav_contains(self, aid: int, media_id: int | None = None,
                     max_pages: int = 3) -> bool:
        """扫默认收藏夹前几页判断是否已收藏。收藏动作是写操作，必须先查准。"""
        media_id = media_id or self.fav_default_id()
        for page in range(1, max_pages + 1):
            data = self._get("/x/v3/fav/resource/list", {
                "media_id": media_id, "pn": page, "ps": 20, "keyword": "",
                "order": "mtime", "type": 0, "tid": 0, "platform": "web",
            }, wbi=False)
            medias = (data or {}).get("medias") or []
            if not medias:
                return False
            if any(m.get("id") == aid for m in medias):
                return True
        return False

    def fav_toggle(self, aid: int, media_id: int | None = None) -> bool:
        """返回操作后的收藏状态：True=已收藏，False=已取消。"""
        if not self.logged_in or not self.bili_jct:
            raise BiliError(-101, "未登录或缺少 bili_jct")
        media_id = media_id or self.fav_default_id()
        if self.fav_contains(aid, media_id):
            self._post("/x/v3/fav/resource/deal",
                       {"rid": aid, "type": 2, "add_media_ids": "", "del_media_ids": media_id})
            return False
        self._post("/x/v3/fav/resource/deal",
                   {"rid": aid, "type": 2, "add_media_ids": media_id, "del_media_ids": ""})
        return True

    # ---------------- 私信 ----------------
    def messages(self, limit: int = 20) -> list[dict]:
        """只读文字。B 站私信走 session_svr，风控最严，失败就让设备显示"暂不可用"。"""
        if not self.logged_in:
            raise BiliError(-101, "未登录")
        try:
            r = self.s.get(
                "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
                params={"session_type": 1, "group_fold": 1, "unfollow_fold": 0,
                        "sort_rule": 2, "build": 0, "mobi_app": "web"},
                timeout=self.timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise BiliError(-1, f"私信接口失败: {exc}") from exc
        if body.get("code", -1) != 0:
            raise BiliError(body.get("code", -1), body.get("message", "私信不可用"))
        out = []
        for sess in (body.get("data") or {}).get("session_list") or []:
            last = sess.get("last_msg") or {}
            # 正文藏了两层：last_msg.content 是一段 JSON 字符串，里面才是真正的 content。
            # 图片/视频等非文本消息只有 url 之类，给个占位标签。
            raw = last.get("content") or ""
            text = ""
            try:
                inner = json.loads(raw)
                if isinstance(inner, dict):
                    text = inner.get("content") or ""
                    if not text:
                        kind = inner.get("msg_type") or last.get("msg_type")
                        text = {2: "[图片]", 5: "[视频]", 6: "[表情]", 7: "[语音]"}.get(kind, "[消息]")
                else:
                    text = str(inner)
            except (ValueError, TypeError):
                text = raw
            who = sess.get("talker_id") or last.get("sender_uid")
            out.append({
                "u": str(who or ""),
                "m": _clip(text, COMMENT_MAX),
                "t": sess.get("session_ts", 0),
            })
        return out[:limit]

    def thread(self, talker: str, limit: int = 20) -> list[dict]:
        """某个会话的历史消息。设备点进私信后看的就是这个。"""
        # 注意路径：历史消息在 svr_sync 下，不在 session_svr 下。
        # 列会话是 /session_svr/v1/session_svr/get_sessions，两者不同名不同段，
        # 按"同类接口同前缀"的直觉去猜会一直 404（踩过）。
        data = self._get("/svr_sync/v1/svr_sync/fetch_session_msgs", {
            "talker_id": talker, "session_type": 1, "size": limit,
            "build": 0, "mobi_app": "web",
        }, base=VC, wbi=False) or {}
        msgs = data.get("messages") or data.get("msgs") or []
        out = []
        for m in msgs:
            raw = m.get("content") or ""
            text = ""
            try:
                inner = json.loads(raw)
                if isinstance(inner, dict):
                    text = inner.get("content") or ""
                    if not text:
                        text = {2: "[图片]", 5: "[视频]", 6: "[表情]", 7: "[语音]"}.get(
                            m.get("msg_type"), "[消息]")
                elif isinstance(inner, list):
                    # 系统提示是 [{"text": "..."}] 这种列表，直接 str() 会露出一堆 JSON
                    first = inner[0] if inner else {}
                    text = (first.get("text") or "") if isinstance(first, dict) else str(first)
                else:
                    text = str(inner)
            except (ValueError, TypeError):
                text = raw
            out.append({
                "u": str(m.get("sender_uid") or ""),
                "m": _clip(text, COMMENT_MAX),
                "t": m.get("timestamp", 0),
            })
        return out[:limit]

    # ---------------- 自检 ----------------
    def self_check(self) -> dict:
        result: dict[str, Any] = {"logged_in": self.logged_in}
        try:
            result["feed"] = len(self.feed(0, 8))
        except BiliError as exc:
            result["feed"] = f"FAIL {exc}"
        try:
            nav = self._get("/x/web-interface/nav", {}, wbi=False) or {}
            result["nav"] = "ok" if nav else "empty"
            result["wbi"] = bool(self._refresh_mixin())
        except BiliError as exc:
            # 未登录时 nav 返回 -101，但 wbi_img 照样能拿到，不算故障
            result["nav"] = "anonymous" if exc.code == -101 else f"FAIL {exc}"
            try:
                result["wbi"] = bool(self._refresh_mixin())
            except BiliError:
                result["wbi"] = False
        if self.logged_in:
            try:
                result["folders"] = len(self.fav_folders())
            except BiliError as exc:
                result["folders"] = f"FAIL {exc}"
        return result


if __name__ == "__main__":
    import json
    import os
    run_env = os.environ.get("PASSPORT_BILI_ENV", "/etc/passport-bili/env")
    if os.path.isfile(run_env):
        for line in open(run_env, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    client = BiliClient(
        sessdata=os.environ.get("BILI_SESSDATA", ""),
        bili_jct=os.environ.get("BILI_BILI_JCT", ""),
        dede_userid=os.environ.get("BILI_DEDEUSERID", ""),
        buvid3=os.environ.get("BILI_BUVID3", ""),
    )
    print(json.dumps(client.self_check(), ensure_ascii=False, indent=2))
    try:
        for item in client.feed(0, 5):
            print(f"  {item['b']}  {item['t']}  @{item['u']}  {item['d']}s")
    except BiliError as exc:
        print("feed 失败:", exc)
