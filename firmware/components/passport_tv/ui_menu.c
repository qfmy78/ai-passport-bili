// strnlen is POSIX, not C. macOS exposes it through <string.h> without being
// asked, glibc only exposes it when a feature-test macro asks for POSIX 2008 --
// so this file built on macOS and failed on the Linux CI runner with
// "implicit declaration of function 'strnlen'". The macro has to come before
// any header is included, which is why it is the first thing in the file.
#define _POSIX_C_SOURCE 200809L

#include "ui_menu.h"
#include "av_settings.h"

#include <stdlib.h>
#include <string.h>

// How long the overlay stays up without a gesture. The banner is deliberately
// shorter than the menu: it carries one fact the viewer already knows.
#define UI_MENU_TIMEOUT_MS 12000
#define UI_MENU_BANNER_MS 5000
#define UI_MENU_STATUS_MS 8000
// The backlight page stays up longer than the status page: it is a control the
// viewer is working, so it should not vanish between presses.
#define UI_MENU_BRIGHTNESS_MS 12000
// How long the volume indicator stays up after the last change. Long enough to
// read while the button is held, short enough not to sit over the picture.
#define UI_MENU_VOLUME_MS 1500

// A channel id must stay printable and short enough for the device table; the
// name is only ever drawn, so it is bounded by the font and the screen.
static bool usable(const ui_menu_entry_t *entry)
{
    size_t id_length = strnlen(entry->id, UI_MENU_ID_MAX);
    if (id_length == 0 || id_length >= UI_MENU_ID_MAX) {
        return false;
    }
    for (size_t i = 0; i < id_length; i++) {
        if (entry->id[i] < '!' || entry->id[i] > '~') {
            return false;
        }
    }
    // A name is required but never a reason to drop the channel: an overlong one
    // is truncated where it is drawn, so only an empty name disqualifies an entry.
    return strnlen(entry->name, UI_MENU_NAME_MAX) > 0;
}

// Keep the highlight inside the window the caller can draw. Without this the
// selection walks off the bottom of the screen and the viewer presses a key
// with nothing visibly happening.
static void follow_selection(ui_menu_t *menu, unsigned rows)
{
    if (rows == 0) {
        return;               // the caller has not said how much fits
    }
    if (menu->selected < menu->scroll) {
        menu->scroll = menu->selected;
    } else if (menu->selected >= menu->scroll + rows) {
        menu->scroll = menu->selected - rows + 1;
    }
}

unsigned ui_menu_visible_rows(int screen_height, int row_height)
{
    if (screen_height <= 0 || row_height <= 0) {
        return 0;
    }
    unsigned rows = (unsigned)(screen_height / row_height);
    if (rows > UI_MENU_MAX) {
        rows = UI_MENU_MAX;
    }
    return rows;
}

void ui_menu_enable_quality(ui_menu_t *menu, unsigned quality) {
    if (!menu) return;
    menu->quality_enabled=true;
    menu->quality=quality<AV_QUALITY_COUNT?quality:AV_QUALITY_DEFAULT;
    menu->quality_selected=menu->quality;
    menu->revision++;
}
unsigned ui_menu_item_count(const ui_menu_t *menu) {
    return menu ? menu->count : 0u;
}

void ui_menu_init(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    memset(menu, 0, sizeof(*menu));
    menu->view = UI_VIEW_VIDEO;
}

bool ui_menu_load(ui_menu_t *menu, const ui_menu_entry_t *entries, unsigned count,
                  const char *current)
{
    if (!menu || !entries || count == 0 || count > UI_MENU_MAX) {
        return false;
    }
    // Copy the entries that can be shown and skip the rest. Refusing the whole
    // list because one entry is malformed meant a single odd channel name could
    // disable the menu completely, with only a log line to show for it.
    unsigned kept = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            kept++;
        }
    }
    if (kept == 0) {
        return false;         // nothing usable: keep whatever was there before
    }
    // Build into a local first: a rejected list must leave the previous one in
    // place, or a malformed CONFIG would empty the menu mid-session.
    // Heap, not stack: at up to UI_MENU_MAX entries this is far past the stack
    // of any caller, and a stack copy here is the same overflow that crashed the
    // device inside config_valid. Freed on every path below.
    ui_menu_entry_t *staged = malloc(sizeof(ui_menu_entry_t) * (size_t)kept);
    if (!staged) {
        return false;
    }
    unsigned next = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            staged[next++] = entries[i];
        }
    }
    count = kept;

    // Keep the highlight on the playing channel so opening the list does not
    // silently move it, and keep an existing highlight when the list is
    // otherwise unchanged and still names it.
    unsigned selected = 0;
    bool found = false;
    if (current) {
        for (unsigned i = 0; i < count; i++) {
            if (strcmp(staged[i].id, current) == 0) {
                selected = i;
                found = true;
                break;
            }
        }
    }
    if (!found && menu->count) {
        const char *was = ui_menu_selected(menu);
        if (was) {
            for (unsigned i = 0; i < count; i++) {
                if (strcmp(staged[i].id, was) == 0) {
                    selected = i;
                    found = true;
                    break;
                }
            }
        }
    }

    memcpy(menu->entries, staged, count * sizeof(staged[0]));
    free(staged);
    menu->count = count;
    menu->revision++;
    menu->selected = selected;
    // Start from the top of the window and pull it to the highlight. The old
    // code only clamped scroll downwards and never advanced it, so once the
    // selection passed the last visible row it drew off-screen.
    menu->scroll = 0;
    follow_selection(menu, menu->visible_rows);
    return true;
}

const char *ui_menu_selected(const ui_menu_t *menu)
{
    if (!menu || menu->count == 0 || menu->selected >= menu->count) {
        return NULL;
    }
    return menu->entries[menu->selected].id;
}

void ui_menu_open_comments(ui_menu_t *menu)
{
    if (!menu) return;
    menu->view = UI_VIEW_COMMENTS;
    menu->comment_selected = 0;
    menu->comment_scroll = 0;
    // 不设超时：看评论是要读的，不能读两秒自己消失。
    menu->timeout_ms = 0;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_set_comments(ui_menu_t *menu, const char *const *lines, unsigned count)
{
    if (!menu) return;
    menu->comments = lines;
    menu->comment_count = count;
    menu->comment_rev++;          // 覆盖层看到它变了才会重画
}

void ui_menu_set_audio(ui_menu_t *menu, bool only)
{
    if (!menu) return;
    if (menu->audio_only != only) {
        menu->audio_only = only;
        menu->revision++;          // the overlay redraws 开/关 from this
    }
}

void ui_menu_set_mute(ui_menu_t *menu, bool muted)
{
    if (!menu) return;
    if (menu->audio_muted != muted) {
        menu->audio_muted = muted;
        menu->revision++;
    }
}

void ui_menu_open_controls(ui_menu_t *menu)
{
    if (!menu) return;
    // 播放设置页始终可用：音频开关和收藏跟有没有频道列表、有没有可调画质无关。
    // 原来这里在 quality_enabled 为假时会退回频道列表，导致单频道场景下点不出设置。
    menu->view=UI_VIEW_CONTROLS;
    menu->controls_selected=0;
    menu->timeout_ms=UI_MENU_TIMEOUT_MS;
    menu->action=UI_ACTION_NONE;
}

void ui_menu_back(ui_menu_t *menu)
{
    if (!menu) return;
    if (menu->quality_enabled && (menu->view==UI_VIEW_MENU || menu->view==UI_VIEW_QUALITY)) {
        menu->controls_selected=menu->view==UI_VIEW_QUALITY?1u:0u;
        menu->view=UI_VIEW_CONTROLS;
        ui_menu_keep_open(menu);
    } else ui_menu_close(menu);
}

void ui_menu_open(ui_menu_t *menu)
{
    if (!menu || ui_menu_item_count(menu) == 0) {
        return;
    }
    menu->view = UI_VIEW_MENU;
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_open_brightness(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_BRIGHTNESS;
    // Same timeout discipline as the status page: the page closes itself, so a
    // viewer who walks away cannot leave the arrows bound to brightness and be
    // unable to change channel.
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_keep_open(ui_menu_t *menu)
{
    if (!menu || menu->view == UI_VIEW_VIDEO) {
        return;   // the video view has no page timeout to extend
    }
    // Whichever page is up gets the same treatment as a scroll: the clock
    // restarts, so a viewer mid-adjustment is never cut off.
    menu->timeout_ms = (menu->view == UI_VIEW_BRIGHTNESS) ? UI_MENU_BRIGHTNESS_MS
                                                          : UI_MENU_TIMEOUT_MS;
}

void ui_menu_open_status(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_STATUS;
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_close(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_VIDEO;
    menu->timeout_ms = 0;
}

void ui_menu_set_rows(ui_menu_t *menu, unsigned rows)
{
    if (!menu) {
        return;
    }
    menu->visible_rows = rows;
    follow_selection(menu, rows);
}

void ui_menu_step(ui_menu_t *menu, int delta)
{
    ui_menu_scroll_by(menu, delta, menu->visible_rows);
}

void ui_menu_scroll_by(ui_menu_t *menu, int delta, unsigned rows)
{
    if (menu && menu->view==UI_VIEW_CONTROLS && delta) {
        int next=((int)menu->controls_selected+delta)%4;
        menu->controls_selected=(unsigned)(next<0?next+4:next);
        ui_menu_keep_open(menu);
        return;
    }
    if (menu && menu->view==UI_VIEW_COMMENTS && delta) {
        // 逐条移动，不让高亮跑出可视窗口，也不越过首尾。
        int last=(int)(menu->comment_count?menu->comment_count-1:0);
        int next=(int)menu->comment_selected+delta;
        if (next<0) next=0;
        if (next>last) next=last;
        menu->comment_selected=(unsigned)next;
        unsigned rows=menu->visible_rows?menu->visible_rows:1;
        if (menu->comment_selected<menu->comment_scroll) {
            menu->comment_scroll=menu->comment_selected;
        } else if (menu->comment_selected>=menu->comment_scroll+rows) {
            menu->comment_scroll=menu->comment_selected-rows+1;
        }
        menu->timeout_ms=0;        // 评论页没有超时，别在这儿重新计时
        return;
    }
    if (menu && menu->view==UI_VIEW_QUALITY && delta) {
        int next=((int)menu->quality_selected+delta)%(int)AV_QUALITY_COUNT;
        menu->quality_selected=(unsigned)(next<0?next+(int)AV_QUALITY_COUNT:next);
        ui_menu_keep_open(menu);
        return;
    }
    if (!menu || menu->view != UI_VIEW_MENU || ui_menu_item_count(menu) == 0 || delta == 0) {
        return;
    }
    if (rows) {
        menu->visible_rows = rows;
    }
    // Wrap around: reaching either end and continuing is more natural than
    // stopping dead, and it is the only way back without holding a key through
    // the whole list.
    int count = (int)ui_menu_item_count(menu);
    int next = ((int)menu->selected + delta) % count;
    if (next < 0) {
        next += count;
    }
    menu->selected = (unsigned)next;
    follow_selection(menu, menu->visible_rows);
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
}

void ui_menu_confirm(ui_menu_t *menu)
{
    if (menu && menu->view==UI_VIEW_CONTROLS) {
        // 四行：0=仅音频 1=静音 2=评论 3=收藏。
        if (menu->controls_selected==0) {
            menu->audio_only=!menu->audio_only;
            menu->action=UI_ACTION_AUDIO_ONLY;
            ui_menu_close(menu);
        } else if (menu->controls_selected==1) {
            menu->audio_muted=!menu->audio_muted;
            menu->action=UI_ACTION_MUTE;
            ui_menu_close(menu);
        } else if (menu->controls_selected==2) {
            // 进评论页并通知宿主去拉内容；拉回来之前页面显示"加载中"。
            ui_menu_open_comments(menu);
            menu->action=UI_ACTION_COMMENTS;
        } else {
            menu->action=UI_ACTION_FAVORITE;
            ui_menu_close(menu);
        }
        return;
    }
    if (menu && menu->view==UI_VIEW_QUALITY) {
        if (menu->quality_selected!=menu->quality) {
            menu->action=UI_ACTION_QUALITY;
        }
        ui_menu_close(menu);
        return;
    }
    if (!menu || menu->view != UI_VIEW_MENU || menu->count == 0) {
        return;
    }
    menu->action = UI_ACTION_SWITCH;
}

bool ui_menu_tick(ui_menu_t *menu, int64_t elapsed_ms)
{
    if (!menu || elapsed_ms <= 0) {
        return false;
    }
    bool changed = false;
    if (menu->banner_ms > 0) {
        menu->banner_ms -= elapsed_ms;
        if (menu->banner_ms <= 0) {
            menu->banner_ms = 0;
            changed = true;
        }
    }
    if (menu->volume_ms > 0) {
        menu->volume_ms -= elapsed_ms;
        if (menu->volume_ms <= 0) {
            menu->volume_ms = 0;
            changed = true;
        }
    }
    if (menu->view != UI_VIEW_VIDEO && menu->timeout_ms > 0) {
        menu->timeout_ms -= elapsed_ms;
        if (menu->timeout_ms <= 0) {
            menu->timeout_ms = 0;
            ui_menu_close(menu);
            changed = true;
        }
    }
    return changed;
}

ui_action_t ui_menu_take_action(ui_menu_t *menu, char *out, size_t out_size)
{
    if (!menu) {
        return UI_ACTION_NONE;
    }
    ui_action_t action = menu->action;
    menu->action = UI_ACTION_NONE;
    if (action==UI_ACTION_QUALITY) menu->quality=menu->quality_selected;
    if (out && out_size) {
        out[0] = '\0';
    }
    if (action == UI_ACTION_SWITCH && out && out_size) {
        const char *id = ui_menu_selected(menu);
        if (!id || strnlen(id, UI_MENU_ID_MAX) >= out_size) {
            return UI_ACTION_NONE;  // cannot name it, so do not pretend to
        }
        memcpy(out, id, strlen(id) + 1);
        ui_menu_close(menu);  // the list has done its job
    }
    return action;
}

void ui_menu_show_volume(ui_menu_t *menu, unsigned level)
{
    if (!menu) {
        return;
    }
    menu->volume = level > 100u ? 100u : level;
    menu->volume_ms = UI_MENU_VOLUME_MS;
}

bool ui_menu_volume_visible(const ui_menu_t *menu)
{
    return menu && menu->volume_ms > 0;
}

void ui_menu_banner(ui_menu_t *menu, const char *name, const char *previous)
{
    if (!menu || !name) {
        return;
    }
    // Nothing changed, so a banner would only cover the picture to repeat what
    // the viewer already had: a single-channel list, or a reconnect that landed
    // on the same channel.
    if (previous && strcmp(name, previous) == 0) {
        return;
    }
    strncpy(menu->banner, name, sizeof(menu->banner) - 1);
    menu->banner[sizeof(menu->banner) - 1] = '\0';
    menu->banner_ms = UI_MENU_BANNER_MS;
}
