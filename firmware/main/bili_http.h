// B 站客户端：局域网 HTTP 客户端。
//
// 设备不做 WBI 签名、不碰 Cookie —— 全部由服务端（那台 Linux）负责。
// 这里只负责把瘦 JSON 拉下来，塞进定长结构体。
//
// 所有缓冲都是静态的：ESP32-C3 无 PSRAM，运行时空闲堆只有约 68 KB，
// 播放器已经吃掉 45 KB 视频缓冲，UI 必须抠着用。
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define BILI_LIST_MAX   20
#define BILI_TITLE_MAX  84     // 26 个汉字 * 3 字节 + 余量
#define BILI_UP_MAX     40
#define BILI_BVID_MAX   16
#define BILI_COMMENT_MAX 12
#define BILI_COMMENT_TEXT_MAX 96

typedef struct {
    char bvid[BILI_BVID_MAX];
    char title[BILI_TITLE_MAX];
    char up[BILI_UP_MAX];
    uint32_t duration;   // 秒
    uint32_t views;
} bili_video_t;

typedef struct {
    bili_video_t items[BILI_LIST_MAX];
    unsigned count;
    unsigned selected;
    unsigned scroll;
} bili_list_t;

typedef struct {
    char user[BILI_UP_MAX];
    char text[BILI_COMMENT_TEXT_MAX];
} bili_comment_t;

typedef struct {
    bili_comment_t items[BILI_COMMENT_MAX];
    unsigned count;
    unsigned selected;
    unsigned scroll;
} bili_comment_list_t;

typedef struct {
    char name[BILI_UP_MAX];
    uint32_t level;
    uint32_t coins;
    uint32_t following;
    uint32_t follower;
    uint32_t favs;
    bool vip;
} bili_me_t;

typedef struct {
    char id[16];
    char name[BILI_UP_MAX];
    uint32_t count;
} bili_folder_t;

typedef struct {
    bili_folder_t items[8];
    unsigned count;
    unsigned selected;
} bili_folder_list_t;

typedef struct {
    char user[BILI_UP_MAX];
    char text[BILI_COMMENT_TEXT_MAX];
} bili_msg_t;

typedef struct {
    bili_msg_t items[10];
    unsigned count;
    unsigned selected;
    unsigned scroll;
} bili_msg_list_t;

// 拉一页推荐流。失败时 out->count 归零，返回错误码供 UI 显示。
esp_err_t bili_fetch_feed(const char *host, uint16_t port, bili_list_t *out,
                          unsigned page);
// 拉某个视频的评论。
esp_err_t bili_fetch_comments(const char *host, uint16_t port, uint32_t aid,
                              bili_comment_list_t *out);
// 告诉服务端"我要看这个视频"，服务端据此启动转码；随后再起 FAV1 会话。
esp_err_t bili_request_play(const char *host, uint16_t port, const char *device,
                            const char *bvid);
// 把正在播的视频收进收藏夹。
esp_err_t bili_request_favorite(const char *host, uint16_t port, const char *device,
                                const char *bvid);
// 暂停/恢复：服务端据此停止或继续推进转码，画面真正定格而不是丢帧。
esp_err_t bili_request_pause(const char *host, uint16_t port, const char *device,
                             bool paused);
// 设备唯一 ID：12 位小写十六进制，取自出厂 MAC。
void bili_device_id(char out[13]);

// 个人主页：账号概要。
esp_err_t bili_fetch_me(const char *host, uint16_t port, bili_me_t *out);
// 收藏夹列表。
esp_err_t bili_fetch_folders(const char *host, uint16_t port, bili_folder_list_t *out);
// 某个收藏夹里的视频，复用推荐列表的结构。
esp_err_t bili_fetch_folder_items(const char *host, uint16_t port, const char *media_id,
                                  bili_list_t *out);
// 私信（只读文字）。
esp_err_t bili_fetch_messages(const char *host, uint16_t port, bili_msg_list_t *out);
// 某个会话的历史消息。
esp_err_t bili_fetch_thread(const char *host, uint16_t port, const char *talker,
                            bili_msg_list_t *out);
