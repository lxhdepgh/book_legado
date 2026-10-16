/*
Package main provides the unified FFI interface between Dart/Flutter and the Go backend.

Exported C functions (only 3):
  - Initialize()            — bootstrap ServiceHub (must call before anything)
  - Call(action, paramsJSON) — universal router; returns JSON ResponseEnvelope
  - Cleanup()               — stop all services

All payloads flow through UTF-8 JSON strings via c-shared. Zero per-action code.
*/
package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
	"unsafe"

	"github.com/legodo-booksource/legodo_go/core"
	internalmodels "github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/internal/services"
)

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

var hub atomic.Pointer[services.ServiceHub]

// evtRing records every published event for Dart-side polling (engine_poll).
// FFI 没有推送出口：Dart 以游标来问，这里只负责按发布序留一份最近的。
var evtRing atomic.Pointer[core.EventRing]

func getHub() *services.ServiceHub { return hub.Load() }
func toCString(s string) *C.char   { return C.CString(s) }
func toJSON(v any) string          { b, _ := json.Marshal(v); return string(b) }

// FreeString releases a C string returned by Call/Cleanup/etc.
//
//export FreeString
func FreeString(s *C.char) { C.free(unsafe.Pointer(s)) }

// main is required as a stub for `go build -buildmode=c-shared`.
func main() {}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// Initialize boots the ServiceHub. Idempotent. Must be called before Call().
//
//export Initialize
func Initialize() {
	if h := getHub(); h != nil {
		return
	}
	hub.Store(services.NewServiceHub())
	if err := getHub().RegisterInfrastructure(); err != nil {
		log.Printf("[Initialize] infrastructure registration failed: %v", err)
	}
	if err := getHub().RegisterBusinessServices(); err != nil {
		log.Printf("[Initialize] business services registration failed: %v", err)
	}
	// 先挂事件环再启服务：服务启动期发的事件（ServiceStateChangedEvent）
	// 也要进环，否则界面在初始化后拿到的第一帧是残缺的。
	ring := core.NewEventRing(4096)
	evtRing.Store(ring)
	if bus := getHub().EventBus(); bus != nil {
		bus.SubscribeAll(func(event core.AppEvent) {
			ring.Record(event)
		})
	} else {
		log.Println("[Initialize] event bus missing: engine_poll will stay empty")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := getHub().StartAll(ctx); err != nil {
		log.Printf("[Initialize] service startup failed: %v", err)
	}
	log.Println("[Initialize] all services started")
}

// Cleanup stops all services. After this, call Initialize() again to reuse.
//
//export Cleanup
func Cleanup() {
	if h := getHub(); h != nil {
		h.StopAll()
	}
	hub.Store(nil)
}

// ---------------------------------------------------------------------------
// Universal data entry — ONE function handles ALL RPC actions
// ---------------------------------------------------------------------------

// ResultEnvelope is the standard reply shape.
type ResultEnvelope struct {
	Ok      bool        `json:"ok"`
	Data    interface{} `json:"data,omitempty"`
	Error   string      `json:"error,omitempty"`
	ID      string      `json:"id,omitempty"`
	Version int         `json:"v,omitempty"`
}

func okResult(data interface{}, id string) ResultEnvelope {
	return ResultEnvelope{Ok: true, Data: data, ID: id, Version: 1}
}
func failResult(msg, id string) ResultEnvelope {
	return ResultEnvelope{Ok: false, Error: msg, ID: id, Version: 1}
}

// Call is the SINGLE data-entry-point FFI function.
//
//export Call
func Call(actionRaw *C.char, paramsRaw *C.char) *C.char {
	actionStr := C.GoString(actionRaw)
	paramsStr := C.GoString(paramsRaw)

	h := getHub()
	if h == nil {
		return toCString(toJSON(failResult("hub not initialized", "")))
	}

	var p map[string]interface{}
	if len(paramsStr) > 0 {
		json.Unmarshal([]byte(paramsStr), &p)
	}

	resp := route(h, actionStr, p)
	return toCString(toJSON(resp))
}

// StatusReport reads-only view used by flutter to verify readiness.
//
//export StatusReport
func StatusReport() *C.char {
	h := getHub()
	if h == nil {
		return toCString(`[]`)
	}
	statuses := h.StatusReport()
	result := make([]map[string]string, len(statuses))
	for i, s := range statuses {
		result[i] = map[string]string{"id": s.ServiceID, "state": string(s.State)}
	}
	return toCString(toJSON(result))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func gs(p map[string]interface{}, k string) string  { v, _ := p[k].(string); return v }
func gb(p map[string]interface{}, k string) bool    { b, _ := p[k].(bool); return b }
func gi(p map[string]interface{}, k string) int     { n, _ := p[k].(float64); return int(n) }
func gl(p map[string]interface{}, k string) float64 { v, _ := p[k].(float64); return v }

// gbDefault 缺省按 def：shelve/resume/save_progress 的缺省都是 true，
// 用 gb 会把"没传"读成 false（对协议默认值来说是个静默翻转）。
func gbDefault(p map[string]interface{}, k string, def bool) bool {
	if v, ok := p[k].(bool); ok {
		return v
	}
	return def
}

// gurls 取一串 url（批量启停/删除的入参数组）：非字符串元素丢掉，
// 不能让它们数进"改动条数"里——那会把没改的也算成改了。
func gurls(p map[string]interface{}, k string) []string {
	raw, _ := p[k].([]interface{})
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// gid reads an integer id from params. Dart 侧的 rule_id/font_id 是
// .toString() 发来的字符串（json.Number 是将来换 UseNumber 的兜底），
// 数字键则解成 float64——三种都要收，否则删除/更新会永远匹配不到行。
func gid(p map[string]interface{}, k string) (int64, bool) {
	switch v := p[k].(type) {
	case float64:
		return int64(v), true
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
		if err != nil {
			return 0, false
		}
		return n, true
	case json.Number:
		n, err := v.Int64()
		if err != nil {
			return 0, false
		}
		return n, true
	}
	return 0, false
}

func stringSlice(raw interface{}) []string {
	arr, ok := raw.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, item := range arr {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Convention-based router: <service>_<action> → Service.Method()
// ---------------------------------------------------------------------------

// 这一张表钉的是「这条能力在本引擎里就是没有」这件事本身，住在服务端而不是界面：
// 界面拿到的是错误，不需要再自己猜哪一条算支持。
//
// 为什么不放行它们落进 `default`（那里回英文的 `unimplemented: <action>`）：
// `default` 是给**没人登记过**的 action 用的，而下面这些是界面真的会发的——
// 发的人要知道缺的是哪一个后端，以及"这一发之后磁盘/远端/库里到底多了什么"。
//
// 每一条都以"不会发生什么"收尾，因为这张表存在的理由就是它替掉的那些 `okResult`：
// 导出回 `{path:"",success:true}`、WebDAV 备份回 `true`、朗读回 `true`、
// 书架导入回 `{imported:0}`、`source.audit` 起一个协程给每条启用的书源写
// `respond_time≈0`（它根本没联网）再回 `{started:true}`。
// 那些回包在界面上长成"做完了"，而一个字节都没写、一个包都没发、
// 库里那一列还被覆成了假的。护栏见 `unimplemented_test.go`。
var unimplementedActions = map[string]string{
	"browser_open_page": "打开网页未实现：Go 引擎没有 CDP/无头浏览器后端，" +
		"网页由 Flutter 侧的 webview_flutter 直接渲染，不经这一发；这一发不会返回任何页面内容。",
	"browser_navigate": "网页翻页未实现：Go 引擎没有可导航的浏览器会话；" +
		"这一发不会让任何页面动起来。",
	"browser_close": "关闭网页未实现：Go 引擎没有浏览器会话可关；这一发不会释放任何东西。",

	"webbridge_start": "网络桥未实现：Go 引擎里没有内置的 HTTP/WebSocket 服务，" +
		"这一发不会监听任何端口。",
	"webbridge_stop": "网络桥未实现：这一发没有可停的服务，也不会改动任何监听。",
	"webbridge_status": "网络桥未实现：没有可报状态的服务，" +
		"这一发不会回「运行中」，也不会回一份空状态装成已停止。",

	"export_epub": "EPUB 导出未实现：Go 引擎里的导出器没有取正文的调用点（章节内容不会喂进去），" +
		"这一发不会写出任何 .epub 文件。",
	"export_txt": "TXT 导出未实现：同上，缺的是把正文交给导出器的那一步；" +
		"这一发不会写出任何 .txt 文件。",

	"webdav_backup": "WebDAV 备份未实现：Go 引擎的 WebDAV 客户端上传时不带备份包，" +
		"这一发不会在远端生成任何文件。",
	"webdav_restore": "WebDAV 恢复未实现：没有可用的下载路径，这一发不会改动本地数据目录。",
	"webdav_list_backups": "WebDAV 备份列表未实现：这一发不会去问远端，" +
		"也不会回一份空列表装成「远端没有备份」。",

	"read_aloud_start": "朗读未实现：Go 引擎没有 TTS 后端；朗读由 Flutter 侧的 flutter_tts " +
		"直接实现，不经这一发。",
	"read_aloud_pause": "朗读暂停未实现：这一发没有可控的发声状态。",
	"read_aloud_stop":  "朗读停止未实现：这一发不会让任何正在读的声音停下来。",
	"read_aloud_progress": "朗读进度未实现：没有发声状态就没有进度可读，" +
		"这一发不会回 0 装成「刚开头」。",

	"bookshelf_import": "书架导入未实现：本引擎没有定义可导入的书架文件格式，" +
		"这一发不会往书架里写任何一行。",

	"source_audit": "书源兼容性审计未实现：Go 引擎没有 Python 侧 CompatAuditService 的对应实现；" +
		"这一发不会联网，也不会改动任何一条书源。",
}

// unimplementedText 按 action 取那句"未实现"。键把点号拼法折成下划线，
// 所以 `webbridge.start` 与 `webbridge_start` 拿到的永远是同一句话——
// 两条拼法各写一份，改天就会只改其中一条。
func unimplementedText(action string) (string, bool) {
	key := strings.ToLower(strings.ReplaceAll(action, ".", "_"))
	msg, ok := unimplementedActions[key]
	return msg, ok
}

// unimplementedResult 回的是**错误**，不是一个形状对的空成功。
// `_callGo` 那一侧见 `ok != true` 就抛 `EngineFailure`，界面拿到的是这句话本身；
// 用 `failResult` 而不是新造一种信封，是为了让"没实现"和"实现了但失败"走同一条出声路径。
//
// 表里没有这一格时不回空串：空错误的包在界面上是一句"失败："，
// 那比英文的 `unimplemented:` 更难查。
func unimplementedResult(action string) ResultEnvelope {
	if msg, ok := unimplementedText(action); ok {
		return failResult(msg, "")
	}
	return failResult("unimplemented: "+action, "")
}

func route(h *services.ServiceHub, action string, p map[string]interface{}) ResultEnvelope {
	switch action {

	// --- engine ---
	case "engine_status", "engine.hello":
		statuses := h.StatusReport()
		list := make([]interface{}, len(statuses))
		for i, s := range statuses {
			list[i] = map[string]interface{}{"id": s.ServiceID, "state": s.State}
		}
		return okResult(list, "")
	case "engine.status":
		statuses := h.StatusReport()
		total := len(statuses)
		running := 0
		for _, s := range statuses {
			if s.State == "RUNNING" {
				running++
			}
		}
		return okResult(map[string]interface{}{"total": total, "running": running}, "")
	case "engine_shutdown", "engine.shutdown":
		Cleanup()
		return okResult(true, "")
	case "engine_poll", "engine.poll":
		return doEnginePoll(p)

	// --- source ---
	case "source_list", "source.list":
		return doSourceList(h)
	case "source_get", "source.get":
		return doSourceGet(h, p)
	case "source_save", "source.save":
		return doSourceSave(h, p)
	case "source_delete", "source.delete":
		return doSourceDelete(h, p)
	case "source_enable", "source.enable":
		return doSourceEnable(h, p)
	case "source.group", "source_group":
		return doSourceGroup(h, p)
	case "source.batch_enable", "source_batch_enable":
		return doSourceBatchEnable(h, p)
	case "source.batch_delete", "source_batch_delete":
		return doSourceBatchDelete(h, p)
	case "source.export", "source_export":
		return doSourceExport(h)
	case "source.audit", "source_audit":
		return unimplementedResult(action)

	// --- bookshelf ---
	case "bookshelf_list", "bookshelf.list":
		return doBookshelfList(h, p)
	case "bookshelf_add", "bookshelf.add":
		return doBookshelfAdd(h, p)
	case "bookshelf_update", "bookshelf.update":
		return doBookshelfUpdate(h, p)
	case "bookshelf_delete", "bookshelf.delete":
		return doBookshelfDelete(h, p)
	case "bookshelf_import":
		return unimplementedResult(action)

	// --- check ---
	case "check_start", "check.start":
		return doCheckStart(h, p)
	case "check_snapshot", "check.snapshot":
		return doCheckSnapshot(h)
	case "check_cancel", "check.cancel":
		return doCheckCancel(h)
	case "check_source", "check.source":
		return doCheckSource(h, p)

	// --- search ---
	case "search_start", "search.start":
		return doSearchStart(h, p)
	case "search_stop", "search.stop":
		if svc := h.ReadingService(); svc != nil {
			svc.StopSearch()
		}
		return okResult(true, "")
	case "search_scopes", "search.scopes":
		return doSearchScopes(h)

	// --- switch / sweep ---
	case "switch_seeds", "switch.seeds":
		return doSwitchSeeds(h, p)
	case "switch_scan", "switch.scan":
		return doSwitchScan(h, p)
	case "switch_snapshot", "switch.snapshot":
		return doSwitchSnapshot(h)
	case "switch_cancel", "switch.cancel":
		return doSwitchCancel(h)
	case "switch_apply", "switch.apply":
		return doSwitchApply(h, p)

	// --- reader ---
	case "reader_open", "reader.open":
		return doReaderOpen(h, p)
	case "reader_close", "reader.close":
		return doReaderClose(h, p)
	case "reader_toc", "reader.toc":
		return doReaderToc(h, p)
	case "reader_content", "reader.content":
		return doReaderContent(h, p)
	case "reader_position_get", "reader.position_get":
		return doReaderPosGet(h, p)
	case "reader_position_save", "reader.position_save":
		return doReaderPosSave(h, p)
	case "reader_staged", "reader.staged":
		return doReaderStaged(h)
	case "reader_staged_promote", "reader.staged_promote":
		return doReaderStagedPromote(h)
	case "reader_staged_discard", "reader.staged_discard":
		return doReaderStagedDiscard(h)
	case "reader_bookmarks", "reader.bookmarks":
		return doReaderBookmarks(h, p)
	case "reader_bookmark_add", "reader.bookmark_add":
		return doReaderBookmarkAdd(h, p)
	case "reader_bookmark_remove", "reader.bookmark_remove":
		return doReaderBookmarkRemove(h, p)
	case "reader_search_content", "reader.search_content":
		return doReaderSearchContent(h, p)

	// --- rss ---
	case "rss_sources", "rss.sources":
		return doRssSources(h)
	case "rss_articles", "rss.articles":
		return doRssArticles(h, p)
	case "rss_article", "rss.article":
		return doRssArticle(h, p)
	case "rss_add_source", "rss.add_source":
		return doRssAddSource(h, p)
	case "rss_remove_source", "rss.remove_source":
		return doRssRemoveSource(h, p)
	case "rss_refresh", "rss.refresh":
		// call 语义（对齐 Python：这一发真的发网络请求，60s 预算）；回本次**新增**数。
		// 抓取失败是**错误信封**不是静默 0——「0 新增」与「没抓着」是两件事。
		svc := h.RSSService()
		if svc == nil {
			return failResult("rss service unavailable", "")
		}
		count, err := svc.Refresh(context.Background(), gs(p, "source_id"))
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(count, "")
	case "rss_refresh_all", "rss.refresh_all":
		// job 语义：受理回包只有 {started, action}，真交付全在事件流上
		// （RssRefreshProgressEvent 逐帧 + RssRefreshedEvent 每源一条）。
		refreshID := int64(gi(p, "refresh_id"))
		go func() {
			if svc := h.RSSService(); svc != nil {
				svc.RefreshAll(context.Background(), refreshID)
			}
		}()
		return okResult(map[string]interface{}{"started": true, "action": "rss.refresh_all"}, "")
	case "rss_mark_read", "rss.mark_read":
		if svc := h.RSSService(); svc != nil {
			changed, err := svc.MarkRead(int64(gi(p, "article_id")))
			if err != nil {
				return failResult(err.Error(), "")
			}
			return okResult(changed, "")
		}
		return failResult("rss service unavailable", "")
	case "rss_star", "rss.star":
		if svc := h.RSSService(); svc != nil {
			changed, err := svc.Star(int64(gi(p, "article_id")), gb(p, "starred"))
			if err != nil {
				return failResult(err.Error(), "")
			}
			return okResult(changed, "")
		}
		return failResult("rss service unavailable", "")

	// --- browser / webbridge（两条都没有后端：见 unimplementedActions）---
	case "browser_open_page", "browser.open_page",
		"browser_navigate", "browser.navigate",
		"browser_close", "browser.close",
		"webbridge_start", "webbridge.start",
		"webbridge_stop", "webbridge.stop",
		"webbridge_status", "webbridge.status":
		return unimplementedResult(action)
	case "engine_web_content_ready", "engine.webContentReady":
		return doWebContentReady(h, p)

	// --- qr ---
	case "qr_generate", "qr.generate":
		return doQRGenerate(h, p)
	case "qr_decode", "qr.decode":
		return doQRDecode(h, p)

	// --- update ---
	case "update_check", "update.check":
		return doUpdateCheck(h)
	case "update_ignore", "update.ignore":
		return doUpdateIgnore(h, p)

	// --- settings ---
	case "settings_read", "settings.read":
		return doSettingsRead(h, p)
	case "settings_write", "settings.write":
		return doSettingsWrite(h, p)

	// --- theme ---
	case "theme_preferences", "theme.preferences":
		return doThemePreferences(h)
	case "theme_save", "theme.save":
		return doThemeSave(h, p)

	// --- font ---
	case "font_scan_system", "font.scan_system":
		return doFontScanSystem(h)
	case "font_add_user", "font.add_user":
		return doFontAddUser(h, p)
	case "font_remove", "font.remove":
		return doFontRemove(h, p)
	case "font_select", "font.select":
		return doFontSelect(h, p)

	// --- replace ---
	case "replace_list", "replace.list":
		return doReplaceList(h)
	case "replace_add", "replace.add":
		return doReplaceAdd(h, p)
	case "replace_update", "replace.update":
		return doReplaceUpdate(h, p)
	case "replace_delete", "replace.delete":
		return doReplaceDelete(h, p)
	case "replace_toggle_global", "replace.toggle_global":
		return doReplaceToggleGlobal(h, p)

	// --- export / webdav（服务在库里，但没有能把活干完的调用点）---
	case "export_epub", "export.epub",
		"export_txt", "export.txt",
		"webdav_backup", "webdav.backup",
		"webdav_restore", "webdav.restore",
		"webdav_list_backups", "webdav.list_backups":
		return unimplementedResult(action)

	// --- browse ---
	case "browse_categories", "browse.categories":
		return doBrowseCategories(h, p)
	case "browse_books", "browse.books":
		return doBrowseBooks(h, p)

	// --- import ---
	case "import_text", "import.text":
		return doImportText(h, p)
	case "import_url", "import.url":
		return doImportUrl(h, p)

	// --- read_aloud（发声在 Flutter 侧，Go 这一路没有后端）---
	case "read_aloud_start", "read_aloud.start",
		"read_aloud_pause", "read_aloud.pause",
		"read_aloud_stop", "read_aloud.stop",
		"read_aloud_progress", "read_aloud.progress":
		return unimplementedResult(action)

	default:
		return failResult("unimplemented: "+action, "")
	}
}

// ---------------------------------------------------------------------------
// Source handlers
// ---------------------------------------------------------------------------

func doSourceList(h *services.ServiceHub) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		srcs := svc.List()
		data := make([]interface{}, len(srcs))
		for i, src := range srcs {
			data[i] = srcToMap(src)
		}
		return okResult(data, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceGet(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		url := gs(p, "url")
		if url == "" {
			return failResult("missing url", "")
		}
		if src, ok := svc.Get(url); ok {
			return okResult(srcToMap(*src), "")
		}
		return failResult("source not found", "")
	}
	return failResult("source service unavailable", "")
}

func doSourceSave(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		raw, _ := json.Marshal(p["source"])
		var src internalmodels.BookSource
		if err := json.Unmarshal(raw, &src); err != nil {
			return failResult("invalid source JSON: "+err.Error(), "")
		}
		if err := svc.Save(&src); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(map[string]interface{}{"saved": true, "url": src.BookSourceURL}, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceDelete(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		url := gs(p, "url")
		if url == "" {
			return failResult("missing url", "")
		}
		if err := svc.Delete(url); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(true, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceEnable(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		url := gs(p, "url")
		en := gb(p, "enabled")
		if url == "" {
			return failResult("missing url", "")
		}
		if err := svc.Enable(url, en); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(en, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceGroup(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		url := gs(p, "url")
		if url == "" {
			return failResult("missing url", "")
		}
		if err := svc.SetGroup(url, gs(p, "group")); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(true, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceBatchEnable(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		updated := svc.EnableBatch(gurls(p, "urls"), gb(p, "enabled"))
		return okResult(map[string]interface{}{"updated": updated}, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceBatchDelete(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		deleted := svc.DeleteBatch(gurls(p, "urls"))
		return okResult(map[string]interface{}{"deleted": deleted}, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceExport(h *services.ServiceHub) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		data, err := svc.ExportAll()
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(data, "")
	}
	return failResult("source service unavailable", "")
}

// ---------------------------------------------------------------------------
// Bookshelf handlers
// ---------------------------------------------------------------------------

func doBookshelfList(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.BookshelfService(); svc != nil {
		items, err := svc.List(gb(p, "include_staged"))
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(items, "")
	}
	return failResult("bookshelf service unavailable", "")
}

// doBookshelfAdd 对齐 Python shelve_by_url → shelve_book：源按 url 取回（取不到
// 当场报"书源不存在"），`shelved=false` 即 notShelf 试读暂存；同键已存在不覆盖，
// 但 shelved=true 时补一次转正（试读过的书再点"加入书架"不能"点了没反应"）。
func doBookshelfAdd(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.BookshelfService()
	if svc == nil {
		return failResult("bookshelf service unavailable", "")
	}
	sourceURL := gs(p, "source_url")
	var sourceName string
	if srcSvc := h.SourceService(); srcSvc != nil {
		if source, ok := srcSvc.Get(sourceURL); ok && source != nil {
			sourceName = source.BookSourceName
		}
	}
	if sourceName == "" {
		return failResult(fmt.Sprintf("书源不存在：%s", sourceURL), "")
	}
	bookRaw, _ := json.Marshal(p["book"])
	book := parseBookItem(bookRaw)
	if book.URL == "" {
		return failResult("missing book url", "")
	}
	shelved := gbDefault(p, "shelved", true)
	added := svc.CreateShelfRecord(sourceURL, sourceName, book, shelved)
	if shelved && !added {
		added = svc.PromoteShelfRecord(sourceURL, book.URL)
	}
	return okResult(added, "")
}

func doBookshelfUpdate(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.BookshelfService(); svc != nil {
		bookRaw, _ := json.Marshal(p["book"])
		internalBook := parseBookItem(bookRaw)
		if internalBook.URL != "" {
			if reading, ok := p["reading"]; ok {
				svc.SaveReading(internalBook.URL, reading, p["vars"])
			}
		}
		return okResult(true, "")
	}
	return failResult("bookshelf service unavailable", "")
}

func doBookshelfDelete(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.BookshelfService(); svc != nil {
		url := gs(p, "url")
		if url == "" {
			url = gs(p, "bookUrl")
		}
		if url == "" {
			return failResult("missing bookUrl", "")
		}
		if err := svc.Remove(url); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(true, "")
	}
	return failResult("bookshelf service unavailable", "")
}

// ---------------------------------------------------------------------------
// Check handlers
// ---------------------------------------------------------------------------

func doCheckStart(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.CheckService()
	if svc == nil {
		return failResult("check service unavailable", "")
	}
	groups := stringSlice(p["groups"])
	go func() {
		if _, err := svc.RunAll(groups); err != nil {
			log.Printf("[FFI] check.start failed: %v", err)
		}
	}()
	return okResult(map[string]interface{}{"started": true, "action": "check.start"}, "")
}

func doCheckSnapshot(h *services.ServiceHub) ResultEnvelope {
	svc := h.CheckService()
	if svc == nil {
		return failResult("check service unavailable", "")
	}
	return okResult(svc.Snapshot(), "")
}

func doCheckCancel(h *services.ServiceHub) ResultEnvelope {
	if svc := h.CheckService(); svc != nil {
		svc.Cancel()
	}
	return okResult(nil, "")
}

func doCheckSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.CheckService()
	if svc == nil {
		return failResult("check service unavailable", "")
	}
	sourceJSON, _ := p["source_json"].(map[string]interface{})
	if sourceJSON == nil {
		return failResult("missing source_json", "")
	}
	report, err := svc.CheckSingle(sourceJSON, gs(p, "depth"))
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(report, "")
}

// ---------------------------------------------------------------------------
// Reader handlers
// ---------------------------------------------------------------------------

// doReaderPosGet 回**存档形状**（books 表没有 chapter_url/name 两列，那两格恒空、
// 续读那一章由 chapter_index 在目录里定位）；没有记录就是 null，不是"位置为 0"。
func doReaderPosGet(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return okResult(nil, "")
	}
	data, ok := svc.ReadPositionByURL(gs(p, "source_url"), gs(p, "book_url"))
	if !ok {
		return okResult(nil, "")
	}
	return okResult(data, "")
}

func doReaderPosSave(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return failResult("reading service unavailable", "")
	}
	position, _ := p["position_json"].(map[string]interface{})
	data, err := svc.SavePositionByURL(gs(p, "source_url"), gs(p, "book_url"), position)
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(data, "")
}

// doReaderStaged 当前书若是试读暂存（notShelf）给出那条书架记录；在架的书、
// 没记录、没会话都是 null——在架的书退出时不该被问一遍。
func doReaderStaged(h *services.ServiceHub) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return okResult(nil, "")
	}
	record, ok := svc.StagedRecordBySession()
	if !ok {
		return okResult(nil, "")
	}
	return okResult(record, "")
}

func doReaderStagedPromote(h *services.ServiceHub) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return okResult(false, "")
	}
	return okResult(svc.PromoteStaged(), "")
}

func doReaderStagedDiscard(h *services.ServiceHub) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return okResult(false, "")
	}
	return okResult(svc.DiscardStaged(), "")
}

func doReaderBookmarks(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		bms, err := svc.ListBookmarks(gs(p, "book_url"))
		if err != nil {
			return failResult(err.Error(), "")
		}
		if bms == nil {
			bms = []services.Bookmark{}
		}
		return okResult(bms, "")
	}
	return failResult("reader service unavailable", "")
}

func doReaderBookmarkAdd(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		bm, err := svc.AddBookmark(
			gs(p, "book_url"),
			gi(p, "chapter_index"),
			gl(p, "offset"),
			gs(p, "summary"),
		)
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(bm.ID, "")
	}
	return failResult("reader service unavailable", "")
}

func doReaderBookmarkRemove(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		removed, err := svc.RemoveBookmark(gs(p, "book_url"), int64(gi(p, "bookmark_id")))
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(removed, "")
	}
	return failResult("reader service unavailable", "")
}

// ---------------------------------------------------------------------------
// RSS handlers
// ---------------------------------------------------------------------------

func doRssSources(h *services.ServiceHub) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		return okResult(svc.SourceRows(), "")
	}
	return okResult([]interface{}{}, "")
}

func doRssArticles(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		return okResult(svc.ArticleRows(gs(p, "source_id"), gb(p, "unread_only")), "")
	}
	return okResult([]interface{}{}, "")
}

func doRssAddSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		id, err := svc.AddSource(gs(p, "name"), gs(p, "url"), gs(p, "source_group"))
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(id, "")
	}
	return failResult("rss service unavailable", "")
}

func doRssRemoveSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		changed, err := svc.RemoveSource(gs(p, "source_id"))
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(changed, "")
	}
	return failResult("rss service unavailable", "")
}

// ---------------------------------------------------------------------------
// Settings handlers
// ---------------------------------------------------------------------------

func doSettingsRead(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SettingsService(); svc != nil {
		keysRaw, _ := p["keys"].([]interface{})
		keys := make([]string, 0, len(keysRaw))
		for _, k := range keysRaw {
			if s, ok := k.(string); ok {
				keys = append(keys, s)
			}
		}
		vals, _ := svc.ReadKeys(keys)
		return okResult(vals, "")
	}
	return failResult("settings service unavailable", "")
}

func doSettingsWrite(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SettingsService(); svc != nil {
		key := gs(p, "key")
		value := p["value"]
		if key == "" || value == nil {
			return failResult("key and value required", "")
		}
		if err := svc.Set(key, value); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(true, "")
	}
	return failResult("settings service unavailable", "")
}

// ---------------------------------------------------------------------------
// Replace handlers
// ---------------------------------------------------------------------------

func doReplaceList(h *services.ServiceHub) ResultEnvelope {
	svc := h.ReplaceService()
	if svc == nil {
		return failResult("replace service unavailable", "")
	}
	rules := svc.List()
	data := make([]interface{}, len(rules))
	for i, r := range rules {
		// 键对齐 Python replace_rule_service.list_rules 的行形状
		// （Dart 读 id/name/pattern/replacement/enabled，另三个键备用）。
		data[i] = map[string]interface{}{
			"id":          r.ID,
			"name":        r.Name,
			"pattern":     r.Pattern,
			"replacement": r.Replacement,
			"scope":       r.Scope,
			"is_regex":    r.IsRegex,
			"enabled":     r.Enabled,
		}
	}
	return okResult(data, "")
}

func doReplaceAdd(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReplaceService()
	if svc == nil {
		return failResult("replace service unavailable", "")
	}
	id, err := svc.AddRule(gs(p, "name"), gs(p, "pattern"), gs(p, "replacement"),
		gbDefault(p, "enabled", true))
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(id, "")
}

func doReplaceUpdate(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReplaceService()
	if svc == nil {
		return failResult("replace service unavailable", "")
	}
	id, ok := gid(p, "rule_id")
	if !ok {
		return failResult("rule_id required", "")
	}
	// Dart 只发 name/pattern/replacement/enabled（管理页没有 scope/is_regex 编辑）；
	// scope/is_regex 保持行上原值，与 Dart 契约一致。
	if err := svc.UpdateRule(id, gs(p, "name"), gs(p, "pattern"),
		gs(p, "replacement"), gbDefault(p, "enabled", true)); err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(true, "")
}

func doReplaceDelete(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReplaceService()
	if svc == nil {
		return failResult("replace service unavailable", "")
	}
	id, ok := gid(p, "rule_id")
	if !ok {
		return failResult("rule_id required", "")
	}
	if err := svc.DeleteRule(id); err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(true, "")
}

func doReplaceToggleGlobal(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReplaceService()
	if svc == nil {
		return failResult("replace service unavailable", "")
	}
	enabled := gbDefault(p, "enabled", true)
	if err := svc.SetGlobalEnabled(enabled); err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(enabled, "")
}

// ---------------------------------------------------------------------------
// Font handlers
// ---------------------------------------------------------------------------

func doFontScanSystem(h *services.ServiceHub) ResultEnvelope {
	svc := h.FontService()
	if svc == nil {
		return failResult("font service unavailable", "")
	}
	fonts, err := svc.ScanSystem()
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(fonts, "")
}

func doFontAddUser(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.FontService()
	if svc == nil {
		return failResult("font service unavailable", "")
	}
	path := gs(p, "file_path")
	if path == "" {
		return failResult("file_path required", "")
	}
	id, err := svc.AddUserFont(path)
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(id, "")
}

func doFontRemove(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.FontService()
	if svc == nil {
		return failResult("font service unavailable", "")
	}
	id, ok := gid(p, "font_id")
	if !ok {
		return failResult("font_id required", "")
	}
	removed, err := svc.RemoveUserFont(id)
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(removed, "")
}

func doFontSelect(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.FontService()
	if svc == nil {
		return failResult("font service unavailable", "")
	}
	if err := svc.SelectActive(gs(p, "family")); err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(true, "")
}

// ---------------------------------------------------------------------------
// Import handlers
// ---------------------------------------------------------------------------

// importEnvelope 是 import.* 唯一的回包形状：`{kind, count, errors, warnings, placeholder}`
// （Dart 侧 `ImportOutcome.fromJson` 逐格读这五个键）。少一格不是"字段没了"，是那一格
// 在界面上退化成 0 或 false——placeholder 少了就把"这类东西今天没有落点"报成"导入了 0 条"。
func importEnvelope(result *services.ImportResult) ResultEnvelope {
	return okResult(map[string]interface{}{
		"kind":        result.Kind,
		"count":       result.Count,
		"errors":      result.Errors,
		"warnings":    result.Warnings,
		"placeholder": result.Placeholder,
	}, "")
}

// importKindMode 收两个可选参数：kind 缺了就按唯一有落点的那一类（对齐 Python 控制器
// 的 IMPORT_KIND 默认），mode 缺省 append（Python 门面签名的默认值）。
func importKindMode(p map[string]interface{}) (string, string) {
	kind := gs(p, "kind")
	if kind == "" {
		kind = services.ImportKindBookSource
	}
	mode := gs(p, "mode")
	if mode == "" {
		mode = services.ImportModeAppend
	}
	return kind, mode
}

func doImportText(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ImportService()
	if svc == nil {
		return failResult("ImportService 未就绪：导入不会落库", "")
	}
	kind, mode := importKindMode(p)
	return importEnvelope(svc.ImportText(gs(p, "text"), kind, mode))
}

func doImportUrl(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ImportService()
	if svc == nil {
		return failResult("ImportService 未就绪：导入不会落库", "")
	}
	url := strings.TrimSpace(gs(p, "url"))
	if url == "" {
		return failResult("缺少 url：没说要导入哪个地址", "")
	}
	kind, mode := importKindMode(p)
	return importEnvelope(svc.ImportFromURL(url, kind, mode))
}

// ---------------------------------------------------------------------------
// Conversion helpers
// ---------------------------------------------------------------------------

func srcToMap(src internalmodels.BookSource) map[string]interface{} {
	return map[string]interface{}{
		"bookSourceUrl":   src.BookSourceURL,
		"bookSourceName":  src.BookSourceName,
		"bookSourceGroup": src.BookSourceGroup,
		"enabled":         src.Enabled,
		"customOrder":     src.CustomOrder,
		"respondTime":     src.RespondTime,
		"lastUpdateTime":  0,
	}
}

func bookToMap(b *internalmodels.BookItem) map[string]interface{} {
	if b == nil {
		return map[string]interface{}{}
	}
	return map[string]interface{}{
		"name":       b.Name,
		"author":     b.Author,
		"intro":      b.Intro,
		"url":        b.URL,
		"coverUrl":   b.CoverURL,
		"origin":     b.Origin,
		"originName": b.OriginName,
	}
}

// ---------------------------------------------------------------------------
// Theme handlers（三格外观偏好，对齐 Python app/services/tools/theme_service.py）
// ---------------------------------------------------------------------------

func doThemePreferences(h *services.ServiceHub) ResultEnvelope {
	svc := h.ThemeService()
	if svc == nil {
		return failResult("ThemeService 未就绪：外观偏好读不回来", "")
	}
	return okResult(svc.GetPreferences(), "")
}

func doThemeSave(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ThemeService()
	if svc == nil {
		return failResult("ThemeService 未就绪：外观偏好存不下去", "")
	}
	// 回的是**服务端归一后的三格**（黄金样本 theme.save.json 的 response.data），
	// 不是 true：界面拿返回值当"已保存态"，回 true 会把刚保存的那一版折回默认档。
	return okResult(svc.SetPreferences(
		optionalString(p, "skin_id"),
		optionalString(p, "mode"),
		optionalBool(p, "eink")), "")
}

// optionalString / optionalBool：键缺失或值是 JSON null → nil，意思是"这一格不改"
// （schema theme.save.json 把三格都写成可 null）。类型对不上也当没给，不折回默认。
func optionalString(p map[string]interface{}, k string) *string {
	raw, ok := p[k]
	if !ok {
		return nil
	}
	s, isStr := raw.(string)
	if !isStr {
		return nil
	}
	return &s
}

func optionalBool(p map[string]interface{}, k string) *bool {
	raw, ok := p[k]
	if !ok {
		return nil
	}
	b, isBool := raw.(bool)
	if !isBool {
		return nil
	}
	return &b
}

// ---------------------------------------------------------------------------
// Update handlers（只检查、不下载不安装）
// ---------------------------------------------------------------------------

func doUpdateCheck(h *services.ServiceHub) ResultEnvelope {
	svc := h.UpdateService()
	if svc == nil {
		return failResult("UpdateService 未就绪：没有可查询的更新源", "")
	}
	// 比服务里的客户端多留 2s 余量：这层先到期说明是调用方等不起，不是源挂了。
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	info, err := svc.Check(ctx)
	if err != nil {
		log.Printf("[update_check] %v", err)
		return failResult(err.Error(), "")
	}
	// nil = 已是最新/该版本已被忽略 → data 为 null，界面读成"已是最新版本"。
	if info == nil {
		return okResult(nil, "")
	}
	return okResult(info, "")
}

func doUpdateIgnore(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.UpdateService()
	if svc == nil {
		return failResult("UpdateService 未就绪：忽略的版本没处落库", "")
	}
	version := gs(p, "version")
	if version == "" {
		return failResult("缺少 version：没说要忽略哪个版本", "")
	}
	if err := svc.IgnoreVersion(version); err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(true, "")
}

// ---------------------------------------------------------------------------
// QR handlers
// ---------------------------------------------------------------------------

func doQRGenerate(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.QRService()
	if svc == nil {
		return failResult("QRService 未就绪：没有可用的二维码编码器", "")
	}
	text := gs(p, "text")
	if text == "" {
		return failResult("二维码内容不能为空", "")
	}
	png, err := svc.GeneratePNG(text)
	if err != nil {
		log.Printf("[qr_generate] %v", err)
		return failResult("二维码生成失败："+err.Error(), "")
	}
	// 界面（qr_code_screen）拿 base64 直接喂 Image.memory；键名 result 是 Dart 侧
	// 已经在读的那一个，不再另起一套。
	return okResult(map[string]interface{}{
		"result":       base64.StdEncoding.EncodeToString(png),
		"content_type": "image/png",
	}, "")
}

func doQRDecode(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	// 识别侧没实现（Go 侧没有纯 Go 的 QR 解码器，Python 用 opencv）。回显式错误，
	// 不回空串——空串在界面上长成"这张图里没有二维码"，那是两件不同的事（0 号原则）。
	if svc := h.QRService(); svc != nil {
		_, err := svc.Decode(gs(p, "image_data"))
		return failResult(err.Error(), "")
	}
	return failResult(services.ErrQRDecodeUnimplemented.Error(), "")
}

// parseBookItem converts raw bytes into a BookItem (handles snake_case Dart keys).
func parseBookItem(raw []byte) *internalmodels.BookItem {
	var m map[string]interface{}
	if err := json.Unmarshal(raw, &m); err != nil {
		return &internalmodels.BookItem{}
	}
	// 主口径是 35 键 snake_case 快照（事件/搜索行原样回传的那份，见 BookSnapshot）；
	// 旧 camelCase 行的键在快照里没有对应时逐个兜底。
	book := services.BookFromSnapshot(m)
	if book.URL == "" {
		book.URL = gs(m, "bookUrl")
	}
	if book.CoverURL == "" {
		book.CoverURL = gs(m, "coverUrl")
	}
	if book.OriginName == "" {
		book.OriginName = gs(m, "originName")
	}
	if book.TocURL == "" {
		book.TocURL = gs(m, "tocUrl")
	}
	if book.LastChapter == "" {
		book.LastChapter = gs(m, "lastChapter")
	}
	return book
}

// ---------------------------------------------------------------------------
// Helper: convert model types to JSON-compatible maps for Dart side
// ---------------------------------------------------------------------------

func chapterRow(ch *internalmodels.Chapter) map[string]interface{} {
	return map[string]interface{}{
		"name":      ch.Title,
		"url":       ch.URL,
		"index":     ch.Index,
		"isVolume":  ch.IsVolume,
		"isVip":     ch.IsVip,
		"isPay":     ch.IsPay,
		"tag":       ch.Tag,
		"wordCount": chapterWordCount(ch),
	}
}

// chapterWordCount 给 Dart 侧的显示串：规则/缓存里的原文（"3240字"）优先，
// 没有原文才回读到结构化字数拼串，都没有就是空串（Dart 侧空串不显示）。
func chapterWordCount(ch *internalmodels.Chapter) string {
	if ch.WordCountText != "" {
		return ch.WordCountText
	}
	if ch.WordCount != 0 {
		return fmt.Sprintf("%d", ch.WordCount)
	}
	return ""
}

func bookToMapFull(b *internalmodels.BookItem) map[string]interface{} {
	if b == nil {
		return map[string]interface{}{}
	}
	raw := map[string]interface{}{
		"name":        b.Name,
		"author":      b.Author,
		"intro":       b.Intro,
		"url":         b.URL,
		"cover_url":   b.CoverURL,
		"origin":      b.Origin,
		"origin_name": b.OriginName,
	}
	return raw
}

// ---------------------------------------------------------------------------
// Reader handlers — open / close / toc / content
// ---------------------------------------------------------------------------

// doReaderOpen 开会话（对齐 Python open_by_url）：回会话快照 + 位置，目录不在
// 这一发里（几千章另走 reader.toc）。缺省 shelve=true / resume=true。
func doReaderOpen(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return failResult("reading service unavailable", "")
	}
	bookRaw, _ := json.Marshal(p["book"])
	book := parseBookItem(bookRaw)
	if book.URL == "" {
		return failResult("missing book url", "")
	}
	data, err := svc.OpenByURL(gs(p, "source_url"), book,
		gbDefault(p, "shelve", true), gbDefault(p, "resume", true))
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(data, "")
}

// doReaderClose 结束会话（对齐 Python close_by_url）：url 对不上回
// {closed:false, staged:null} 什么都不关。缺省 save_progress=true。
func doReaderClose(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return failResult("reading service unavailable", "")
	}
	return okResult(svc.CloseByURL(gs(p, "source_url"), gs(p, "book_url"),
		gbDefault(p, "save_progress", true)), "")
}

func doReaderToc(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return failResult("reading service unavailable", "")
	}
	chapters, err := svc.TocByURL(gs(p, "source_url"), gs(p, "book_url"), gb(p, "refresh"))
	if err != nil {
		return failResult(err.Error(), "")
	}
	data := make([]interface{}, len(chapters))
	for i, ch := range chapters {
		data[i] = chapterRow(ch)
	}
	return okResult(data, "")
}

func doReaderContent(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if h == nil || p == nil {
		return failResult("invalid params", "")
	}
	sourceURL := gs(p, "source_url")
	bookURL := gs(p, "book_url")
	chapterURL := gs(p, "chapter_url")
	if sourceURL == "" || bookURL == "" || chapterURL == "" {
		return failResult("missing required params", "")
	}
	svc := h.ReadingService()
	if svc == nil {
		return failResult("reading service unavailable", "")
	}
	data, err := svc.ChapterDocument(sourceURL, bookURL, chapterURL)
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(data, "")
}

// ---------------------------------------------------------------------------
// Browse handlers
// ---------------------------------------------------------------------------

// doBrowseCategories 把书源的 exploreUrl 解析成分类清单（静态 `标题::地址`、
// JSON 数组、@js: 动态三类形状都走引擎那一套；webView 分类按设计稿 §7 显式报错）。
func doBrowseCategories(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	sourceURL := gs(p, "source_url")
	if sourceURL == "" {
		return failResult("missing source_url", "")
	}
	sourceSvc := h.SourceService()
	src, ok := sourceSvc.Get(sourceURL)
	if !ok {
		return failResult("source not found", "")
	}
	categories, err := h.RuleEngine().ResolveCategories(context.Background(), src)
	if err != nil {
		log.Printf("[browse_categories] %s: %v", sourceURL, err)
		return failResult(err.Error(), "")
	}
	data := make([]interface{}, 0, len(categories))
	for _, category := range categories {
		data = append(data, map[string]interface{}{"title": category.Title, "url": category.URL})
	}
	return okResult(data, "")
}

// doBrowseBooks 分类页书单。分类地址**原样**交给引擎（含 {{page}}、`<a,b>` 页对、
// @js:、POST 选项等形状）——在这里预替换会让引擎的页号语义变成第二套实现。
func doBrowseBooks(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	sourceURL := gs(p, "source_url")
	categoryJSON := p["category"]
	page := gi(p, "page")
	if page < 1 {
		page = 1
	}

	sourceSvc := h.SourceService()
	src, ok := sourceSvc.Get(sourceURL)
	if !ok {
		return failResult("source not found", "")
	}

	catURL := ""
	if catMap, ok := categoryJSON.(map[string]interface{}); ok {
		catURL = gs(catMap, "url")
	}
	if catURL == "" {
		return failResult("该分类没有可浏览的地址", "")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	books, err := h.RuleEngine().Explore(ctx, src, catURL, page)
	if err != nil {
		log.Printf("[browse_books] %s page=%d: %v", sourceURL, page, err)
		return failResult(err.Error(), "")
	}

	data := make([]interface{}, len(books))
	for i, b := range books {
		data[i] = bookToMapFull(b)
	}
	return okResult(data, "")
}

// ---------------------------------------------------------------------------
// Event polling
// ---------------------------------------------------------------------------

// doEnginePoll serves the Dart-side cursor poll: {"since": N} →
// {"events":[{"seq","name","payload"}], "last_seq": N, "lost": bool}.
func doEnginePoll(p map[string]interface{}) ResultEnvelope {
	ring := evtRing.Load()
	if ring == nil {
		return okResult(map[string]interface{}{"events": []interface{}{}, "last_seq": 0, "lost": false}, "")
	}
	since := int64(gi(p, "since"))
	entries, lastSeq, lost := ring.Since(since)
	return okResult(map[string]interface{}{
		"events":   entries,
		"last_seq": lastSeq,
		"lost":     lost,
	}, "")
}

// ---------------------------------------------------------------------------
// Webview bridge
// ---------------------------------------------------------------------------

// doWebContentReady acknowledges a webview render delivery from Dart.
// Contract (Dart WebviewDelivery): {"delivered": bool}. Under FFI the engine
// does not issue reverse webview requests, so there is never a pending one to
// satisfy; answering delivered=false is the honest shape (matches what the
// Dart parser produced from the old "ready" key anyway).
func doWebContentReady(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	return okResult(map[string]interface{}{"delivered": false}, "")
}

// ---------------------------------------------------------------------------
// Search handlers
// ---------------------------------------------------------------------------

// doSearchStart 起一轮多源找书（mode="job"）：受理回包只有 {started, action}，
// 逐源结果与收口走 SearchFinishedEvent 事件流（search_id 认本轮，同一 id 再调即翻页）。
func doSearchStart(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	keyword := gs(p, "keyword")
	if keyword == "" {
		return failResult("empty keyword", "")
	}
	scope := gs(p, "scope")
	searchID := int64(gl(p, "search_id"))
	if searchID == 0 {
		// 缺 search_id 的调用没有关联依据：事件回带 "0" 而界面认不到，等于结果作废。
		return failResult("missing search_id", "")
	}

	readingSvc := h.ReadingService()
	if readingSvc == nil {
		return failResult("reading service unavailable", "")
	}
	go readingSvc.SearchInScope(context.Background(), keyword, scope, searchID)

	return okResult(map[string]interface{}{
		"started": true,
		"action":  "search.start",
	}, "")
}

func doSearchScopes(h *services.ServiceHub) ResultEnvelope {
	readingSvc := h.ReadingService()
	if readingSvc == nil {
		return okResult([]interface{}{}, "")
	}
	// [{scope, count}]：范围有哪些、各带几条可搜源，只有服务端一份算法。
	return okResult(readingSvc.SearchScopes(), "")
}

// ---------------------------------------------------------------------------
// Switch/Seeds handlers
// ---------------------------------------------------------------------------

// doSwitchSeeds 本地种子库查同名书（不联网）。入参 book 是书架行原样，
// 只读 name/author；没有服务时回空数组而不是报错——对话框空空比弹红字好。
func doSwitchSeeds(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	readingSvc := h.ReadingService()
	if readingSvc == nil {
		return okResult([]interface{}{}, "")
	}
	bookRaw, _ := json.Marshal(p["book"])
	return okResult(readingSvc.SwitchSeeds(parseBookItem(bookRaw)), "")
}

// doSwitchScan 起一轮换源扫描。这是个异步动作：回包只表示"受理了"，
// 逐源进度与最终候选都走 SourceSwitch* 事件。switch_id 缺席按 0
// （对齐 Python `str(switch_id or 0)`），不拒收。
func doSwitchScan(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	readingSvc := h.ReadingService()
	if readingSvc == nil {
		return failResult("reading service unavailable", "")
	}
	record, _ := p["book"].(map[string]interface{})
	switchID := int64(gl(p, "switch_id"))
	go readingSvc.SwitchScan(context.Background(), record, switchID, gs(p, "keyword"), gs(p, "group"))
	return okResult(map[string]interface{}{"started": true, "action": "switch.scan"}, "")
}

// doSwitchSnapshot 本轮（或最近一轮）扫描的回放快照；没跑过回 9 键空轮。
func doSwitchSnapshot(h *services.ServiceHub) ResultEnvelope {
	if svc := h.ReadingService(); svc != nil {
		return okResult(svc.SwitchSnapshot(), "")
	}
	return okResult(services.IdleSwitchRun(), "")
}

// doSwitchCancel 合作式叫停在跑的扫描；没有可停的就回 cancelled=false。
func doSwitchCancel(h *services.ServiceHub) ResultEnvelope {
	if svc := h.ReadingService(); svc != nil {
		return okResult(svc.SwitchCancel(), "")
	}
	return okResult(map[string]interface{}{"cancelled": false}, "")
}

// doSwitchApply 换源写回。源按 source_url 取回；取不到**报错**而不是回
// applied=false——那三种失败成因（源没了 / 记录没了 / 冲突）得让界面分得开。
func doSwitchApply(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	readingSvc := h.ReadingService()
	if readingSvc == nil {
		return failResult("reading service unavailable", "")
	}
	record, _ := p["record"].(map[string]interface{})
	bookRaw, _ := json.Marshal(p["book"])
	result, err := readingSvc.ApplySwitch(record, gs(p, "source_url"), parseBookItem(bookRaw))
	if err != nil {
		return failResult(err.Error(), "")
	}
	return okResult(result, "")
}

// ---------------------------------------------------------------------------
// RSS article detail handler
// ---------------------------------------------------------------------------

// doRssArticle 打开一篇文章（10 键 = 列表 9 格 + 去标签的 text）。没这篇回
// data=null——Dart `openRssArticle` 把 null（"没有"）与空正文分得开。
func doRssArticle(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if h == nil || h.RSSService() == nil {
		return failResult("rss service unavailable", "")
	}
	detail := h.RSSService().ArticleDetail(int64(gi(p, "article_id")))
	return okResult(detail, "")
}

// ---------------------------------------------------------------------------
// Reader full-text search handler
// ---------------------------------------------------------------------------

// doReaderSearchContent 书内全文检索（对齐 Python search_ensured）：服务侧先按需
// 补索引再查，回 hits + written(int) + indexedChapters + ftsAvailable——
// 0 命中要分得清"没缓存"与"真没有"。
func doReaderSearchContent(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	svc := h.ReadingService()
	if svc == nil {
		return okResult(map[string]interface{}{
			"hits":            []interface{}{},
			"written":         0,
			"indexedChapters": 0,
			"ftsAvailable":    false,
		}, "")
	}
	return okResult(svc.SearchContentByURL(gs(p, "book_url"), gs(p, "keyword"), gs(p, "source_url")), "")
}
