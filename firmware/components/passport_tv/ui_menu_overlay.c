#include "ui_menu_overlay.h"
#include "av_settings.h"
#include <stdio.h>

// A compact bottom card within the 320x180 video area (y=30..210).
enum { X=0, W=320, BOTTOM=210, HEADER=26, ROW=24, FOOTER=26,
       LIST_TOP=BOTTOM-HEADER-UI_MENU_OVERLAY_ROWS*ROW-FOOTER,
       QUALITY_TOP=BOTTOM-108,
       CONTROLS_TOP=BOTTOM-HEADER-4*ROW-FOOTER,
       COMMENTS_TOP=BOTTOM-HEADER-4*ROW-FOOTER };
#define COMMENT_ROWS 4u
#define PANEL 0x1082u
#define FOCUS 0x2249u
#define ACCENT 0x5DF6u

static bool stale(const ui_menu_overlay_t *c,const ui_menu_t *m) {
    // 每个视图的"选中项/滚动位/总条数"来路不同，必须按视图取值。
    // 之前评论页复用了频道列表的 selected/scroll/count，于是滚动改了
    // comment_scroll 而这里看不出来，屏幕一次都不重画（按下去像没反应）。
    bool cmt = m->view==UI_VIEW_COMMENTS;
    unsigned selected = cmt?m->comment_selected:
                        m->view==UI_VIEW_CONTROLS?m->controls_selected:m->selected;
    unsigned scroll   = cmt?m->comment_scroll:m->scroll;
    unsigned count    = cmt?m->comment_count:m->count;
    return !c->valid || c->view!=m->view || c->selected!=selected ||
        c->scroll!=scroll || c->quality!=m->quality ||
        c->quality_selected!=m->quality_selected || c->revision!=m->revision ||
        c->quality_enabled!=m->quality_enabled || c->count!=count ||
        c->audio_only!=m->audio_only || c->audio_muted!=m->audio_muted ||
        c->comment_rev!=m->comment_rev;
}
static void prepare(ui_menu_overlay_t *c,const ui_menu_t *m) {
    c->valid=true;c->view=m->view;
    c->audio_only=m->audio_only;
    c->audio_muted=m->audio_muted;
    c->comment_rev=m->comment_rev;
    if (m->view==UI_VIEW_COMMENTS) {
        c->selected=m->comment_selected;c->scroll=m->comment_scroll;
    } else {
        c->selected=m->view==UI_VIEW_CONTROLS?m->controls_selected:m->selected;c->scroll=m->scroll;
    }
    c->quality=m->quality;c->quality_selected=m->quality_selected;
    c->revision=m->revision;c->quality_enabled=m->quality_enabled;c->count=m->count;
    c->builds++;
    char label[64];
    if (m->view==UI_VIEW_QUALITY) {
        ui_text_prepare(&c->text[0],"清晰度",240);
        static const char *rates[]={"8 fps","15 fps","30 fps"};
        for (unsigned i=0;i<AV_QUALITY_COUNT;i++) {
            ui_text_prepare(&c->text[1+i*2],av_quality_name(i),76);
            ui_text_prepare(&c->text[2+i*2],rates[i],76);
        }
    } else if (m->view==UI_VIEW_CONTROLS) {
        // 四行播放设置：仅音频、静音、评论、收藏。原来是「频道 / 清晰度」，
        // 单频道场景下那两项没意义，这几个才真正影响观感。
        // 槽位：标签 2..5，右值 6..9，页脚 10/11（别和标签撞）。
        ui_text_prepare(&c->text[0],"播放设置",100);
        ui_text_prepare(&c->text[2],"仅音频",160);
        ui_text_prepare(&c->text[3],"静音",160);
        ui_text_prepare(&c->text[4],"评论",160);
        ui_text_prepare(&c->text[5],"收藏",160);
        ui_text_prepare(&c->text[6],m->audio_only?"开":"关",64);
        ui_text_prepare(&c->text[7],m->audio_muted?"开":"关",64);
        ui_text_prepare(&c->text[8],"查看",64);
        ui_text_prepare(&c->text[9],"当前视频",96);
    } else if (m->view==UI_VIEW_COMMENTS) {
        ui_text_prepare(&c->text[0],"评论",100);
        for (unsigned i=0;i<COMMENT_ROWS;i++) {
            unsigned index=m->comment_scroll+i;
            label[0]=0;
            if (m->comments && index<m->comment_count && m->comments[index]) {
                snprintf(label,sizeof(label),"%u. %s",index+1,m->comments[index]);
            } else if (m->comment_count==0) {
                snprintf(label,sizeof(label),"加载中…");
            }
            ui_text_prepare(&c->text[2+i],label,300);
        }
    } else {
        ui_text_prepare(&c->text[0],"频道",100);
        snprintf(label,sizeof(label),"%s",av_quality_name(m->quality));
        ui_text_prepare(&c->text[1],m->quality_enabled?label:"直播",100);
        for (unsigned i=0;i<UI_MENU_OVERLAY_ROWS;i++) {
            unsigned index=m->scroll+i;
            label[0]=0;
            if (index<ui_menu_item_count(m)) {
                snprintf(label,sizeof(label),"%02u  %s",index+1,
                    m->entries[index].name[0]?m->entries[index].name:m->entries[index].id);
            }
            ui_text_prepare(&c->text[2+i],label,252);
        }
        ui_text_prepare(&c->text[5],av_quality_name(m->quality),64);
    }
    ui_text_prepare(&c->text[10],"上下选择  OK确认",180);
    ui_text_prepare(&c->text[11],"长按返回",76);
}
static void card(ui_surface_t s,int x,int y,int w,int h,uint16_t color) {
    // Four-pixel stepped corners, with every fill clipped to this stripe.
    ui_text_fill(s,x+4,y,w-8,1,color);
    ui_text_fill(s,x+2,y+1,w-4,2,color);
    ui_text_fill(s,x+1,y+3,w-2,1,color);
    ui_text_fill(s,x,y+4,w,h-8,color);
    ui_text_fill(s,x+1,y+h-4,w-2,1,color);
    ui_text_fill(s,x+2,y+h-3,w-4,2,color);
    ui_text_fill(s,x+4,y+h-1,w-8,1,color);
}
void ui_menu_overlay_begin(ui_menu_overlay_t *c,const ui_menu_t *m) {
    if (m->view!=UI_VIEW_MENU && m->view!=UI_VIEW_QUALITY && m->view!=UI_VIEW_CONTROLS &&
        m->view!=UI_VIEW_COMMENTS) {
        c->view=m->view;c->valid=false;return;
    }
    if (stale(c,m)) prepare(c,m);
}
bool ui_menu_overlay_covers(const ui_menu_overlay_t *c,unsigned y,unsigned rows) {
    if (!c->valid || !rows || (c->view!=UI_VIEW_MENU && c->view!=UI_VIEW_QUALITY &&
        c->view!=UI_VIEW_CONTROLS && c->view!=UI_VIEW_COMMENTS)) return false;
    unsigned top=c->view==UI_VIEW_MENU?LIST_TOP:
        c->view==UI_VIEW_CONTROLS?CONTROLS_TOP:
        c->view==UI_VIEW_COMMENTS?COMMENTS_TOP:QUALITY_TOP;
    // Only the full-width rectangular interior is opaque. Corner stripes and
    // all visible video still take the normal checked decoder path.
    return y>=top+4u && y<BOTTOM-4u && rows<=BOTTOM-4u-y;
}
void ui_menu_overlay_paint(ui_menu_overlay_t *c,const ui_menu_t *m,ui_surface_t s) {
    ui_menu_overlay_begin(c,m);
    ui_menu_overlay_draw(c,s);
}
void ui_menu_overlay_draw(const ui_menu_overlay_t *c,ui_surface_t s) {
    if (!c->valid) return;
    ui_view_t view=c->view;
    if (view!=UI_VIEW_MENU && view!=UI_VIEW_QUALITY && view!=UI_VIEW_CONTROLS &&
        view!=UI_VIEW_COMMENTS) return;
    int top=view==UI_VIEW_MENU?LIST_TOP:
        view==UI_VIEW_CONTROLS?CONTROLS_TOP:
        view==UI_VIEW_COMMENTS?COMMENTS_TOP:QUALITY_TOP;
    if (s.origin_y+s.rows<=top || s.origin_y>=BOTTOM) return;
    card(s,X,top,W,BOTTOM-top,PANEL);
    ui_text_paint(s,X+12,top+6,&c->text[0],UI_COLOR_TEXT);
    if (view==UI_VIEW_MENU) {
        ui_text_paint(s,X+W-12-c->text[1].width,top+6,&c->text[1],ACCENT);
        for (unsigned i=0;i<UI_MENU_OVERLAY_ROWS;i++) {
            unsigned index=c->scroll+i;
            if (index>=c->count) break;
            int y=top+HEADER+(int)i*ROW;
            bool chosen=index==c->selected;
            if (chosen) {
                card(s,X+6,y,W-12,ROW,FOCUS);
                ui_text_fill(s,X+10,y+7,2,10,ACCENT);
            }
            ui_text_paint(s,X+18,y+4,&c->text[2+i],chosen?UI_COLOR_TEXT:UI_COLOR_DIM);
        }
    } else if (view==UI_VIEW_CONTROLS) {
        for (unsigned i=0;i<4;i++) {
            int y=top+HEADER+(int)i*ROW;
            bool chosen=i==c->selected;
            if (chosen) {
                card(s,X+6,y,W-12,ROW,FOCUS);
                ui_text_fill(s,X+10,y+7,2,10,ACCENT);
            }
            ui_text_paint(s,X+18,y+4,&c->text[2+i],chosen?UI_COLOR_TEXT:UI_COLOR_DIM);
            ui_text_paint(s,X+W-18-c->text[6+i].width,y+4,&c->text[6+i],ACCENT);
        }
    } else if (view==UI_VIEW_COMMENTS) {
        for (unsigned i=0;i<COMMENT_ROWS;i++) {
            unsigned index=c->scroll+i;
            if (index>=c->count) break;
            int y=top+HEADER+(int)i*ROW;
            bool chosen=index==c->selected;
            if (chosen) {
                // 没有高亮的话，前几次按上下键屏幕上毫无变化，
                // 用的人会以为滚不动。列表页一直有，评论页也得有。
                card(s,X+6,y,W-12,ROW,FOCUS);
                ui_text_fill(s,X+10,y+7,2,10,ACCENT);
            }
            ui_text_paint(s,X+12,y+4,&c->text[2+i],UI_COLOR_TEXT);
        }
    } else {
        for (unsigned i=0;i<AV_QUALITY_COUNT;i++) {
            int x=X+(W-292)/2+(int)i*100,y=top+HEADER+2;
            bool chosen=i==c->quality_selected;
            card(s,x,y,92,48,chosen?FOCUS:UI_COLOR_BACKDROP);
            ui_text_paint(s,x+(92-c->text[1+i*2].width)/2,y+5,&c->text[1+i*2],UI_COLOR_TEXT);
            ui_text_paint(s,x+(92-c->text[2+i*2].width)/2,y+25,&c->text[2+i*2],chosen?ACCENT:UI_COLOR_DIM);
            if (i==c->quality) ui_text_fill(s,x+34,y+44,24,2,ACCENT);
        }
    }
    ui_text_fill(s,X+12,BOTTOM-FOOTER,W-24,1,UI_COLOR_RULE);
    ui_text_paint(s,X+12,BOTTOM-FOOTER+6,&c->text[10],UI_COLOR_DIM);
    ui_text_paint(s,X+W-12-c->text[11].width,BOTTOM-FOOTER+6,&c->text[11],UI_COLOR_DIM);
}
