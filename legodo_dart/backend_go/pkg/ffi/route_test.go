package main

// FFI 面上的路由覆盖测试：Dart 适配器发出的每一个 action 都必须落在 route 的某个
// case 上，未知 action 必须落 default 并原样带出动作名。
//
// 动作清单从 Dart 侧**现读**：lib/core/rpc/ffi_engine_adapter.dart 里 81 个
// `_syncCall<...>('action', ...)` 的首参 + 事件通道直发的 'engine_poll'（其余两处
// `ffi.call(action, ...)` 是转发形参，不算字面量；rpc_engine_repository.dart 的
// 点号形是 WS 时代的发送面，走的是待删的老链）。**不从这里回抄路由表**——回抄会让
// "Dart 发了 X 而 Go 没路由 X"永不可见，而那正是本测试要抓的失败。
// 重提取命令（Dart 增动作时更新此表与条数断言）：
//
//	grep -oE "_syncCall<[^(]*\('[A-Za-z0-9_.]+'" legodo_dart/lib/core/rpc/ffi_engine_adapter.dart
//
// 空载荷执行是安全的：这些 handler 缺参一律早退回错误信封（rss.refresh 空
// source_id 直接 (0,nil) 不外发），作业类（check/search/switch/rss_all）在空库上
// 立即收口。所以判据只是**路由**：回包的 error 不许是 default 分支的形状。

import (
	"strings"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/services"
)

// ffiWireActions — FFI 适配器现读的发送面（81 蛇形 + engine_poll = 82，2026-10-16 提取）。
var ffiWireActions = []string{
	"bookshelf_add", "bookshelf_list", "browse_books", "browse_categories",
	"browser_close", "browser_navigate", "browser_open_page",
	"check_cancel", "check_snapshot", "check_source", "check_start",
	"engine_poll", "engine_shutdown", "engine_status", "engine_web_content_ready",
	"export_epub", "export_txt",
	"font_add_user", "font_remove", "font_scan_system", "font_select",
	"import_text", "import_url",
	"qr_decode", "qr_generate",
	"reader_bookmark_add", "reader_bookmark_remove", "reader_bookmarks",
	"reader_close", "reader_content", "reader_open",
	"reader_position_get", "reader_position_save", "reader_search_content",
	"reader_staged", "reader_staged_discard", "reader_staged_promote", "reader_toc",
	"replace_add", "replace_delete", "replace_list", "replace_toggle_global", "replace_update",
	"rss_add_source", "rss_article", "rss_articles", "rss_mark_read",
	"rss_refresh", "rss_refresh_all", "rss_remove_source", "rss_sources", "rss_star",
	"search_scopes", "search_start", "search_stop",
	"settings_read", "settings_write",
	"source_audit", "source_batch_delete", "source_batch_enable", "source_delete",
	"source_enable", "source_export", "source_get", "source_group", "source_list", "source_save",
	"switch_apply", "switch_cancel", "switch_scan", "switch_seeds", "switch_snapshot",
	"theme_preferences", "theme_save",
	"update_check", "update_ignore",
	"webbridge_start", "webbridge_status", "webbridge_stop",
	"webdav_backup", "webdav_list_backups", "webdav_restore",
}

func TestRouteCoversFFIWireSurface(t *testing.T) {
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", t.TempDir())
	hub := services.NewServiceHub()
	if err := hub.RegisterInfrastructure(); err != nil {
		t.Fatalf("register infrastructure: %v", err)
	}
	if err := hub.RegisterBusinessServices(); err != nil {
		t.Fatalf("register business services: %v", err)
	}
	// 与 bookmark_handlers_test 同一理由：StopAll 在没 StartAll 时是空转，
	// 但关上库 Windows 上 TempDir 才删得掉。
	t.Cleanup(func() {
		if s := hub.StorageService(); s != nil {
			_ = s.Close()
		}
	})

	// sink 判据现读现场：未知动作落 default 的形状就是"没路由"的形状。
	// 正向用现读的这条前缀，而不是测试自带常量——路由表换了 default 文案，
	// 这里先红，要求连判据一起重看。
	const probe = "definitely_not_a_route_zzz"
	unknown := route(hub, probe, nil)
	if unknown.Ok {
		t.Fatalf("未知动作必须失败（落 default），回包 ok=true")
	}
	if unknown.Error != "unimplemented: "+probe {
		t.Fatalf("default 分支文案变了：%q —— 先更新这条钉，再谈正向判据", unknown.Error)
	}
	sink := strings.TrimSuffix(unknown.Error, probe)

	// 条数钉住：清单被截表/丢行会静默缩小覆盖面。
	if len(ffiWireActions) != 82 {
		t.Fatalf("清单 %d 条，应为提取时的 82 —— 重提取并核对后再改", len(ffiWireActions))
	}

	for _, action := range ffiWireActions {
		env := route(hub, action, nil)
		if strings.HasPrefix(env.Error, sink) {
			t.Errorf("Dart 侧发的 %s 没有路由（落进 default）：%q", action, env.Error)
		}
	}
}
