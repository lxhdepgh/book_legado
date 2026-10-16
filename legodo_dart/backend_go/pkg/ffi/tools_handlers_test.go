package main

// replace.* / font.* 面的契约测试。断言口径照 **Dart 适配器走线的那一侧**：
// lib/core/rpc/ffi_engine_adapter.dart 发出的 replace_add{name,pattern,
// replacement,enabled}、replace_update{rule_id:String,...}、replace_delete
// {rule_id:String}、replace_toggle_global{enabled}、font_add_user{file_path}、
// font_remove{font_id:String}、font_select{family}（与 Python
// app/gateway/methods.py 的 G4-E 工具域一致）。
//
// 这里曾经整族是桩：handler 直接回 true/[]，前端点按钮"成功"但库里没有行——
// 所以每个动作都要**走 route() 再读回库/读回 settings键**核，不只看空手回执。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/services"
)

func toolsRoute(t *testing.T, hub *services.ServiceHub, action string, p map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(route(hub, action, p))
	if err != nil {
		t.Fatalf("marshal envelope: %v", err)
	}
	var env map[string]interface{}
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal envelope: %v", err)
	}
	if ok, _ := env["ok"].(bool); !ok {
		t.Fatalf("%s 失败：%v", action, env["error"])
	}
	return env
}

func TestReplaceHandlersMatchDartContract(t *testing.T) {
	dataDir := t.TempDir()
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", dataDir)
	hub := services.NewServiceHub()
	if err := hub.RegisterInfrastructure(); err != nil {
		t.Fatalf("register infrastructure: %v", err)
	}
	if err := hub.RegisterBusinessServices(); err != nil {
		t.Fatalf("register business services: %v", err)
	}
	if err := hub.StartAll(t.Context()); err != nil {
		t.Fatalf("start all: %v", err)
	}
	t.Cleanup(func() { _ = hub.StopAll() })

	// add：Dart 发 name/pattern/replacement/enabled，data 回裸数字 id。
	add := toolsRoute(t, hub, "replace_add", map[string]interface{}{
		"name": "去广告", "pattern": `广告\d+`, "replacement": "", "enabled": true,
	})
	if id, ok := add["data"].(float64); !ok || id != 1 {
		t.Fatalf("add 应回裸 id 1：%T %v", add["data"], add["data"])
	}

	// list：行形状对齐 Python list_rules（Dart 读 id/name/pattern/replacement/enabled）。
	list := toolsRoute(t, hub, "replace_list", map[string]interface{}{})
	rows, ok := list["data"].([]interface{})
	if !ok || len(rows) != 1 {
		t.Fatalf("list 应回一行：%T %v", list["data"], list["data"])
	}
	row, _ := rows[0].(map[string]interface{})
	assertExactKeys(t, "replace_list 行", row,
		"id", "name", "pattern", "replacement", "scope", "is_regex", "enabled")
	if row["name"] != "去广告" || row["pattern"] != `广告\d+` || row["enabled"] != true {
		t.Errorf("行值与落库不符：%v", row)
	}
	if row["scope"] != "content" || row["is_regex"] != true {
		t.Errorf("默认 scope/is_regex 应为 content/true：%v", row)
	}
	// 老键名 enable/global 是曾经的 Dart 读不到键（读 'enabled' 得 null →
	// 开关永远显示默认）——出参里必须没有它们。
	if _, has := row["enable"]; has {
		t.Error("行里还有旧键 'enable'，Dart 读的是 'enabled'")
	}

	// update：rule_id 是 **字符串**（Dart 的 .toString()），改完库与列表都要变。
	toolsRoute(t, hub, "replace_update", map[string]interface{}{
		"rule_id": "1", "name": "去广告改", "pattern": "x", "replacement": "y", "enabled": false,
	})
	list2 := toolsRoute(t, hub, "replace_list", map[string]interface{}{})
	row2, _ := list2["data"].([]interface{})[0].(map[string]interface{})
	if row2["name"] != "去广告改" || row2["pattern"] != "x" || row2["replacement"] != "y" || row2["enabled"] != false {
		t.Errorf("update 后行没变：%v", row2)
	}

	// toggle_global：全局开关落 settings 键（replace_rules 表没有这列）。
	tg := toolsRoute(t, hub, "replace_toggle_global", map[string]interface{}{"enabled": false})
	if tg["data"] != false {
		t.Errorf("toggle_global 应回 false：%v", tg["data"])
	}
	if got := hub.SettingsService().Get("replace.global.enabled", ""); got != "false" {
		t.Errorf("settings 键 replace.global.enabled 应落 false，得到 %q", got)
	}

	// delete：rule_id 字符串键；删空后再 list 回**空数组**不是 null。
	del := toolsRoute(t, hub, "replace_delete", map[string]interface{}{"rule_id": "1"})
	if del["data"] != true {
		t.Fatalf("delete 应回 true：%v", del["data"])
	}
	list3 := toolsRoute(t, hub, "replace_list", map[string]interface{}{})
	if rows3, ok := list3["data"].([]interface{}); !ok || len(rows3) != 0 {
		t.Errorf("删空后应回空数组：%T %v", list3["data"], list3["data"])
	}
}

func TestFontHandlersMatchDartContract(t *testing.T) {
	dataDir := t.TempDir()
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", dataDir)
	hub := services.NewServiceHub()
	if err := hub.RegisterInfrastructure(); err != nil {
		t.Fatalf("register infrastructure: %v", err)
	}
	if err := hub.RegisterBusinessServices(); err != nil {
		t.Fatalf("register business services: %v", err)
	}
	if err := hub.StartAll(t.Context()); err != nil {
		t.Fatalf("start all: %v", err)
	}
	t.Cleanup(func() { _ = hub.StopAll() })

	// add_user：Dart 发 file_path；字体文件拷进数据目录 fonts/ 并登记。
	srcDir := t.TempDir()
	srcPath := filepath.Join(srcDir, "demo.ttf")
	if err := os.WriteFile(srcPath, []byte("fake-font-bytes"), 0644); err != nil {
		t.Fatalf("造字体文件失败: %v", err)
	}
	add := toolsRoute(t, hub, "font_add_user", map[string]interface{}{"file_path": srcPath})
	if id, ok := add["data"].(float64); !ok || id != 1 {
		t.Fatalf("add_user 应回裸 id 1：%T %v", add["data"], add["data"])
	}
	// 落盘位置必须是 <数据目录>/fonts/（对齐 Python app_paths），不是 user-fonts。
	fontDir := filepath.Join(dataDir, "fonts")
	entries, err := os.ReadDir(fontDir)
	if err != nil || len(entries) != 1 {
		t.Fatalf("拷贝目标必须是 %s：err=%v entries=%v", fontDir, err, entries)
	}
	var name, family string
	if err := hub.StorageService().QueryRow("SELECT name, family FROM fonts WHERE id = 1").Scan(&name, &family); err != nil {
		t.Fatalf("读 fonts 行失败: %v", err)
	}
	if name != "demo" || family != "demo" {
		t.Errorf("name/family 应取文件名主干 demo：%q %q", name, family)
	}

	// select：落 settings 键 reader.font_family（阅读器排版读的就是这个键）。
	toolsRoute(t, hub, "font_select", map[string]interface{}{"family": "Fira Code"})
	if got := hub.SettingsService().Get("reader.font_family", ""); got != `"Fira Code"` {
		t.Errorf("settings 键 reader.font_family 应落 \"Fira Code\"，得到 %q", got)
	}

	// remove：font_id 字符串键；删过的再删回 false（Python 同样无错）。
	rem := toolsRoute(t, hub, "font_remove", map[string]interface{}{"font_id": "1"})
	if rem["data"] != true {
		t.Fatalf("首次删除应回 true：%v", rem["data"])
	}
	again := toolsRoute(t, hub, "font_remove", map[string]interface{}{"font_id": "1"})
	if again["data"] != false {
		t.Fatalf("重复删除应回 false：%v", again["data"])
	}
	var count int
	if err := hub.StorageService().QueryRow("SELECT COUNT(*) FROM fonts").Scan(&count); err != nil {
		t.Fatalf("数 fonts 行失败: %v", err)
	}
	if count != 0 {
		t.Errorf("删后应 0 行，得到 %d", count)
	}

	// scan_system：Windows 机器必有 C:\Windows\Fonts，非空就是正对照；
	// 其它平台只核形状（空数组也合法）。
	scan := toolsRoute(t, hub, "font_scan_system", map[string]interface{}{})
	fonts, ok := scan["data"].([]interface{})
	if !ok {
		t.Fatalf("scan_system 应回数组：%T", scan["data"])
	}
	if runtime.GOOS == "windows" && len(fonts) == 0 {
		t.Error("Windows 上 C:\\Windows\\Fonts 扫出 0 个字体 —— 扫描没接上")
	}
}
