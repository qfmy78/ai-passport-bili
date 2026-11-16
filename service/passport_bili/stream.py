"""FAV1 会话驱动：把一个 B 站视频推给设备。

协议沿用 ai-passport-tv 的 FAV1（24 字节包头 + 七种包），固件侧一个字节都没改。
会话顺序（设备端 tv_player.c 逐项精确校验，错一个字段就连上即断）：

    设备 → HELLO   session=0 seq=0 pts=0   {"version":1,"token":"<ticket>"}
    服务端 → CONFIG session=N seq=0        width/height/fps/... 全字段精确匹配
    服务端 → 媒体   session=N seq=1..      音频 PCM 与视频条带共用序号，必须连续

调色板不发：固件对 rgb8 用的是内置 3-3-2 固定调色板（av_palette_rgb565），
PALETTE 包只用于自适应调色板，发了反而可能和内置值有细微色差。
"""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import threading
import time

from .tv import adpcm, frames, protocol
from .tv.protocol import Kind, Packet, json_bytes, receive_packet, send_packet

# zlib 级别从默认 6 降到 3：压缩速度大约快一倍，体积只大 10~15%。
# 15fps 时每秒要压 225 个条带，级别 6 在 2 核 VPS 上会把 CPU 吃满，
# 表现就是画面隔一会儿卡一下（音频反而正常，因为它不压缩）。
frames._COMPRESS_LEVEL = int(os.environ.get("TV_ZLIB_LEVEL", "3"))

PCM_CHUNK = protocol.AUDIO_PCM_BYTES          # 1280 = 640 个 s16le 采样 = 40 ms
FRAME_BYTES = frames.FRAME_PIXELS             # 320*180 = 57600 字节 rgb8
FPS = int(os.environ.get("TV_FPS", "15"))
START_DELAY_MS = 200

# B 站 CDN 一定要合法 UA：只带 Referer 会 403，而 ffmpeg 默认 UA 是 "Lavf/x.y.z"，
# 会被判定成爬虫。静态版 ffmpeg 在 403/TLS 失败的路径上还会直接段错误（core dump），
# 排查时只表现为"连上就断、日志里一句 ffmpeg dumped core"。
BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BILI_REFERER = "Referer: https://www.bilibili.com/\r\n"

# 静态版 ffmpeg 不认系统默认证书路径，必须显式给；而且这类按输入生效的选项
# 每个 -i 前都得重复一遍（只在第一个输入前给，第二个输入就会 TLS 失败）。
CA_FILE = os.environ.get("TV_CA_FILE", "/etc/pki/tls/certs/ca-bundle.crt")

# 关掉音频后不下发 PCM 包（仍然从 ffmpeg 读掉，免得管道堵住）。
# 时钟照常推进，视频 pts 才能继续往前跑。
AUDIO_ENABLED = os.environ.get("TV_AUDIO", "1") != "0"


class SessionError(RuntimeError):
    pass


def _recv_pcm(sock: socket.socket, size: int, stop: threading.Event) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        if stop.is_set():
            return None
        try:
            chunk = sock.recv(size - len(data))
        except OSError:
            return None
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


class _Sender:
    """串行化发送并维护连续序号。音频与视频来自两个线程，序号必须一条一条领。"""

    def __init__(self, conn: socket.socket, session: int):
        self.conn = conn
        self.session = session
        self.seq = 1        # CONFIG 占 0
        self.lock = threading.Lock()
        self.closed = False

    def send(self, kind: Kind, pts_ms: int, payload: bytes, flags: int = 0) -> None:
        with self.lock:
            if self.closed:
                return
            packet = Packet(kind, self.session, self.seq, pts_ms, payload, flags)
            try:
                send_packet(self.conn, packet, timeout=2.0)
            except (OSError, EOFError, TimeoutError) as exc:
                self.closed = True
                raise SessionError(f"发送失败: {exc}") from exc
            self.seq = (self.seq + 1) & 0xFFFFFFFF


def rgb332_palette() -> bytes:
    """256 项大端 RGB565 调色板，逐项与固件 av_palette_rgb565() 一致。

    必须发：设备会话开始只把 palette_ready 置 false，并不会把固定调色板填进去，
    而那块缓冲是 calloc 出来的全零。不发 PALETTE 的话每个索引都映射到 0x0000，
    现象就是"有声音、画面全黑"——音频链路完全正常，所以很容易误判成视频没到。
    位扩展用移位不用乘法：3 位进 5 位要复制高位补满量程，否则 256 项里有 220 项差一。
    """
    out = bytearray()
    for index in range(256):
        r3 = (index >> 5) & 0x7
        g3 = (index >> 2) & 0x7
        b2 = index & 0x3
        r5 = (r3 << 2) | (r3 >> 1)
        g6 = (g3 << 3) | g3
        b5 = (b2 << 3) | (b2 << 1) | (b2 >> 1)
        out += struct.pack(">H", (r5 << 11) | (g6 << 5) | b5)
    return bytes(out)


class _Clock:
    """协议时间轴。

    固件 av_stream_accept() 的校验是硬的，用墙上时钟必被拒：
      · 音频：pts_ms 必须严格等于 audio_next_pts，每包递增 40ms —— 0,40,80,120…
        差一毫秒（比如实测的 37、78）整个会话立刻终止。
      · 视频：新帧的 pts 必须严格大于上一帧；续包必须与所属帧同值。

    所以音频按包序号乘 40 推，视频对齐到当前音频媒体时间并保证严格递增。
    """

    AV_AUDIO_MS = 40

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._audio_packets = 0
        self._last_video_pts = -1

    def next_audio_pts(self) -> int:
        with self._lock:
            pts = self._audio_packets * self.AV_AUDIO_MS
            self._audio_packets += 1
            return pts

    def next_video_pts(self, frame_index: int, fps: int) -> int:
        with self._lock:
            by_audio = self._audio_packets * self.AV_AUDIO_MS
            by_index = int(frame_index * 1000 / max(1, fps))
            pts = max(by_audio, by_index, self._last_video_pts + 1)
            self._last_video_pts = pts
            return pts

    def now_ms(self) -> int:
        with self._lock:
            return self._audio_packets * self.AV_AUDIO_MS


def _split_media_urls(cmd_inputs: list[tuple[str, str]]) -> list[str]:
    """把 (url, extra_headers) 摊平成 ffmpeg 输入参数。

    这三项都必须逐个输入重复：它们作用于「下一个 -i」，不是全局设置。
    """
    out: list[str] = []
    for url, headers in cmd_inputs:
        # -re 按原生速率读入。不加的话 ffmpeg 会把整部片子以解码速度一次性解完，
        # 实测 20 秒就推出了 85 MB（相当于 35 Mbps），设备端必被冲垮。
        out += ["-re"]
        if os.path.isfile(CA_FILE):
            out += ["-ca_file", CA_FILE]
        out += ["-user_agent", BILI_UA]
        if headers:
            out += ["-headers", headers]
        out += ["-i", url]
    return out


def build_ffmpeg(video_url: str, audio_url: str, video_port: int, audio_port: int,
                 headers: str = BILI_REFERER,
                 ffmpeg: str = "ffmpeg", fps: int = FPS) -> list[str]:
    """两个输入（B 站 DASH 音视频分离），两个回环 TCP 输出。

    画面量化到 rgb8（固定 3-3-2 调色板），关闭抖动与固件一致；音频转 16 kHz 单声道 s16le。
    ffmpeg 作为客户端连本地两个端口，所以调用前父进程必须先监听。
    """
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats"]
    cmd += _split_media_urls([(video_url, headers), (audio_url, headers)])
    cmd += ["-map", "0:v:0", "-vf", frames.FIT, "-pix_fmt", "rgb8",
            "-r", str(fps), "-f", "rawvideo", f"tcp://127.0.0.1:{video_port}"]
    cmd += ["-map", "1:a:0", "-ac", "1", "-ar", "16000",
            "-f", "s16le", f"tcp://127.0.0.1:{audio_port}"]
    return cmd


def _listen(port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(2)
    srv.settimeout(15.0)
    return srv


def _free_port_pair() -> tuple[int, int]:
    for base in range(20000, 40000, 2):
        try:
            a = socket.socket()
            a.bind(("127.0.0.1", base))
            b = socket.socket()
            b.bind(("127.0.0.1", base + 1))
        except OSError:
            a.close()
            b.close()
            continue
        a.close()
        b.close()
        return base, base + 1
    raise SessionError("找不到可用的回环端口")


def read_hello(conn: socket.socket) -> dict:
    """读设备发来的 HELLO 并校验形状，返回其 JSON。"""
    try:
        hello = receive_packet(conn, timeout=10.0)
    except (OSError, protocol.ProtocolError, EOFError, TimeoutError) as exc:
        raise SessionError(f"HELLO 读取失败: {exc}") from exc
    if hello.kind != Kind.HELLO or hello.session != 0 or hello.seq != 0 or hello.pts_ms != 0:
        raise SessionError("首个包不是合法 HELLO")
    try:
        body = protocol.json_object(hello.payload)
    except protocol.ProtocolError as exc:
        raise SessionError(f"HELLO 解析失败: {exc}") from exc
    if body.get("version") != 1:
        raise SessionError("HELLO 版本不支持")
    return body


def send_config(conn: socket.socket, session: int, fps: int = FPS,
                channel_name: str = "") -> None:
    """回 CONFIG。字段必须与上游 tv_server.py 的 CONFIG 完全一致。

    之前少发了 duration_ms / audio_lead_ms / video_lead_ms，又多发了 channel，
    设备校验不过就直接断连——现象是连上 2 秒就退，日志里只有 ffmpeg 的
    "Connection reset by peer"，看不出真正原因。

    channel / channel_list 决定播放器横幅显示什么名字。不给的话会话开始时
    会弹一条写着 "?" 的横幅（固件 current_channel_name 的兜底）。
    """
    name = (channel_name or "").strip()[:15]      # 名字字段 48 字节，中文 3 字节/字
    config = {
        "width": frames.WIDTH,
        "height": frames.HEIGHT,
        "fps": fps,
        "sample_rate": 16000,
        "channels": 1,
        "sample_bits": 16,
        "audio_codec": "ima_adpcm",
        "audio_chunk_ms": protocol.AUDIO_PCM_BYTES // 32,   # 1280/32 = 40 ms
        "video_max_bytes": frames.VIDEO_MAX,
        "stripe_rows": frames.STRIPE_ROWS,
        "duration_ms": 10000,
        "start_delay_ms": START_DELAY_MS,
        "audio_lead_ms": 320,
        "video_lead_ms": 200,
        "session": session,
    }
    if name:
        config["channel"] = "bili"                 # s.channel 只有 16 字节，用短 id
        config["channel_list"] = [{"id": "bili", "name": name}]
    payload = json_bytes(config)
    send_packet(conn, Packet(Kind.CONFIG, session, 0, 0, payload), timeout=5.0)


def stream(conn: socket.socket, resolve, stop: threading.Event | None = None,
           fps: int = FPS, log=print) -> None:
    """推完一路视频。

    `resolve(token)` 由调用方提供：用 HELLO 里的配对令牌查出该设备要看哪个视频，
    返回 (video_url, audio_url)。查不到就抛 SessionError。
    """
    stop = stop or threading.Event()
    body = read_hello(conn)
    video_url, audio_url, title = resolve(body.get("token"))
    session = int(time.time()) & 0x7FFFFFFF
    send_config(conn, session, fps, title)
    log(f"  session {session}：CONFIG 已发送，等待设备接受")
    sender = _Sender(conn, session)
    # PALETTE 紧随 CONFIG（占序号 1），先于所有帧。不发就是全黑。
    sender.send(Kind.PALETTE, 0, rgb332_palette())
    encoder = adpcm.Encoder()
    clock = _Clock()

    vport, aport = _free_port_pair()
    vlistener = _listen(vport)
    alistener = _listen(aport)
    proc = None
    threads: list[threading.Thread] = []
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def video_loop() -> None:
        try:
            vsock, _ = vlistener.accept()
        except OSError as exc:
            log(f"  视频端口未连上: {exc}")
            stop.set()
            return
        vsock.settimeout(10.0)
        frames_sent = 0
        shown = bytearray(FRAME_BYTES)     # 设备当前画面，用于算差异
        have_shown = False
        # 每 45 帧强制整帧一次：万一中途丢包，设备画面能在两秒内自愈。
        FULL_REFRESH_EVERY = 45
        SP = frames.STRIPE_PIXELS
        with vsock:
            while not stop.is_set():
                raw = _recv_pcm(vsock, FRAME_BYTES, stop)
                if raw is None:
                    log(f"  视频源结束（已发 {frames_sent} 帧）")
                    break
                # 已回滚：增量发送（只发变化条带）在这块固件上会让帧率掉到 1~2 帧，
                # 说明设备是"集齐一帧的条带才绘制"，收到零长度条带就一直在等。
                # 恢复整帧发送。
                mask = None
                compressed = frames.compress_stripes(raw, mask)
                payloads = frames.pack_stripes(compressed)
                pts = clock.next_video_pts(frames_sent, fps)
                try:
                    for i, payload in enumerate(payloads):
                        sender.send(Kind.JPEG, pts, payload,
                                    protocol.VIDEO_CONTINUES if i else 0)
                except SessionError as exc:
                    log(f"  视频发送失败（第 {frames_sent + 1} 帧）: {exc}")
                    stop.set()
                    break
                shown[:] = raw
                have_shown = True
                frames_sent += 1
                if frames_sent == 1:
                    log(f"  首帧已发出（{len(payloads)} 个包，{len(raw)} 字节原图）")
        stop.set()

    def audio_loop() -> None:
        try:
            asock, _ = alistener.accept()
        except OSError as exc:
            log(f"  音频端口未连上: {exc}")
            stop.set()
            return
        asock.settimeout(10.0)
        chunks = 0
        with asock:
            while not stop.is_set():
                pcm = _recv_pcm(asock, PCM_CHUNK, stop)
                if pcm is None:
                    log(f"  音频源结束（已发 {chunks} 块）")
                    break
                try:
                    pts = clock.next_audio_pts()
                    if not AUDIO_ENABLED:
                        chunks += 1
                        continue      # 时钟已推进，但不下发音频包
                    sender.send(Kind.PCM, pts, encoder.encode_pcm(pcm))
                except SessionError as exc:
                    log(f"  音频发送失败: {exc}")
                    stop.set()
                    break
                chunks += 1
        stop.set()

    try:
        proc = subprocess.Popen(build_ffmpeg(video_url, audio_url, vport, aport, fps=fps),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for target in (video_loop, audio_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            threads.append(t)

        # 任一方向结束（视频读完）就算播完；音频线程可能还在等，用超时兜住。
        while not stop.is_set() and proc.poll() is None:
            if all(not t.is_alive() for t in threads):
                break
            time.sleep(0.2)
    finally:
        stop.set()
        vlistener.close()
        alistener.close()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for t in threads:
            t.join(timeout=2)
        err = b""
        if proc and proc.stderr:
            try:
                err = proc.stderr.read() or b""
            except OSError:
                err = b""
            proc.stderr.close()
        if err:
            log(f"  ffmpeg: {err.decode('utf-8', 'replace')[-400:]}")
        try:
            sender.send(Kind.END, clock.now_ms(), b"")
        except SessionError:
            pass
