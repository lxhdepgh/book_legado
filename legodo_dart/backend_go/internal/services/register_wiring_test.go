package services

import (
	"context"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
)

// TestHubWiringLoadsServiceCaches 钉住"注册进 hub 的壳必须转发真实服务的
// Start()"这条接线。四个服务的加载逻辑（loadAllSources / loadAllRules /
// loadAllFonts / ensureFTSTable）都挂在自己 base 的 onStart 上，而 hub 里放的
// 是另一个只带 stopFn 的 bizSvc 壳——壳不调真身，缓存就永远是空的（书源页
// 空列表、替换规则不生效、字体列表空、书内搜索降级）。各服务的单测都直接
// 注入缓存/存储，绕过了这条接线，所以只有这条全链路种子测试能钉住它。
func TestHubWiringLoadsServiceCaches(t *testing.T) {
	dir := t.TempDir()

	seed, err := core.NewStorageService(dir)
	if err != nil {
		t.Fatalf("种子存储建不起来: %v", err)
	}
	statements := []struct {
		query string
		args  []interface{}
	}{
		{
			`INSERT INTO book_sources (name, url, "group", enabled, custom_order, check_state, respond_time, json)
			 VALUES (?, ?, ?, 1, 0, 0, 180000, ?)`,
			[]interface{}{"源甲", "http://a.test", "组一", `{"bookSourceUrl":"http://a.test","bookSourceName":"源甲"}`},
		},
		{
			`INSERT INTO book_sources (name, url, enabled, custom_order, check_state, respond_time, json)
			 VALUES (?, ?, 1, 0, 0, 180000, ?)`,
			[]interface{}{"源乙", "http://b.test", `{"bookSourceUrl":"http://b.test","bookSourceName":"源乙"}`},
		},
		{
			`INSERT INTO replace_rules (name, pattern, replacement, scope, is_regex, enabled, created_at, updated_at)
			 VALUES (?, ?, ?, 'content', 1, 1, 0, 0)`,
			[]interface{}{"去广告", "广告词", ""},
		},
		{
			`INSERT INTO fonts (name, family, path) VALUES (?, ?, ?)`,
			[]interface{}{"测试字体", "测试字体", `C:\fonts\test.ttf`},
		},
		{
			`INSERT INTO settings (key, value, updated_at) VALUES (?, ?, 0)`,
			[]interface{}{"wiring.probe", "来自真库的种子值"},
		},
	}
	for i, stmt := range statements {
		if _, err := seed.Execute(stmt.query, stmt.args...); err != nil {
			t.Fatalf("种子第 %d 条落库失败: %v", i+1, err)
		}
	}
	if err := seed.Close(); err != nil {
		t.Fatalf("关种子存储失败: %v", err)
	}

	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", dir)

	hub := NewServiceHub()
	if err := hub.RegisterInfrastructure(); err != nil {
		t.Fatalf("注册基础设施失败: %v", err)
	}
	if err := hub.RegisterBusinessServices(); err != nil {
		t.Fatalf("注册业务服务失败: %v", err)
	}
	hub.StartAll(context.Background())
	t.Cleanup(func() { _ = hub.StopAll() })

	if got := len(hub.SourceService().List()); got != 2 {
		t.Errorf("书源缓存 %d 条，想要 2 —— SourceService 的启动加载没被接线（书源页会空）", got)
	}
	if got := len(hub.ReplaceService().List()); got != 1 {
		t.Errorf("替换规则缓存 %d 条，想要 1 —— ReplaceService 的启动加载没被接线", got)
	}
	if got := len(hub.FontService().List()); got != 1 {
		t.Errorf("字体缓存 %d 条，想要 1 —— FontService 的启动加载没被接线", got)
	}
	// SettingsService 读内存缓存、OnStart 时才装 DB 全表；漏接线时所有读取
	// 静默回默认值（真库有 6 个键，主题/阅读偏好全靠这条）。
	if got := hub.SettingsService().Get("wiring.probe", ""); got != "来自真库的种子值" {
		t.Errorf("settings 缓存读到 %q，想要种子值 —— SettingsService.OnStart 没被接线", got)
	}

	rows, err := hub.StorageService().Query(
		"SELECT name FROM sqlite_master WHERE type='table' AND name='content_search'")
	if err != nil {
		t.Fatalf("查 sqlite_master 失败: %v", err)
	}
	defer rows.Close()
	if !rows.Next() {
		t.Error("content_search 虚表不存在 —— ContentSearchService 的启动建表没被接线（或该 sqlite 未编 FTS5）")
	}
}
