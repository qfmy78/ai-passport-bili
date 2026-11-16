// 列表界面绘制。只用 ui_text 的条带接口，不依赖 LVGL。
#pragma once

#include <stdint.h>

#include "bili_http.h"

// 传入播放器用的 DMA 条带缓冲（>= 320*12*2 字节），UI 复用它绘图。
void bili_ui_init(uint8_t *stripes);

// 推荐/收藏夹/私信共用的列表页。
void bili_ui_list(const bili_list_t *list, const char *header, const char *footer);

// 居中提示页（正在加载、失败、已暂停…）。
void bili_ui_message(const char *title, const char *hint);

// 评论页：一行一条，作者与正文同屏。
void bili_ui_comments(const bili_comment_list_t *list, const char *header);

// 个人主页：上方是账号概要，下方是三个入口。
#define BILI_PROFILE_ENTRIES 3
void bili_ui_profile(const bili_me_t *me, unsigned selected);

// 收藏夹列表。
void bili_ui_folders(const bili_folder_list_t *list);

// 私信列表（只读）。
void bili_ui_messages(const bili_msg_list_t *list);
