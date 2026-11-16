#include "bili_ui.h"

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "bsp_battery.h"
#include "bsp_display.h"
#include "ui_text.h"

#define SCR_W          320
#define SCR_H          240
#define STRIPE_ROWS    12
#define HEADER_H       20
#define FOOTER_Y       222
#define ROW_H          40
#define ROWS_VISIBLE   5
#define CMT_ROW_H      34
#define CMT_VISIBLE    5

static uint8_t *g_buf;

void bili_ui_init(uint8_t *stripes)
{
    g_buf = stripes;
}

typedef void (*paint_fn)(ui_surface_t s, const void *ctx);

// 整屏重绘：逐条带画满 240 行。20 条 × 7.5 KB ≈ 150 KB 走 SPI，40 MHz 下约 30 ms。
static void render(paint_fn fn, const void *ctx)
{
    if (!g_buf || bsp_display_raw_claim() != ESP_OK) {
        return;
    }
    for (int y = 0; y < SCR_H; y += STRIPE_ROWS) {
        ui_surface_t s = { g_buf, SCR_W, y, STRIPE_ROWS };
        fn(s, ctx);
        if (bsp_display_raw_submit_nowait(y, STRIPE_ROWS, g_buf) != ESP_OK) {
            break;
        }
        if (bsp_display_raw_drain(1, 200) != ESP_OK) {
            break;
        }
    }
    (void)bsp_display_raw_drain(32, 200);
    (void)bsp_display_raw_release();
}

static void draw_header(ui_surface_t s, const char *title)
{
    ui_text_fill(s, 0, 0, SCR_W, HEADER_H, UI_COLOR_BACKDROP);
    ui_text_fill(s, 0, HEADER_H - 1, SCR_W, 1, UI_COLOR_RULE);
    if (title && title[0]) {
        char clipped[48];
        ui_text_truncate(clipped, sizeof clipped, title, SCR_W - 70);
        ui_text_draw(s, 6, 2, clipped, UI_COLOR_TEXT, UI_COLOR_BACKDROP);
    }
    int level = bsp_battery_soc();
    char soc[16];
    if (level < 0) {
        snprintf(soc, sizeof soc, "---");
    } else {
        snprintf(soc, sizeof soc, "%d%%", level);
    }
    uint16_t colour = (level >= 0 && level <= 10) ? 0xF800 : UI_COLOR_DIM;
    ui_text_draw(s, SCR_W - 6 - ui_text_width(soc), 2, soc, colour, UI_COLOR_BACKDROP);
}

static void draw_footer(ui_surface_t s, const char *hint)
{
    ui_text_fill(s, 0, FOOTER_Y, SCR_W, SCR_H - FOOTER_Y, UI_COLOR_BACKDROP);
    ui_text_fill(s, 0, FOOTER_Y, SCR_W, 1, UI_COLOR_RULE);
    if (hint && hint[0]) {
        char clipped[64];
        ui_text_truncate(clipped, sizeof clipped, hint, SCR_W - 12);
        ui_text_draw(s, 6, FOOTER_Y + 2, clipped, UI_COLOR_DIM, UI_COLOR_BACKDROP);
    }
}

// 整数运算拼小数，避开 newlib nano 格式化不支持 %f 的坑。
static void fmt_views(char *out, size_t cap, uint32_t n)
{
    if (n >= 100000000u) {
        snprintf(out, cap, "%u.%u亿", (unsigned)(n / 100000000u),
                 (unsigned)((n % 100000000u) / 10000000u));
    } else if (n >= 10000u) {
        snprintf(out, cap, "%u.%u万", (unsigned)(n / 10000u),
                 (unsigned)((n % 10000u) / 1000u));
    } else {
        snprintf(out, cap, "%u", (unsigned)n);
    }
}

static void fmt_duration(char *out, size_t cap, uint32_t sec)
{
    if (sec >= 3600u) {
        snprintf(out, cap, "%u:%02u:%02u", (unsigned)(sec / 3600u),
                 (unsigned)(sec / 60u % 60u), (unsigned)(sec % 60u));
    } else {
        snprintf(out, cap, "%02u:%02u", (unsigned)(sec / 60u), (unsigned)(sec % 60u));
    }
}

typedef struct {
    const bili_list_t *list;
    const char *header;
    const char *footer;
} list_ctx_t;

static void paint_list(ui_surface_t s, const void *vctx)
{
    const list_ctx_t *ctx = (const list_ctx_t *)vctx;
    const bili_list_t *l = ctx->list;

    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    draw_header(s, ctx->header);

    for (unsigned i = 0; i < ROWS_VISIBLE; i++) {
        unsigned idx = l->scroll + i;
        if (idx >= l->count) {
            break;
        }
        const bili_video_t *v = &l->items[idx];
        int y = HEADER_H + 2 + (int)i * ROW_H;
        bool sel = (idx == l->selected);
        uint16_t bg = sel ? UI_COLOR_SELECT : UI_COLOR_BOX;
        uint16_t fg = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_TEXT;
        uint16_t dim = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_DIM;

        if (sel) {
            ui_text_fill(s, 0, y - 1, SCR_W, ROW_H, bg);
        }
        char line[112];
        ui_text_truncate(line, sizeof line, v->title, SCR_W - 20);
        ui_text_draw(s, 8, y + 3, line, fg, bg);

        char dur[16], views[24], meta[144];
        fmt_duration(dur, sizeof dur, v->duration);
        fmt_views(views, sizeof views, v->views);
        snprintf(meta, sizeof meta, "@%s  %s  %s", v->up, dur, views);
        ui_text_truncate(line, sizeof line, meta, SCR_W - 20);
        ui_text_draw(s, 8, y + 21, line, dim, bg);
    }

    draw_footer(s, ctx->footer);
}

void bili_ui_list(const bili_list_t *list, const char *header, const char *footer)
{
    list_ctx_t ctx = { .list = list, .header = header, .footer = footer };
    render(paint_list, &ctx);
}

typedef struct {
    const char *title;
    const char *hint;
} msg_ctx_t;

static void paint_message(ui_surface_t s, const void *vctx)
{
    const msg_ctx_t *ctx = (const msg_ctx_t *)vctx;
    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    if (ctx->title && ctx->title[0]) {
        char line[96];
        ui_text_truncate(line, sizeof line, ctx->title, SCR_W - 40);
        ui_text_draw(s, (SCR_W - ui_text_width(line)) / 2, 104, line,
                     UI_COLOR_TEXT, UI_COLOR_BOX);
    }
    if (ctx->hint && ctx->hint[0]) {
        char line[96];
        ui_text_truncate(line, sizeof line, ctx->hint, SCR_W - 40);
        ui_text_draw(s, (SCR_W - ui_text_width(line)) / 2, 132, line,
                     UI_COLOR_DIM, UI_COLOR_BOX);
    }
}

void bili_ui_message(const char *title, const char *hint)
{
    msg_ctx_t ctx = { .title = title, .hint = hint };
    render(paint_message, &ctx);
}

typedef struct {
    const bili_comment_list_t *list;
    const char *header;
} cmt_ctx_t;

static void paint_comments(ui_surface_t s, const void *vctx)
{
    const cmt_ctx_t *ctx = (const cmt_ctx_t *)vctx;
    const bili_comment_list_t *l = ctx->list;

    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    draw_header(s, ctx->header);

    for (unsigned i = 0; i < CMT_VISIBLE; i++) {
        unsigned idx = l->scroll + i;
        if (idx >= l->count) {
            break;
        }
        const bili_comment_t *c = &l->items[idx];
        int y = HEADER_H + 2 + (int)i * CMT_ROW_H;
        bool sel = (idx == l->selected);
        uint16_t bg = sel ? UI_COLOR_SELECT : UI_COLOR_BOX;
        uint16_t fg = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_TEXT;
        uint16_t dim = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_DIM;

        if (sel) {
            ui_text_fill(s, 0, y - 1, SCR_W, CMT_ROW_H, bg);
        }
        char line[128];
        snprintf(line, sizeof line, "@%s", c->user);
        ui_text_truncate(line, sizeof line, line, SCR_W - 20);
        ui_text_fill(s, 0, y + 16, SCR_W, 1, UI_COLOR_RULE);
        ui_text_draw(s, 8, y + 1, line, dim, bg);
        ui_text_truncate(line, sizeof line, c->text, SCR_W - 20);
        ui_text_draw(s, 8, y + 17, line, fg, bg);
    }

    draw_footer(s, "长按OK 返回 · 上下键 滚动");
}

void bili_ui_comments(const bili_comment_list_t *list, const char *header)
{
    cmt_ctx_t ctx = { .list = list, .header = header };
    render(paint_comments, &ctx);
}

// ---- 个人主页 ---------------------------------------------------------------
static const char *const PROFILE_ENTRIES[BILI_PROFILE_ENTRIES] = {
    "我的收藏夹", "私信", "设置"
};

typedef struct {
    const bili_me_t *me;
    unsigned selected;
} profile_ctx_t;

static void paint_profile(ui_surface_t s, const void *vctx)
{
    const profile_ctx_t *ctx = (const profile_ctx_t *)vctx;
    const bili_me_t *me = ctx->me;

    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    draw_header(s, "个人主页");

    // 昵称 + 等级
    char line[96];
    ui_text_truncate(line, sizeof line, me->name, SCR_W - 90);
    ui_text_draw(s, 8, 26, line, UI_COLOR_TEXT, UI_COLOR_BOX);
    if (me->vip) {
        snprintf(line, sizeof line, "Lv.%u VIP", (unsigned)me->level);
    } else {
        snprintf(line, sizeof line, "Lv.%u", (unsigned)me->level);
    }
    ui_text_draw(s, SCR_W - 8 - ui_text_width(line), 26, line, UI_COLOR_SELECT, UI_COLOR_BOX);

    ui_text_fill(s, 8, 46, SCR_W - 16, 1, UI_COLOR_RULE);

    // 统计两行
    snprintf(line, sizeof line, "关注 %u    粉丝 %u",
             (unsigned)me->following, (unsigned)me->follower);
    ui_text_draw(s, 8, 52, line, UI_COLOR_DIM, UI_COLOR_BOX);
    snprintf(line, sizeof line, "收藏夹 %u    硬币 %u",
             (unsigned)me->favs, (unsigned)me->coins);
    ui_text_draw(s, 8, 70, line, UI_COLOR_DIM, UI_COLOR_BOX);

    ui_text_fill(s, 8, 92, SCR_W - 16, 1, UI_COLOR_RULE);

    // 三个入口
    for (unsigned i = 0; i < BILI_PROFILE_ENTRIES; i++) {
        int y = 100 + (int)i * 30;
        bool sel = (i == ctx->selected);
        uint16_t bg = sel ? UI_COLOR_SELECT : UI_COLOR_BOX;
        uint16_t fg = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_TEXT;
        if (sel) {
            ui_text_fill(s, 4, y - 3, SCR_W - 8, 26, bg);
        }
        ui_text_draw(s, 12, y, sel ? ">" : " ", fg, bg);
        ui_text_draw(s, 24, y, PROFILE_ENTRIES[i], fg, bg);
    }

    draw_footer(s, "OK 进入 · 长按OK 返回");
}

void bili_ui_profile(const bili_me_t *me, unsigned selected)
{
    profile_ctx_t ctx = { .me = me, .selected = selected };
    render(paint_profile, &ctx);
}

// ---- 收藏夹 ----------------------------------------------------------------
typedef struct { const bili_folder_list_t *list; } folder_ctx_t;

static void paint_folders(ui_surface_t s, const void *vctx)
{
    const folder_ctx_t *ctx = (const folder_ctx_t *)vctx;
    const bili_folder_list_t *l = ctx->list;

    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    draw_header(s, "我的收藏夹");

    for (unsigned i = 0; i < l->count && i < 6; i++) {
        int y = HEADER_H + 4 + (int)i * 32;
        bool sel = (i == l->selected);
        uint16_t bg = sel ? UI_COLOR_SELECT : UI_COLOR_BOX;
        uint16_t fg = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_TEXT;
        uint16_t dim = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_DIM;
        if (sel) {
            ui_text_fill(s, 0, y - 3, SCR_W, 30, bg);
        }
        char line[96];
        ui_text_truncate(line, sizeof line, l->items[i].name, SCR_W - 100);
        ui_text_draw(s, 8, y, line, fg, bg);
        snprintf(line, sizeof line, "%u 个", (unsigned)l->items[i].count);
        ui_text_draw(s, SCR_W - 8 - ui_text_width(line), y, line, dim, bg);
    }

    draw_footer(s, "OK 打开 · 长按OK 返回");
}

void bili_ui_folders(const bili_folder_list_t *list)
{
    folder_ctx_t ctx = { .list = list };
    render(paint_folders, &ctx);
}

// ---- 私信 ------------------------------------------------------------------
typedef struct { const bili_msg_list_t *list; } msg_ctx2_t;

static void paint_messages(ui_surface_t s, const void *vctx)
{
    const msg_ctx2_t *ctx = (const msg_ctx2_t *)vctx;
    const bili_msg_list_t *l = ctx->list;

    ui_text_fill(s, 0, 0, SCR_W, SCR_H, UI_COLOR_BOX);
    draw_header(s, "私信");

    for (unsigned i = 0; i < 5; i++) {
        unsigned idx = l->scroll + i;
        if (idx >= l->count) {
            break;
        }
        int y = HEADER_H + 2 + (int)i * CMT_ROW_H;
        bool sel = (idx == l->selected);
        uint16_t bg = sel ? UI_COLOR_SELECT : UI_COLOR_BOX;
        uint16_t fg = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_TEXT;
        uint16_t dim = sel ? UI_COLOR_SELECT_TEXT : UI_COLOR_DIM;
        if (sel) {
            ui_text_fill(s, 0, y - 1, SCR_W, CMT_ROW_H, bg);
        }
        char line[128];
        snprintf(line, sizeof line, "UID %s", l->items[idx].user);
        ui_text_draw(s, 8, y + 1, line, dim, bg);
        ui_text_fill(s, 0, y + 16, SCR_W, 1, UI_COLOR_RULE);
        ui_text_truncate(line, sizeof line, l->items[idx].text, SCR_W - 20);
        ui_text_draw(s, 8, y + 17, line, fg, bg);
    }

    draw_footer(s, "上下键 滚动 · 长按OK 返回");
}

void bili_ui_messages(const bili_msg_list_t *list)
{
    msg_ctx2_t ctx = { .list = list };
    render(paint_messages, &ctx);
}
