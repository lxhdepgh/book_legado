package services

// ThemeService 的护栏：钉的是**线上契约与"没发生"**，不读被测代码里的常量。
//
// 为什么这一族要单独测：Go 侧那份实现原本是照着一个臆造的形状写的
// （screenMode/fontSize/bgColor/…，settings 键 "theme.preferences"），而界面与
// 黄金样本要的是 {skin_id, mode, eink} 落在 "appearance.snapshot" 那一格。
// 键名与字段名都是字符串，编不了错、跑不红——只能靠把字面量钉在这里。
//
// 键名一律写死字面量，不许 `themeSettingsKey`：常量漂了这条得先红。

import (
	"encoding/json"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
)

func newThemeTestStore(t *testing.T) (*core.SettingsService, *core.StorageService) {
	t.Helper()
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储失败：%v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })
	settings, err := core.NewSettingsService(storage, core.NewEventBus())
	if err != nil {
		t.Fatalf("建 settings 服务失败：%v", err)
	}
	return settings, storage
}

// strPtr 复用包内既有的那一个（check_service.go）；这里只补指针版的 eink。
func boolPtr(b bool) *bool { return &b }

// TestThemePreferencesWireShape 空库首屏：回的是那三格，且默认档是
// classic_blue / system / false（黄金样本 field_names = eink/mode/skin_id）。
func TestThemePreferencesWireShape(t *testing.T) {
	settings, _ := newThemeTestStore(t)
	svc := NewThemeService(settings)
	if err := svc.Start(); err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	raw, err := json.Marshal(svc.GetPreferences())
	if err != nil {
		t.Fatalf("序列化失败：%v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("回包不是对象：%s", raw)
	}
	if len(got) != 3 {
		t.Fatalf("三格契约变成 %d 格：%s", len(got), raw)
	}
	if got["skin_id"] != "classic_blue" || got["mode"] != "system" || got["eink"] != false {
		t.Fatalf("默认档不对：%s（应 classic_blue/system/false）", raw)
	}
}

// TestThemeSaveKeepsUnsentFields 钉 "None 的那一格保持原值"：
// 只发 eink 时，先前存下的皮肤与明暗必须原样留下。
func TestThemeSaveKeepsUnsentFields(t *testing.T) {
	settings, _ := newThemeTestStore(t)
	svc := NewThemeService(settings)
	if err := svc.Start(); err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	saved := svc.SetPreferences(strPtr("warm_amber"), strPtr("dark"), nil)
	if saved.SkinID != "warm_amber" || saved.Mode != "dark" {
		t.Fatalf("第一发就没存住：%+v", saved)
	}
	// 第二发只动 eink（Dart 的 theme.save 三格各自可缺省）。
	next := svc.SetPreferences(nil, nil, boolPtr(true))
	if next.Mode != "dark" || next.SkinID != "warm_amber" || !next.Eink {
		t.Fatalf("缺省的格被改写：%+v（应保 dark/warm_amber，只动 eink）", next)
	}
}

// TestThemeSaveNormalizesUnknownValues 表外的值是"旧库历史/手滑"，折回默认而不是报错。
func TestThemeSaveNormalizesUnknownValues(t *testing.T) {
	settings, _ := newThemeTestStore(t)
	svc := NewThemeService(settings)
	if err := svc.Start(); err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	got := svc.SetPreferences(strPtr("not_a_skin"), strPtr("sideways"), nil)
	if got.SkinID != "classic_blue" {
		t.Fatalf("表外皮肤没折回默认：%q", got.SkinID)
	}
	if got.Mode != "system" {
		t.Fatalf("表外明暗没折回 system：%q", got.Mode)
	}
}

// TestThemeSnapshotPersistenceKey 落库键必须是 "appearance.snapshot"：
// 这一格同时是 flet 侧与 settings.read 契约读取的那一格
// （test/golden_contract_test.dart「settings.read 的外观藏在 appearance.snapshot 那一格里」）。
func TestThemeSnapshotPersistenceKey(t *testing.T) {
	settings, storage := newThemeTestStore(t)
	svc := NewThemeService(settings)
	if err := svc.Start(); err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	svc.SetPreferences(strPtr("emerald_green"), strPtr("light"), nil)

	var value string
	err := storage.QueryRow(
		`SELECT value FROM settings WHERE key='appearance.snapshot'`).Scan(&value)
	if err != nil {
		t.Fatalf("按字面键读库失败：%v（服务写去的键不是这一格）", err)
	}
	var snap map[string]interface{}
	if err := json.Unmarshal([]byte(value), &snap); err != nil {
		t.Fatalf("库里那格不是 JSON 对象：%q", value)
	}
	if snap["skin_id"] != "emerald_green" || snap["mode"] != "light" {
		t.Fatalf("落库内容不对：%q", value)
	}
	// 反向：不许再往旧键 "theme.preferences" 写（那是 Go 臆造形状留下的键）。
	var legacy string
	if err := storage.QueryRow(
		`SELECT value FROM settings WHERE key='theme.preferences'`).Scan(&legacy); err == nil {
		t.Fatalf("还在往旧键 theme.preferences 写：%q", legacy)
	}
}

// TestThemeSnapshotLoadsAtStart 重启读回：新服务实例必须从库里恢复三格，
// 而不是首屏全默认（用户存了皮肤、下次打开变回经典蓝）。
func TestThemeSnapshotLoadsAtStart(t *testing.T) {
	settings, _ := newThemeTestStore(t)
	if err := settings.Set("appearance.snapshot", map[string]interface{}{
		"skin_id": "warm_amber", "mode": "light", "eink": false,
	}); err != nil {
		t.Fatalf("预置快照失败：%v", err)
	}
	restarted := NewThemeService(settings)
	if err := restarted.Start(); err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	got := restarted.GetPreferences()
	if got != (ThemePreferences{SkinID: "warm_amber", Mode: "light", Eink: false}) {
		t.Fatalf("启动没读回快照：%+v", got)
	}
}

// TestThemeBadStoredValueDoesNotBlockStart 库里那格是坏的（非对象/半截 JSON）时，
// 服务照常起、按默认档走——读不回偏好不等于设置页打不开。
func TestThemeBadStoredValueDoesNotBlockStart(t *testing.T) {
	settings, _ := newThemeTestStore(t)
	if err := settings.Set("appearance.snapshot", "not json at all"); err != nil {
		t.Fatalf("预置坏值失败：%v", err)
	}
	svc := NewThemeService(settings)
	if err := svc.Start(); err != nil {
		t.Fatalf("坏快照把启动带崩了：%v", err)
	}
	if got := svc.GetPreferences(); got.SkinID != "classic_blue" || got.Mode != "system" {
		t.Fatalf("坏值应当折回默认档，得 %+v", got)
	}
}
