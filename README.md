# AI Passport · bilibili 客户端

把 [FoloToy AI Passport](https://github.com/FoloToy/ai-passport)（ESP32-C3 小电视）变成一台 B 站播放器：设备上浏览推荐流、播放视频、看收藏夹和私信，服务端负责拉流、转码和协议封装。

设备只有 2 MB 可用堆和一块 320×240 的屏，所以整套东西是围绕"**用最少的带宽和解码成本，让 C3 画出能看的画面**"设计的。

```
┌──────────────┐   HTTP/JSON    ┌─────────────────────┐
│  AI Passport │ ◄────────────► │  服务端 (Linux)      │
│  ESP32-C3    │  :10745 控制   │  FastAPI 风格纯标准库 │
│              │                │                     │
│              │  FAV1 媒体流   │  ffmpeg 拉流 + 缩放  │
│              │ ◄───────────── │  条带压缩 + 协议封装  │
└──────────────┘  :10746 音视频 └─────────┬───────────┘
                                          │ HTTPS
                                          ▼
                                   bilibili CDN / API
```

## 功能

| 功能 | 状态 | 说明 |
|---|---|---|
| 推荐流浏览 | ✅ | 上下键翻页，标题/UP 主/时长/播放量 |
| 视频播放 | ✅ | 320×180 @ 12–15 fps，16 kHz 单声道 |
| 评论 | ✅ | 每页 12 条 |
| 个人主页 | ✅ | 昵称、等级、关注/粉丝/收藏数、硬币 |
| 我的收藏夹 | ✅ | 收藏夹列表 → 视频列表 → 直接播放 |
| 私信 | ✅ | 会话列表 + 进入看历史对话（只读） |
| 播放中按 OK | ✅ | 仅音频 / 静音 / 收藏 当前视频 |
| 音量、亮度 | ✅ | 长按上下键调音量，OK 双击调亮度 |
| 发弹幕、发私信 | ❌ | 未实现（协议是只读的） |

## 硬件与前置条件

- **FoloToy AI Passport**（ESP32-C3，8 MB Flash，无 PSRAM）
- 一台 **Linux 服务器**（1 核 1 GB 起，实测 2 核 896 MB 够用；macOS 也能跑）
- 服务器需要能访问 bilibili（境外机器请自行确认）
- **上行带宽 ≥ 3 Mbps**：12 fps 实测约 2 Mbps，15 fps 约 2.5 Mbps

## 快速开始

### 1. 服务端

```bash
git clone <本仓库>
cd ai-passport-bili
sudo bash deploy/install.sh
```

脚本会：装 Python 3.11 + ffmpeg → 建虚拟环境 → 交互式收集 B 站 Cookie 和配对码 → 注册 systemd 服务 → 起服务并自检。

无人值守安装（配置已存在时）：

```bash
sudo bash deploy/install.sh --yes
```

常用命令：

```bash
sudo bash deploy/install.sh --show-pairing   # 查看配对码
journalctl -u passport-bili -f               # 看日志
curl localhost:10745/api/health              # 自检
```

配置文件在 `/etc/passport-bili/env`（权限 600）：

```ini
BILI_SESSDATA=...          # 必填，否则只能推荐流游客模式
BILI_BILI_JCT=...          # 收藏要用
BILI_DEDEUSERID=...
BILI_BUVID3=...
TV_PORT=10745              # 控制端口，媒体端口 = 控制端口 + 1
TV_PAIRING_CODE=ABCD       # 设备配网时填这个
TV_QUALITY=standard        # smooth / standard / ultra
TV_FPS=12                  # 实测 12~15 是 C3 的舒适区
TV_ZLIB_LEVEL=3            # 条带压缩级别，6 会把 2 核 VPS 跑满
```

### 2. 固件

```bash
# 需要 ESP-IDF v5.5
./tools/build.sh                      # 编译
./tools/flash.sh COM8                 # 只写应用分区，保留配网信息
./tools/flash.sh COM8 --full          # 完整镜像，等于恢复出厂
```

### 3. 配网

1. 设备上电 → 连热点 `PassportTV-xxxx` → 打开 `http://192.168.4.1`
2. 填服务器地址、端口、配对码 → 保存
3. 设备重启后进入推荐列表

> 配网页文案还是 Passport TV 原有的"电视服务器/连接电视服务"，功能正常，只是没改成 B 站的说法。

## 服务端 API

控制通道走 HTTP/JSON（默认 10745），媒体通道是自定义二进制协议（10746）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查、登录状态、已配对数 |
| GET | `/api/feed?page=N` | 推荐流，每页 20 条 |
| POST | `/api/play` | 登记待播视频 `{device, bvid}` |
| GET | `/api/comments?aid=` | 评论 |
| GET | `/api/me` | 账号概要 |
| GET | `/api/favorites` | 收藏夹列表 |
| GET | `/api/favorites/items?media_id=` | 收藏夹内容 |
| POST | `/api/favorite` | 收藏 `{device, bvid}` |
| GET | `/api/messages` | 私信会话 |
| GET | `/api/messages/thread?talker=` | 会话历史 |
| POST | `/api/tv/activate` | 设备配对，返回 `{host, port, ticket}` |
| POST | `/api/tv/config` | 已配对设备刷新连接信息 |

`/social/tv/*` 是同样两个配对接口的别名——固件里用的是旧路径，两边都认，省得为了改路径重烧固件。

## 实现要点

**为什么是 320×180 而不是满屏 320×240？**
面板是 320×240，视频用 320×180（16:9）上下留白，行数 180 能被 12 整除，正好 15 个条带。固件里 `AV_CONFIG_GEOMETRIES` 只允许 `320x240`、`280x210`、`320x180` 三种，其余组合会被设备拒绝。

**为什么是 RGB332 + 条带？**
一帧 320×180 的 RGB565 要 115 KB，C3 没有 PSRAM 放不下双缓冲。RGB332 一帧 57.6 KB，切成 15 个条带分别压缩，设备解压一条画一条，内存峰值只有两个条带。

**音频为什么是 16 kHz ADPCM？**
40 ms 一块、每块 324 字节，I2S DMA 能稳稳吃掉；换成 PCM 体积翻 4 倍。音视频分离送达，设备用音频时钟给视频排期。

## 踩过的坑（重要）

这些都是实际调通才发现的，注释里也写了，这里汇总一份：

1. **B 站 CDN 必须带合法 User-Agent。** 只带 Referer 是 403。ffmpeg 默认 UA 是 `Lavf/x.y.z`，直接被判爬虫。

2. **静态版 ffmpeg 在 TLS 失败时会段错误。** 现象是"连上就断、日志只有 ffmpeg dumped core"，没有任何错误信息。换 BtbN 的构建才能看到真正的原因（证书校验失败）。

3. **静态版不认系统证书路径。** 必须显式 `-ca_file /etc/pki/tls/certs/ca-bundle.crt`，而且这类"按输入生效"的选项**每个 `-i` 前面都要重复**——只给第一个输入，第二个输入照样 TLS 失败。

4. **必须给 `-re`。** 不加的话 ffmpeg 以解码速度把整部片子一次性解完，实测 20 秒推了 85 MB，设备必被冲垮。

5. **时间轴是硬约束。** 固件 `av_stream_accept()` 要求音频 pts **严格等差 40 ms**（0/40/80…），视频新帧 pts 严格递增。用墙上时钟必被拒，表现为会话建立 2 秒就断。

6. **必须发 PALETTE。** 固件会话开始只把 `palette_ready` 置 false，调色板缓冲是 calloc 的全零，**不会**自动回填固定调色板。不发就是"有声无图、画面全黑"。

7. **配对接口用 403 表示"未配对"，不是 404。** 固件里写死 `status==403 → ESP_ERR_NOT_FOUND`，其它非 200 一律当"响应非法"直接中止，永远走不到 activate。

8. **`pump_keys()` 的返回值是"要换台"，不是"我处理了按键"。** 新加的菜单动作如果 `return true`，会话层会走收尾流程，worker 没在 5 秒内退出就 `esp_restart()`——表现是"按一下菜单，设备过一会儿自己重启"。

9. **私信历史不在 `session_svr` 下。** 列会话是 `/session_svr/v1/session_svr/get_sessions`，读历史是 `/svr_sync/v1/svr_sync/fetch_session_msgs`。按"同类接口同前缀"的直觉去猜，会一直 404。

10. **增量发送（只发变化条带）在这版固件上不可用。** 协议文档说零长度条带"设备不重绘"，实测设备是"集齐一帧的条带才绘制"，收到不完整的帧就一直等，帧率掉到 1~2。已回退为整帧发送。

## 已知限制

- **帧率天花板**：C3 要 inflate 15 个条带 + 扩成 RGB565 + SPI 刷屏，12–15 fps 是舒适区。想更流畅得降分辨率（`AV_CONFIG_GEOMETRIES` 里加档位 + 重烧固件）。
- **只支持单频道**：`channel_list` 只塞了一个"当前视频"，没有频道概念。
- **私信只读**：能看不能发。
- **Cookie 会过期**：`SESSDATA` 通常几个月，失效后推荐流还在但收藏/私信会报未登录，去 `/etc/passport-bili/env` 换新的再重启服务。
- **配网页文案**未本地化（仍是 Passport TV 那套）。

## 目录结构

```
service/passport_bili/    服务端
  __main__.py              HTTP 路由 + FAV1 监听 + 配对
  bili.py                  bilibili API 封装（含 WBI 签名）
  stream.py                ffmpeg 转码 + 协议打包
  tv/                      传输层：协议、条带、ADPCM
deploy/install.sh          一键部署
firmware/main/             业务层：列表/评论/个人主页/收藏/私信
firmware/components/
  passport_tv/             播放器、协议、UI（衍生自 Passport TV）
  passport_wifi/           配网
  bsp/                     板级驱动
tools/                     编译、烧录、假设备测试、串口抓取
```

`tools/fakedev.py` 是个**假设备**：用真实协议连服务端，校验 CONFIG 字段、pts 时间轴、条带长度、调色板。改服务端之后先跑它，比烧固件快得多。

## 致谢与来源

本项目是衍生作品，站在这些肩膀之上：

- **[Passport TV](https://github.com/FoloToy/ai-passport)** (MIT, © 2026 FoloToy) — 播放器固件、传输协议、服务端骨架都源自这里。没有它就没有这个项目。
- **ESP-IDF**、**LVGL**、**esp_lvgl_port**、**esp_codec_dev** — 乐鑫与 LVGL 团队。
- **yi-huang-1/bilibili-API-collect** — 接口文档（私信那个坑就是靠它的社区镜像找到的）。

## 免责声明

仅供**个人学习**使用，请勿用于商业用途或大规模抓取。

项目需要你的 B 站 Cookie 才能访问收藏和私信。Cookie 等同于账号凭据，**只放在自己可控的服务器上**，不要提交进仓库、不要发到群里。刷弹幕、批量下载等行为可能违反 bilibili 用户协议，本项目不提供也不鼓励这类功能。

本项目与 bilibili 官方无关。

## License

MIT，见 [LICENSE](LICENSE)（含上游 FoloToy 的版权声明）。
