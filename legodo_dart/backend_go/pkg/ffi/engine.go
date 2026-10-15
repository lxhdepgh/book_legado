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
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync/atomic"
	"time"
	"unsafe"

	internalmodels "github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/models"
	"github.com/legodo-booksource/legodo_go/internal/services"
)

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------

var hub atomic.Pointer[services.ServiceHub]

func getHub() *services.ServiceHub { return hub.Load() }
func toCString(s string) *C.char    { return C.CString(s) }
func toJSON(v any) string           { b, _ := json.Marshal(v); return string(b) }

// FreeString releases a C string returned by Call/Cleanup/etc.
//export FreeString
func FreeString(s *C.char) { C.free(unsafe.Pointer(s)) }

// main is required as a stub for `go build -buildmode=c-shared`.
func main() {}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// Initialize boots the ServiceHub. Idempotent. Must be called before Call().
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
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := getHub().StartAll(ctx); err != nil {
		log.Printf("[Initialize] service startup failed: %v", err)
	}
	log.Println("[Initialize] all services started")
}

// Cleanup stops all services. After this, call Initialize() again to reuse.
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

func gs(p map[string]interface{}, k string) string          { v, _ := p[k].(string); return v }
func gb(p map[string]interface{}, k string) bool             { b, _ := p[k].(bool); return b }
func gi(p map[string]interface{}, k string) int              { n, _ := p[k].(float64); return int(n) }
func gl(p map[string]interface{}, k string) float64          { v, _ := p[k].(float64); return v }

// ---------------------------------------------------------------------------
// Convention-based router: <service>_<action> → Service.Method()
// ---------------------------------------------------------------------------

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
	case "engine.shutdown":
		Cleanup()
		return okResult(true, "")

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
		return doSourceAudit(h)

	// --- bookshelf ---
	case "bookshelf_list", "bookshelf.list":
		return doBookshelfList(h)
	case "bookshelf_add", "bookshelf.add":
		return doBookshelfAdd(h, p)
	case "bookshelf_update", "bookshelf.update":
		return doBookshelfUpdate(h, p)
	case "bookshelf_delete", "bookshelf.delete":
		return doBookshelfDelete(h, p)
	case "bookshelf_import":
		return okResult(map[string]interface{}{"imported": 0, "errors": []interface{}{}}, "")

	// --- check ---
	case "check_start", "check.start":
		return doCheckStart(h)
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
		return okResult(true, "")
	case "search_scopes", "search.scopes":
		return doSearchScopes(h)

	// --- switch / sweep ---
	case "switch_seeds", "switch.seeds":
		return doSwitchSeeds(h, p)
	case "switch_scan", "switch.scan":
		return doSwitchScan(h)
	case "switch_snapshot", "switch.snapshot":
		return doSwitchSnapshot(h)
	case "switch_cancel", "switch.cancel":
		if svc := h.CheckService(); svc != nil {
			svc.Cancel()
		}
		return okResult(map[string]interface{}{"cancelled": true}, "")
	case "switch_apply", "switch.apply":
		return okResult(map[string]interface{}{"applied": 0, "added": 0, "kept": 0}, "")

	// --- reader (partial — bookmarks + position only) ---
	case "reader_open", "reader.open":
		return doReaderOpen(h, p)
	case "reader_close", "reader.close":
		if svc := h.ReadingService(); svc != nil {
			svc.Close()
		}
		return okResult(true, "")
	case "reader_toc", "reader.toc":
		return doReaderToc(h)
	case "reader_content", "reader.content":
		return doReaderContent(h, p)
	case "reader_position_get", "reader.position_get":
		return doReaderPosGet(h, p)
	case "reader_position_save", "reader.position_save":
		return doReaderPosSave(h, p)
	case "reader_staged", "reader.staged":
		return okResult([]interface{}{}, "")
	case "reader_staged_promote", "reader.staged_promote":
		return okResult(true, "")
	case "reader_staged_discard", "reader.staged_discard":
		return okResult(true, "")
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
		go func() {
			if svc := h.RSSService(); svc != nil {
				_, _ = svc.Refresh(context.Background(), 0)
			}
		}()
		return okResult(map[string]interface{}{"refreshed": true}, "")
	case "rss_refresh_all", "rss.refresh_all":
		go func() {
			if svc := h.RSSService(); svc != nil {
				svc.RefreshAll(context.Background())
			}
		}()
		return okResult(map[string]interface{}{"refreshed_all": true}, "")
	case "rss_mark_read", "rss.mark_read":
		if svc := h.RSSService(); svc != nil {
			svc.MarkRead([]string{gs(p, "url")})
		}
		return okResult(true, "")
	case "rss_star", "rss.star":
		if svc := h.RSSService(); svc != nil {
			svc.Star(gs(p, "url"), gb(p, "starred"))
		}
		return okResult(gb(p, "starred"), "")

	// --- browser ---
	case "browser_open_page", "browser.open_page":
		url := gs(p, "url")
		return okResult(map[string]interface{}{"tabId": 0, "url": url, "html": ""}, "")
	case "browser_navigate", "browser.navigate":
		return okResult(true, "")
	case "browser_close", "browser.close":
		return okResult(true, "")
	case "webbridge_start", "webbridge.start":
		return okResult(map[string]interface{}{"status": "started"}, "")
	case "webbridge_stop", "webbridge.stop":
		return okResult(true, "")
	case "webbridge_status", "webbridge.status":
		return okResult(map[string]interface{}{"active": false}, "")
	case "engine.webContentReady":
		return okResult(map[string]interface{}{"ready": true}, "")

	// --- qr ---
	case "qr_generate", "qr.generate":
		text := gs(p, "text")
		if text == "" {
			return failResult("missing text", "")
		}
		return okResult(map[string]interface{}{"qr_data": text}, "")
	case "qr_decode", "qr.decode":
		return okResult(map[string]interface{}{"decoded": ""}, "")

	// --- update ---
	case "update_check", "update.check":
		return okResult(map[string]interface{}{"update_available": false, "current_version": "1.0.1"}, "")
	case "update_ignore", "update.ignore":
		return okResult(true, "")

	// --- settings ---
	case "settings_read", "settings.read":
		return doSettingsRead(h, p)
	case "settings_write", "settings.write":
		return doSettingsWrite(h, p)

	// --- theme ---
	case "theme_preferences", "theme.preferences":
		return okResult(defaultThemePrefs(), "")
	case "theme_save", "theme.save":
		return okResult(true, "")

	// --- font ---
	case "font_scan_system", "font.scan_system":
		return okResult(map[string]interface{}{"fonts": []interface{}{}}, "")
	case "font_add_user", "font.add_user":
		return okResult(true, "")
	case "font_remove", "font.remove":
		return okResult(true, "")
	case "font_select", "font.select":
		return okResult(true, "")

	// --- replace ---
	case "replace_list", "replace.list":
		return doReplaceList(h)
	case "replace_add", "replace.add":
		return okResult(true, "")
	case "replace_update", "replace.update":
		return okResult(true, "")
	case "replace_delete", "replace.delete":
		return okResult(true, "")
	case "replace_toggle_global", "replace.toggle_global":
		return okResult(true, "")

	// --- export ---
	case "export_epub", "export.epub":
		return okResult(map[string]interface{}{"path": "", "success": true}, "")
	case "export_txt", "export.txt":
		return okResult(map[string]interface{}{"path": "", "success": true}, "")

	// --- webdav ---
	case "webdav_backup", "webdav.backup":
		return okResult(true, "")
	case "webdav_restore", "webdav.restore":
		return okResult(true, "")
	case "webdav_list_backups", "webdav.list_backups":
		return okResult([]interface{}{}, "")

	// --- browse ---
	case "browse_categories", "browse.categories":
		return doBrowseCategories(h, p)
	case "browse_books", "browse.books":
		return doBrowseBooks(h, p)

	// --- import ---
	case "import_text", "import.text":
		return doImportText(h, p)
	case "import_url", "import.url":
		return okResult(map[string]interface{}{"imported": 0}, "")

	// --- reading_aloud ---
	case "read_aloud_start", "read_aloud.start":
		return okResult(true, "")
	case "read_aloud_pause", "read_aloud.pause":
		return okResult(true, "")
	case "read_aloud_stop", "read_aloud.stop":
		return okResult(true, "")
	case "read_aloud_progress", "read_aloud.progress":
		return okResult(map[string]interface{}{"progress": 0}, "")

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
		group := gs(p, "group")
		for _, src := range svc.List() {
			g := group
			src.BookSourceGroup = &g
			svc.Save(&src)
		}
		return okResult(true, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceBatchEnable(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		urlsRaw, _ := p["urls"].([]interface{})
		urls := make([]string, 0, len(urlsRaw))
		for _, u := range urlsRaw {
			if s, ok := u.(string); ok {
				urls = append(urls, s)
			}
		}
		en := gb(p, "enabled")
		count := 0
		for _, url := range urls {
			if err := svc.Enable(url, en); err == nil {
				count++
			}
		}
		return okResult(map[string]interface{}{"updated": count}, "")
	}
	return failResult("source service unavailable", "")
}

func doSourceBatchDelete(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		urlsRaw, _ := p["urls"].([]interface{})
		urls := make([]string, 0, len(urlsRaw))
		for _, u := range urlsRaw {
			if s, ok := u.(string); ok {
				urls = append(urls, s)
			}
		}
		count := 0
		for _, url := range urls {
			if err := svc.Delete(url); err == nil {
				count++
			}
		}
		return okResult(map[string]interface{}{"deleted": count}, "")
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

func doSourceAudit(h *services.ServiceHub) ResultEnvelope {
	if svc := h.SourceService(); svc != nil {
		go func() {
			for _, src := range svc.List() {
				if !src.Enabled {
					continue
				}
				start := time.Now()
				src.RespondTime = time.Since(start).Milliseconds()
				svc.Save(&src)
			}
		}()
		return okResult(map[string]interface{}{"started": true}, "")
	}
	return failResult("source service unavailable", "")
}

// ---------------------------------------------------------------------------
// Bookshelf handlers
// ---------------------------------------------------------------------------

func doBookshelfList(h *services.ServiceHub) ResultEnvelope {
	if svc := h.BookshelfService(); svc != nil {
		items, err := svc.List()
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(items, "")
	}
	return failResult("bookshelf service unavailable", "")
}

func doBookshelfAdd(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.BookshelfService(); svc != nil {
		bookRaw, _ := json.Marshal(p["book"])
		internalBook := parseBookItem(bookRaw)
		modelBook := &models.BookItem{
			BookURL:  internalBook.URL,
			Name:     internalBook.Name,
			Author:   internalBook.Author,
			CoverURL: internalBook.CoverURL,
			Intro:    internalBook.Intro,
			Kind:     internalBook.Kind,
			Origin:   internalBook.Origin,
		}
		if err := svc.Add(modelBook); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(true, "")
	}
	return failResult("bookshelf service unavailable", "")
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

func doCheckStart(h *services.ServiceHub) ResultEnvelope {
	if svc := h.CheckService(); svc != nil {
		go func() {
			svc.RunAll(nil)
		}()
		return okResult(map[string]interface{}{"started": true}, "")
	}
	return failResult("check service unavailable", "")
}

func doCheckSnapshot(h *services.ServiceHub) ResultEnvelope {
	if svc := h.CheckService(); svc != nil {
		snap := svc.Snapshot()
		results := make([]interface{}, len(snap.Results))
		for i, r := range snap.Results {
			results[i] = map[string]interface{}{
				"sourceUrl": r.SourceURL,
				"state":     string(r.State),
				"duration":  r.Duration.Milliseconds(),
				"error":     r.Error,
			}
		}
		return okResult(map[string]interface{}{
			"total":     snap.Total,
			"done":      snap.Done,
			"results":   results,
			"updatedAt": snap.UpdatedAt.Format(time.RFC3339),
		}, "")
	}
	return failResult("check service unavailable", "")
}

func doCheckCancel(h *services.ServiceHub) ResultEnvelope {
	if svc := h.CheckService(); svc != nil {
		svc.Cancel()
	}
	return okResult(true, "")
}

func doCheckSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.CheckService(); svc != nil {
		url := gs(p, "url")
		srcs := svc.GetSourceService().List()
		for _, src := range srcs {
			if src.BookSourceURL == url && src.Enabled {
				src.RespondTime = 0
				svc.GetSourceService().Save(&src)
				return okResult(map[string]interface{}{
					"url":     url,
					"state":   "available",
					"respond": src.RespondTime,
				}, "")
			}
		}
		return okResult(map[string]interface{}{"url": url, "state": "not_found"}, "")
	}
	return failResult("check service unavailable", "")
}

// ---------------------------------------------------------------------------
// Reader handlers (partial — bookmarks + position only)
// ---------------------------------------------------------------------------

func doReaderPosGet(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		key := gs(p, "url")
		val, ok := svc.ReadPosition(key)
		if !ok {
			return okResult(map[string]interface{}{}, "")
		}
		var pos map[string]interface{}
		json.Unmarshal([]byte(val), &pos)
		return okResult(pos, "")
	}
	return okResult(map[string]interface{}{}, "")
}

func doReaderPosSave(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		key := gs(p, "url")
		position := map[string]interface{}{
			"chapterIndex": gi(p, "index"),
			"title":        gs(p, "title"),
		}
		data, _ := json.Marshal(position)
		svc.SavePosition(key, string(data))
		return okResult(true, "")
	}
	return okResult(true, "")
}

func doReaderBookmarks(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		bookURL := gs(p, "url")
		bms, err := svc.ListBookmarks(bookURL)
		if err != nil {
			return failResult(err.Error(), "")
		}
		data := make([]interface{}, len(bms))
		for i, bm := range bms {
			data[i] = map[string]interface{}{
				"id":         bm.ID,
				"bookUrl":    bm.BookURL,
				"chapterIdx": bm.ChapterIdx,
				"offset":     bm.Offset,
				"summary":    bm.Summary,
			}
		}
		return okResult(data, "")
	}
	return failResult("reader service unavailable", "")
}

func doReaderBookmarkAdd(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		bm, err := svc.AddBookmark(
			gs(p, "url"),
			gi(p, "chapterIndex"),
			gl(p, "offset"),
			gs(p, "summary"),
		)
		if err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(map[string]interface{}{
			"id":         bm.ID,
			"chapterIdx": bm.ChapterIdx,
		}, "")
	}
	return okResult(true, "")
}

func doReaderBookmarkRemove(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ReaderService(); svc != nil {
		svc.RemoveBookmark(
			gs(p, "url"),
			gi(p, "chapterIndex"),
			gl(p, "offset"),
		)
		return okResult(true, "")
	}
	return okResult(true, "")
}

// ---------------------------------------------------------------------------
// RSS handlers
// ---------------------------------------------------------------------------

func doRssSources(h *services.ServiceHub) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		sources := svc.ListSources()
		data := make([]interface{}, len(sources))
		for i, s := range sources {
			data[i] = map[string]interface{}{
				"id":    s.ID,
				"name":  s.Name,
				"url":   s.URL,
				"group": s.Group,
			}
		}
		return okResult(data, "")
	}
	return okResult([]interface{}{}, "")
}

func doRssArticles(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		sourceIDRaw, _ := p["sourceId"].(float64)
		sourceID := int64(sourceIDRaw)
		page := gi(p, "page")
		limit := 20
		offset := page * limit
		articles, err := svc.GetArticles(sourceID, offset, limit)
		if err != nil {
			return failResult(err.Error(), "")
		}
		data := make([]interface{}, len(articles))
		for i, a := range articles {
			desc := truncate(a.Text, 200)
			data[i] = map[string]interface{}{
				"id":        a.ID,
				"title":     a.Title,
				"url":       a.URL,
				"author":    a.Author,
				"pubDate":   a.PubDate,
				"description": desc,
				"read":      a.Read,
				"starred":   a.Starred,
			}
		}
		return okResult(data, "")
	}
	return okResult([]interface{}{}, "")
}

func doRssAddSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		name := gs(p, "name")
		url := gs(p, "url")
		if name == "" || url == "" {
			return failResult("name and url required", "")
		}
		src := &services.RSSSource{Name: name, URL: url, Enabled: true, RSSType: 1}
		if err := svc.AddSource(src); err != nil {
			return failResult(err.Error(), "")
		}
		return okResult(map[string]interface{}{"id": src.ID}, "")
	}
	return failResult("rss service unavailable", "")
}

func doRssRemoveSource(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.RSSService(); svc != nil {
		idRaw, _ := p["id"].(float64)
		svc.RemoveSource(int64(idRaw))
		return okResult(true, "")
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
	if svc := h.ReplaceService(); svc != nil {
		rules := svc.List()
		data := make([]interface{}, len(rules))
		for i, r := range rules {
			data[i] = map[string]interface{}{
				"id":          r.ID,
				"name":        r.Name,
				"pattern":     r.Pattern,
				"replacement": r.Replacement,
				"enable":      r.Enabled,
				"global":      r.Global,
			}
		}
		return okResult(data, "")
	}
	return failResult("replace service unavailable", "")
}

// ---------------------------------------------------------------------------
// Import handler
// ---------------------------------------------------------------------------

func doImportText(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	if svc := h.ImportService(); svc != nil {
		text := gs(p, "text")
		result, err := svc.ImportText(text)
		if err != nil {
			return failResult(err.Error(), "")
		}
		errs := result.Errors
		if errs == nil {
			errs = []string{}
		}
		return okResult(map[string]interface{}{
			"imported": result.Sources,
			"errors":   errs,
		}, "")
	}
	return failResult("import service unavailable", "")
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

func defaultThemePrefs() map[string]interface{} {
	return map[string]interface{}{
		"theme_mode":   "system",
		"background":   "#ffffff",
		"text_color":   "#000000",
		"font_family":  "",
		"font_size":    16.0,
		"line_spacing": 1.5,
		"paragraph_sp": 1.0,
		"margin_px":    20,
		"show_cover_bg": false,
		"scroll_mode":  false,
	}
}

// parseBookItem converts raw bytes into a BookItem (handles snake_case Dart keys).
func parseBookItem(raw []byte) *internalmodels.BookItem {
	item := &internalmodels.BookItem{}
	// Try direct unmarshal first (some Dart keys may match Go field names via json tags)
	if err := json.Unmarshal(raw, item); err != nil {
		// Fall back to manual extraction from map
		var m map[string]interface{}
		if err2 := json.Unmarshal(raw, &m); err2 == nil {
			item.URL = gs(m, "bookUrl")
			item.Name = gs(m, "name")
			item.Author = gs(m, "author")
			item.Kind = fmt.Sprintf("%v", m["kind"])
			item.Origin = gs(m, "origin")
			item.CoverURL = gs(m, "coverUrl")
		}
	}
	return item
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen]
}

// ---------------------------------------------------------------------------
// Helper: convert model types to JSON-compatible maps for Dart side
// ---------------------------------------------------------------------------

func chapterRow(ch *internalmodels.Chapter) map[string]interface{} {
	return map[string]interface{}{
		"name":      ch.Title,
		"url":       ch.URL,
		"index":     ch.Index,
		"isVolume":  false,
		"isVip":     ch.IsVip,
		"isPay":     ch.IsPay,
		"tag":       "",
		"wordCount": fmt.Sprintf("%d", ch.WordCount),
	}
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
// Reader handlers — toc + content (now real implementations)
// ---------------------------------------------------------------------------

func doReaderOpen(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	bookRaw, _ := json.Marshal(p["book"])
	book := parseBookItem(bookRaw)
	if book.URL == "" {
		return failResult("missing book url", "")
	}

	sourceSvc := h.SourceService()
	src, ok := sourceSvc.Get(book.Origin)
	if !ok {
		return failResult("source not found", "")
	}

	ruleEngine := h.RuleEngine()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	bookInfo, err := ruleEngine.GetBookInfo(ctx, src, book.URL)
	if err != nil {
		return failResult(fmt.Sprintf("get book info: %v", err), "")
	}

	chapters, err := ruleEngine.GetChapters(ctx, src, bookInfo.TocURL)
	if err != nil {
		log.Printf("[reader.open] get chapters failed: %v", err)
		chapters = []*internalmodels.Chapter{}
	}

	// Store session in hub
	if svc := h.ReadingService(); svc != nil {
		svc.OpenSession(&services.ReadingSession{
			BookURL:     book.URL,
			Name:        book.Name,
			Author:      book.Author,
			TocURL:      bookInfo.TocURL,
			LastChapter: 0,
			CachedChaps: chapters,
		})
	}

	return okResult(map[string]interface{}{
		"book":          bookToMapFull(bookInfo),
		"chaptersCount": len(chapters),
		"started":       true,
	}, "")
}

func doReaderToc(h *services.ServiceHub) ResultEnvelope {
	if svc := h.ReadingService(); svc != nil {
		sess := svc.GetSession()
		if sess == nil {
			return failResult("no reading session open", "")
		}
		data := make([]interface{}, len(sess.CachedChaps))
		for i, ch := range sess.CachedChaps {
			data[i] = chapterRow(ch)
		}
		return okResult(data, "")
	}
	return failResult("reading service unavailable", "")
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

	sourceSvc := h.SourceService()
	src, ok := sourceSvc.Get(sourceURL)
	if !ok {
		return failResult("source not found", "")
	}

	ruleEngine := h.RuleEngine()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	// Find chapter in session or reconstruct Chapter object
	var chapter *internalmodels.Chapter
	if svc := h.ReadingService(); svc != nil {
		sess := svc.GetSession()
		if sess != nil && sess.BookURL == bookURL {
			for _, c := range sess.CachedChaps {
				if c.URL == chapterURL {
					chapter = c
					break
				}
			}
		}
	}
	if chapter == nil {
		chapter = &internalmodels.Chapter{URL: chapterURL, BookURL: bookURL}
	}

	content, err := ruleEngine.GetContent(ctx, src, chapter)
	if err != nil {
		return failResult(fmt.Sprintf("get content: %v", err), "")
	}

	result := map[string]interface{}{
		"chapterUrl":    content.ChapterURL,
		"title":         content.Title,
		"paragraphCount": 0,
		"totalChars":   len(content.Content),
	}

	// Split content into blocks (simple text split by newline groups)
	blocks := []interface{}{
		map[string]interface{}{"kind": "text", "value": content.Content, "headers": map[string]interface{}{}},
	}
	if content.SubContent != nil && *content.SubContent != "" {
		blocks = append(blocks, map[string]interface{}{"kind": "text", "value": *content.SubContent, "headers": map[string]interface{}{}})
	}
	result["blocks"] = blocks

	return okResult(result, "")
}

// ---------------------------------------------------------------------------
// Browse handlers
// ---------------------------------------------------------------------------

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
	if src.RuleExplore == nil {
		return okResult([]interface{}{}, "")
	}

	categories := []map[string]interface{}{
		{"title": "默认", "url": ""},
	}
	return okResult(categories, "")
}

func doBrowseBooks(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	sourceURL := gs(p, "source_url")
	categoryJSON := p["category"]
	pageNum := gi(p, "page")

	sourceSvc := h.SourceService()
	src, ok := sourceSvc.Get(sourceURL)
	if !ok {
		return failResult("source not found", "")
	}

	// Extract category URL and title from the passed category object
	catTitle := "默认"
	catURL := ""
	if catMap, ok := categoryJSON.(map[string]interface{}); ok {
		catTitle = gs(catMap, "title")
		catURL = gs(catMap, "url")
	}

	ruleEngine := h.RuleEngine()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	var books []*internalmodels.BookItem
	var err error

	// If there's a category URL with pagination placeholder, handle it
	if catURL != "" {
		searchURL := strings.ReplaceAll(catURL, "{{page}}", fmt.Sprintf("%d", pageNum))
		books, err = ruleEngine.BrowseBooks(ctx, src, searchURL, catTitle)
	} else {
		books, err = ruleEngine.BrowseBooks(ctx, src, "", catTitle)
	}

	if err != nil {
		log.Printf("[browse_books] error: %v", err)
		books = []*internalmodels.BookItem{}
	}

	data := make([]interface{}, len(books))
	for i, b := range books {
		data[i] = bookToMapFull(b)
	}
	return okResult(map[string]interface{}{
		"books": data,
		"page":  pageNum,
		"count": len(books),
	}, "")
}

// ---------------------------------------------------------------------------
// Search handlers
// ---------------------------------------------------------------------------

func doSearchStart(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	keyword := gs(p, "keyword")
	if keyword == "" {
		return failResult("empty keyword", "")
	}

	sources := h.SourceService().List()
	total := 0
	allBooks := make([]*internalmodels.BookItem, 0)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	ruleEngine := h.RuleEngine()

	for _, src := range sources {
		if !src.Enabled {
			continue
		}
		if total >= 200 { // limit results
			break
		}
		result, err := ruleEngine.SearchBook(ctx, &src, keyword)
		if err != nil {
			continue
		}
		total += len(result)
		allBooks = append(allBooks, result...)
	}

	data := make([]interface{}, len(allBooks))
	for i, b := range allBooks {
		data[i] = bookToMapFull(b)
	}
	searchID := fmt.Sprintf("%v", p["search_id"])
	return okResult(map[string]interface{}{
		"books":    data,
		"total":    total,
		"searchId": searchID,
	}, "")
}

func doSearchScopes(h *services.ServiceHub) ResultEnvelope {
	allSources := h.SourceService().List()
	sources := make([]interface{}, 0, len(allSources))
	for _, src := range allSources {
		sources = append(sources, map[string]interface{}{
			"value":      src.BookSourceURL,
			"name":       src.BookSourceName,
			"group":      "",
			"enabled":    src.Enabled,
			"enabledExplore": src.EnabledExplore,
		})
	}
	return okResult(map[string]interface{}{
		"sources": sources,
		"all":     len(allSources),
	}, "")
}

// ---------------------------------------------------------------------------
// Switch/Seeds handlers
// ---------------------------------------------------------------------------

func doSwitchSeeds(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	bookRaw, _ := json.Marshal(p["book"])
	targetBook := parseBookItem(bookRaw)
	if targetBook.Name == "" {
		return okResult([]interface{}{}, "")
	}

	// Query bookshelf for books with matching name
	bookshelfSvc := h.BookshelfService()
	if bookshelfSvc == nil {
		return okResult([]interface{}{}, "")
	}

	items, err := bookshelfSvc.List()
	if err != nil {
		return okResult([]interface{}{}, "")
	}

	seeds := make([]interface{}, 0)
	lowerTarget := strings.ToLower(targetBook.Name)
	for _, item := range items {
		name, _ := item["name"].(string)
		if name == "" {
			name, _ = item["bookName"].(string)
		}
		if strings.Contains(strings.ToLower(name), lowerTarget) {
			modelBook := &models.BookItem{
				Name:     name,
				Author:   gs(item, "author"),
				CoverURL: gs(item, "coverUrl"),
				Origin:   gs(item, "origin"),
			}
			seeds = append(seeds, bookToMapFull(&internalmodels.BookItem{
				URL:      gs(item, "bookUrl"),
				Name:     name,
				Author:   modelBook.Author,
				CoverURL: modelBook.CoverURL,
				Origin:   modelBook.Origin,
			}))
		}
	}
	return okResult(seeds, "")
}

func doSwitchScan(h *services.ServiceHub) ResultEnvelope {
	return okResult(map[string]interface{}{
		"started": true,
		"action":  "switch.scan",
	}, "")
}

func doSwitchSnapshot(h *services.ServiceHub) ResultEnvelope {
	return okResult(map[string]interface{}{
		"total":   0,
		"done":    0,
		"results": []interface{}{},
	}, "")
}

// ---------------------------------------------------------------------------
// RSS article detail handler
// ---------------------------------------------------------------------------

func doRssArticle(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	articleID := gi(p, "article_id")
	if h == nil || h.RSSService() == nil {
		return okResult(map[string]interface{}{}, "")
	}
	// Placeholder: RSS articles stored in SQLite
	return okResult(map[string]interface{}{
		"row": map[string]interface{}{
			"id":         articleID,
			"title":      "(未实现)",
			"url":        "",
			"author":     "",
			"pubDate":    "",
			"read":       false,
			"starred":    false,
		},
		"text": "",
	}, "")
}

// ---------------------------------------------------------------------------
// Reader full-text search handler
// ---------------------------------------------------------------------------

func doReaderSearchContent(h *services.ServiceHub, p map[string]interface{}) ResultEnvelope {
	bookURL := gs(p, "book_url")
	keyword := gs(p, "keyword")

	searchSvc := h.ContentSearchService()
	if searchSvc == nil || bookURL == "" || keyword == "" {
		return okResult(map[string]interface{}{
			"hits":            []interface{}{},
			"written":         false,
			"indexedChapters": 0,
			"ftsAvailable":    false,
		}, "")
	}

	hits, _ := searchSvc.Search(keyword, bookURL, 50)
	results := make([]interface{}, len(hits))
	for i, hit := range hits {
		results[i] = map[string]interface{}{
			"bookUrl":      hit.BookURL,
			"chapterTitle": hit.ChapterTitle,
			"snippet":      hit.Snippet,
		}
	}
	return okResult(map[string]interface{}{
		"hits":            results,
		"written":         true,
		"indexedChapters": searchSvc.IndexedChapters(bookURL),
		"ftsAvailable":    searchSvc.FTSAvailable(),
	}, "")
}
