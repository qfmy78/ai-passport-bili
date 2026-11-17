#include "bili_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"

static const char *TAG = "bili_http";

// 8 KB 足够一页 20 条推荐（每条约 120 字节）加评论页。超出视为异常直接放弃，
// 宁可显示"失败"也不要让 cJSON 把堆吃穿。
#define RESP_MAX 8192
static char s_resp[RESP_MAX];

typedef struct {
    size_t len;
    bool overflow;
} sink_t;

static esp_err_t on_event(esp_http_client_event_t *ev)
{
    sink_t *sink = (sink_t *)ev->user_data;
    if (ev->event_id != HTTP_EVENT_ON_DATA || ev->data_len <= 0) {
        return ESP_OK;
    }
    if (sink->len + (size_t)ev->data_len + 1 > RESP_MAX) {
        sink->overflow = true;
        return ESP_FAIL;
    }
    memcpy(s_resp + sink->len, ev->data, (size_t)ev->data_len);
    sink->len += (size_t)ev->data_len;
    s_resp[sink->len] = '\0';
    return ESP_OK;
}

static esp_err_t http_get(const char *host, uint16_t port, const char *path,
                          int timeout_ms)
{
    char url[160];
    snprintf(url, sizeof url, "http://%s:%u%s", host, (unsigned)port, path);

    sink_t sink = { .len = 0, .overflow = false };
    s_resp[0] = '\0';

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .event_handler = on_event,
        .user_data = &sink,
        .timeout_ms = timeout_ms,
        .buffer_size = 1024,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (!cli) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = esp_http_client_perform(cli);
    int status = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s -> %s", url, esp_err_to_name(err));
        return err;
    }
    if (sink.overflow) {
        ESP_LOGW(TAG, "%s -> response too large", url);
        return ESP_ERR_INVALID_SIZE;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "%s -> HTTP %d", url, status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

// 按 UTF-8 边界截断，避免把一个汉字劈成半个（屏幕上会变成乱码方块）。
static void copy_utf8(char *dst, size_t cap, const char *src)
{
    if (!src) {
        dst[0] = '\0';
        return;
    }
    size_t n = strlen(src);
    if (n >= cap) {
        n = cap - 1;
        while (n > 0 && ((unsigned char)src[n] & 0xC0) == 0x80) {
            n--;    // 回退到字符起始字节
        }
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static const char *json_str(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}

static uint32_t json_num(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsNumber(v) ? (uint32_t)v->valuedouble : 0;
}

esp_err_t bili_fetch_feed(const char *host, uint16_t port, bili_list_t *out,
                          unsigned page)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    out->count = 0;
    out->selected = 0;
    out->scroll = 0;

    char path[48];
    snprintf(path, sizeof path, "/api/feed?page=%u", page);

    esp_err_t err = http_get(host, port, path, 12000);
    if (err != ESP_OK) {
        return err;
    }

    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= BILI_LIST_MAX) {
                break;
            }
            bili_video_t *v = &out->items[n];
            memset(v, 0, sizeof *v);
            copy_utf8(v->bvid, sizeof v->bvid, json_str(it, "b"));
            copy_utf8(v->title, sizeof v->title, json_str(it, "t"));
            copy_utf8(v->up, sizeof v->up, json_str(it, "u"));
            v->duration = json_num(it, "d");
            v->views = json_num(it, "v");
            if (v->bvid[0] && v->title[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    ESP_LOGI(TAG, "feed page %u -> %u items (%u bytes)", page, n,
             (unsigned)strlen(s_resp));
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_fetch_comments(const char *host, uint16_t port, uint32_t aid,
                              bili_comment_list_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    out->count = 0;
    out->selected = 0;
    out->scroll = 0;

    char path[64];
    snprintf(path, sizeof path, "/api/comments?aid=%lu", (unsigned long)aid);

    esp_err_t err = http_get(host, port, path, 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= BILI_COMMENT_MAX) {
                break;
            }
            bili_comment_t *c = &out->items[n];
            memset(c, 0, sizeof *c);
            copy_utf8(c->user, sizeof c->user, json_str(it, "u"));
            copy_utf8(c->text, sizeof c->text, json_str(it, "m"));
            if (c->text[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_request_play(const char *host, uint16_t port, const char *device,
                            const char *bvid)
{
    char url[192];
    snprintf(url, sizeof url, "http://%s:%u/api/play", host, (unsigned)port);

    char body[128];
    snprintf(body, sizeof body, "{\"device\":\"%s\",\"bvid\":\"%s\"}", device, bvid);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
        .buffer_size = 512,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (!cli) {
        return ESP_ERR_NO_MEM;
    }
    esp_http_client_set_header(cli, "Content-Type", "application/json");
    esp_http_client_set_post_field(cli, body, (int)strlen(body));

    esp_err_t err = esp_http_client_perform(cli);
    int status = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "play %s -> %s", bvid, esp_err_to_name(err));
        return err;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "play %s -> HTTP %d", bvid, status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

esp_err_t bili_fetch_me(const char *host, uint16_t port, bili_me_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof *out);
    esp_err_t err = http_get(host, port, "/api/me", 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    copy_utf8(out->name, sizeof out->name, json_str(root, "name"));
    out->level = json_num(root, "level");
    out->coins = json_num(root, "coins");
    out->following = json_num(root, "following");
    out->follower = json_num(root, "follower");
    out->favs = json_num(root, "favs");
    out->vip = json_num(root, "vip") != 0;
    cJSON_Delete(root);
    return out->name[0] ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_fetch_folders(const char *host, uint16_t port, bili_folder_list_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof *out);
    esp_err_t err = http_get(host, port, "/api/favorites", 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= sizeof out->items / sizeof out->items[0]) {
                break;
            }
            bili_folder_t *f = &out->items[n];
            const cJSON *id = cJSON_GetObjectItemCaseSensitive(it, "id");
            if (!cJSON_IsNumber(id)) {
                continue;
            }
            snprintf(f->id, sizeof f->id, "%u", (unsigned)id->valuedouble);
            copy_utf8(f->name, sizeof f->name, json_str(it, "n"));
            f->count = json_num(it, "c");
            if (f->name[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_fetch_folder_items(const char *host, uint16_t port, const char *media_id,
                                  bili_list_t *out)
{
    if (!out || !media_id || !media_id[0]) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof *out);
    char path[64];
    snprintf(path, sizeof path, "/api/favorites/items?media_id=%s", media_id);
    esp_err_t err = http_get(host, port, path, 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= BILI_LIST_MAX) {
                break;
            }
            bili_video_t *v = &out->items[n];
            copy_utf8(v->bvid, sizeof v->bvid, json_str(it, "b"));
            copy_utf8(v->title, sizeof v->title, json_str(it, "t"));
            copy_utf8(v->up, sizeof v->up, json_str(it, "u"));
            v->duration = json_num(it, "d");
            v->views = json_num(it, "v");
            if (v->bvid[0] && v->title[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_fetch_messages(const char *host, uint16_t port, bili_msg_list_t *out)
{
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof *out);
    esp_err_t err = http_get(host, port, "/api/messages", 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= sizeof out->items / sizeof out->items[0]) {
                break;
            }
            bili_msg_t *m = &out->items[n];
            copy_utf8(m->user, sizeof m->user, json_str(it, "u"));
            copy_utf8(m->text, sizeof m->text, json_str(it, "m"));
            if (m->text[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_fetch_thread(const char *host, uint16_t port, const char *talker,
                            bili_msg_list_t *out)
{
    if (!out || !talker || !talker[0]) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof *out);
    char path[64];
    snprintf(path, sizeof path, "/api/messages/thread?talker=%s", talker);
    esp_err_t err = http_get(host, port, path, 12000);
    if (err != ESP_OK) {
        return err;
    }
    cJSON *root = cJSON_Parse(s_resp);
    if (!root) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "items");
    unsigned n = 0;
    if (cJSON_IsArray(arr)) {
        const cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= sizeof out->items / sizeof out->items[0]) {
                break;
            }
            bili_msg_t *m = &out->items[n];
            copy_utf8(m->user, sizeof m->user, json_str(it, "u"));
            copy_utf8(m->text, sizeof m->text, json_str(it, "m"));
            if (m->text[0]) {
                n++;
            }
        }
    }
    cJSON_Delete(root);
    out->count = n;
    return n ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t bili_request_favorite(const char *host, uint16_t port, const char *device,
                                const char *bvid)
{
    char url[192];
    snprintf(url, sizeof url, "http://%s:%u/api/favorite", host, (unsigned)port);

    char body[128];
    snprintf(body, sizeof body, "{\"device\":\"%s\",\"bvid\":\"%s\"}", device, bvid);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
        .buffer_size = 512,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (!cli) {
        return ESP_ERR_NO_MEM;
    }
    esp_http_client_set_header(cli, "Content-Type", "application/json");
    esp_http_client_set_post_field(cli, body, (int)strlen(body));

    esp_err_t err = esp_http_client_perform(cli);
    int status = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "favorite %s -> %s", bvid, esp_err_to_name(err));
        return err;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "favorite %s -> HTTP %d", bvid, status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "favorite %s ok", bvid);
    return ESP_OK;
}

esp_err_t bili_request_pause(const char *host, uint16_t port, const char *device, bool paused)
{
    char url[192];
    snprintf(url, sizeof url, "http://%s:%u/api/pause", host, (unsigned)port);
    char body[128];
    snprintf(body, sizeof body, "{\"device\":\"%s\",\"paused\":%s}", device,
             paused ? "true" : "false");

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 8000,
        .buffer_size = 512,
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    if (!cli) {
        return ESP_ERR_NO_MEM;
    }
    esp_http_client_set_header(cli, "Content-Type", "application/json");
    esp_http_client_set_post_field(cli, body, (int)strlen(body));
    esp_err_t err = esp_http_client_perform(cli);
    int status = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);
    if (err != ESP_OK || status != 200) {
        ESP_LOGW(TAG, "pause(%d) -> %s/%d", (int)paused, esp_err_to_name(err), status);
        return err != ESP_OK ? err : ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

void bili_device_id(char out[13])
{
    uint8_t mac[6] = { 0 };
    if (esp_efuse_mac_get_default(mac) != ESP_OK) {
        memset(mac, 0, sizeof mac);
    }
    snprintf(out, 13, "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}
