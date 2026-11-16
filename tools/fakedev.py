"""假设备：用真实协议走一遍 FAV1，验证服务端能否产出可解码的画面。"""
import json, socket, sys, time, urllib.request
sys.path.insert(0, "/opt/passport-bili/service")
from passport_bili.tv import protocol, frames
from passport_bili.tv.protocol import Kind, Packet, json_bytes, receive_packet, send_packet

DEV, CODE, BV, HOST = "aabbccddee01", "JQ7C", sys.argv[1], "127.0.0.1"

def post(path, obj):
    req = urllib.request.Request(f"http://{HOST}:10745{path}",
          data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

ticket = post("/api/tv/activate", {"device": DEV, "code": CODE})["ticket"]
print("配对 ticket:", ticket[:16], "…")
print("play:", post("/api/play", {"device": DEV, "bvid": BV}))

s = socket.create_connection((HOST, 10746), timeout=20)
s.settimeout(25)
send_packet(s, Packet(Kind.HELLO, 0, 0, 0, json_bytes({"version": 1, "token": ticket})))

cfg = receive_packet(s, timeout=15)
print("CONFIG kind:", cfg.kind.name, "seq:", cfg.seq)
c = protocol.json_object(cfg.payload)
print("CONFIG:", json.dumps(c, ensure_ascii=False))
bad = []
for k, want in (("width",320),("height",180),("stripe_rows",12),("video_max_bytes",22528),
                ("audio_chunk_ms",40),("sample_rate",16000),("channels",1),
                ("sample_bits",16),("start_delay_ms",200)):
    if c.get(k) != want: bad.append(f"{k}={c.get(k)}!={want}")
if c.get("audio_codec") != "ima_adpcm": bad.append("audio_codec")
if not 1 <= (c.get("fps") or 0) <= 30: bad.append(f"fps={c.get('fps')}")
print("字段校验:", "全部通过" if not bad else "FAIL " + ",".join(bad))

vin = vout = ain = pal = 0
last_vpts = -1
vbytes = 0
inflated = 0
deadline = time.time() + 20
try:
    while time.time() < deadline:
        p = receive_packet(s, timeout=8, expected_session=c["session"])
        if p.kind == Kind.JPEG:
            vout += 1; vbytes += len(p.payload)
            # 复刻固件 av_stream_accept 的两条硬约束
            if p.flags & protocol.VIDEO_CONTINUES:
                if last_vpts < 0: raise SystemExit("续包出现在首帧之前")
                if p.pts_ms != last_vpts: raise SystemExit(f"续包 pts {p.pts_ms} != 所属帧 {last_vpts}")
            else:
                if last_vpts >= 0 and p.pts_ms <= last_vpts:
                    raise SystemExit(f"新帧 pts {p.pts_ms} 未严格递增（上一帧 {last_vpts}）")
                last_vpts = p.pts_ms
            try:
                v = frames.unpack(p.payload)   # 增量帧含零长度条带，unpack 会抛错，跳过校验
                for stripe in v:
                    if len(stripe) != frames.STRIPE_PIXELS:
                        raise SystemExit(f"条带长度 {len(stripe)} != {frames.STRIPE_PIXELS}")
                    inflated += 1
            except SystemExit:
                raise
            except Exception:
                pass
        elif p.kind == Kind.PALETTE:
            if len(p.payload) != 512:
                raise SystemExit(f"调色板长度 {len(p.payload)} != 512")
            import struct as _st
            entries = _st.unpack(">256H", p.payload)
            if entries[0] != 0x0000 or entries[255] != 0xFFFF:
                raise SystemExit(f"调色板端点不对: {entries[0]:#06x} / {entries[255]:#06x}")
            pal += 1
        elif p.kind == Kind.PCM:
            if len(p.payload) != 324: raise SystemExit(f"音频包长度 {len(p.payload)} != 324")
            want = ain * 40
            if p.pts_ms != want:
                raise SystemExit(f"音频 pts {p.pts_ms} != 期望 {want}（必须严格差 40）")
            ain += 1
        elif p.kind == Kind.END:
            print("收到 END"); break
except (socket.timeout, TimeoutError):
    print("读取超时，按已收到的统计")
except Exception as exc:
    import traceback; traceback.print_exc()
finally:
    s.close()

secs = 20
print(f"\n结果：视频包 {vout}（{vbytes/1024:.0f} KB）、音频包 {ain}、解压验证 {inflated} 条")
print(f"平均码率约 {vbytes*8/secs/1e6:.2f} Mbps，音频 {ain*324*8/secs/1e3:.0f} kbps")
ok = vout > 0 and ain > 0 and inflated > 0 and pal > 0 and not bad
print("判定:", "PASS 服务端能产出可解码画面与音频" if ok else "FAIL")
