package main

// 外观 / 更新 / 二维码三族的 FFI 接线测试。
//
// 为什么要在 route 这一层再测一遍：服务侧测的是"能力对不对"，这一层测的是
// **动作有没有接上能力**。这三族原本全是桩——theme.save 回 true（界面把刚存的
// 那一版折回默认档）、qr.generate 回显入参、update.check 回一句写死的
// "没有更新"。桩换成真服务后，只要 case 上还挂着旧的回车，服务测试照样全绿。
//
// 判据口径取自 Dart 侧现读的那一份：lib/core/rpc/ffi_engine_adapter.dart 的
// theme_save 发 {skin_id,mode,eink}、qr_generate 读 data['result']、
// update_check 把 data 当 UpdateInfo 或 null。

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/services"
)

func appearanceTestHub(t *testing.T) *services.ServiceHub {
	t.Helper()
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", t.TempDir())
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
	return hub
}

// envData 走一次动作并取回 data（要求 ok=true）。
func envData(t *testing.T, hub *services.ServiceHub, action string, p map[string]interface{}) interface{} {
	t.Helper()
	env := route(hub, action, p)
	if !env.Ok {
		t.Fatalf("%s 失败：%v", action, env.Error)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var back map[string]interface{}
	if err := json.Unmarshal(raw, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return back["data"]
}

// envError 走一次动作并要求它失败，回错误文本。
func envError(t *testing.T, hub *services.ServiceHub, action string, p map[string]interface{}) string {
	t.Helper()
	env := route(hub, action, p)
	if env.Ok {
		raw, _ := json.Marshal(env.Data)
		t.Fatalf("%s 本该失败，却回了 ok=true，data=%s", action, raw)
	}
	return env.Error
}

func strMap(v interface{}) map[string]interface{} {
	m, ok := v.(map[string]interface{})
	if !ok {
		return nil
	}
	out := make(map[string]interface{}, len(m))
	for k, val := range m {
		out[k] = val
	}
	return out
}

// settingRaw 用 settings.read 那个口读回一格。Go 侧那一格存的是 **JSON 文本**
// （SettingsService.ReadKeys 原样回字符串），所以这里拿到的是字符串，由调用方解。
func settingRaw(t *testing.T, hub *services.ServiceHub, key string) string {
	t.Helper()
	data := strMap(envData(t, hub, "settings_read", map[string]interface{}{
		"keys": []interface{}{key},
	}))
	raw, ok := data[key].(string)
	if !ok || raw == "" {
		t.Fatalf("settings.read 没回 %s 这一格：%v", key, data)
	}
	return raw
}

// TestThemeFFIRoundTrip：preferences 回三格、save 回**归一后的三格**而不是 true、
// 缺省的那一格跨 FFI 也保持原值，并真的落到 appearance.snapshot。
func TestThemeFFIRoundTrip(t *testing.T) {
	hub := appearanceTestHub(t)

	first := strMap(envData(t, hub, "theme_preferences", nil))
	if first == nil {
		t.Fatalf("theme.preferences 该回一个对象，得 %T", first)
	}
	if len(first) != 3 {
		t.Fatalf("首屏回包不是三格（旧臆造形状有十格）：%v", first)
	}
	if first["skin_id"] != "classic_blue" || first["mode"] != "system" || first["eink"] != false {
		t.Fatalf("首屏默认档不对：%v", first)
	}

	saved := strMap(envData(t, hub, "theme_save", map[string]interface{}{
		"skin_id": "ink_gray", "mode": "dark", "eink": false,
	}))
	if saved == nil || saved["skin_id"] != "ink_gray" || saved["mode"] != "dark" {
		t.Fatalf("theme.save 该回归一后的三格（回 true 会把界面折回默认档）：%v", saved)
	}

	// 只发 eink 那一格（JSON null = 这一格不改）。
	merged := strMap(envData(t, hub, "theme_save", map[string]interface{}{
		"skin_id": nil, "mode": nil, "eink": true,
	}))
	if merged["mode"] != "dark" || merged["skin_id"] != "ink_gray" || merged["eink"] != true {
		t.Fatalf("跨 FFI 的缺省格被改写：%v", merged)
	}

	// 表外的皮肤折回默认，而不是报错（schema theme.save.json 明写了这一条）。
	folded := strMap(envData(t, hub, "theme_save", map[string]interface{}{"skin_id": "no_such_skin"}))
	if folded["skin_id"] != "classic_blue" {
		t.Fatalf("表外皮肤没折回默认：%v", folded)
	}

	// 落库走的是同一个 settings 口：键名字面量 appearance.snapshot，值是三格 JSON。
	snap := strMap(jsonValue(t, settingRaw(t, hub, "appearance.snapshot")))
	if snap == nil {
		t.Fatalf("appearance.snapshot 那一格不是对象")
	}
	if snap["skin_id"] != "classic_blue" || snap["eink"] != true || snap["mode"] != "dark" {
		t.Fatalf("库里那格与回包不一致：%v", snap)
	}
}

// jsonValue 把 settings 里那一格 JSON 文本解回 Go 值。
func jsonValue(t *testing.T, raw string) interface{} {
	t.Helper()
	var out interface{}
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		t.Fatalf("解 JSON 失败 %q：%v", raw, err)
	}
	return out
}

// TestUpdateCheckFFIWithoutChannel：没配更新源时回错，不是一句写死的"没有更新"。
func TestUpdateCheckFFIWithoutChannel(t *testing.T) {
	hub := appearanceTestHub(t)

	errText := envError(t, hub, "update_check", nil)
	if !strings.Contains(errText, "update.api_url") {
		t.Fatalf("错误该指出缺的是哪一格：%q", errText)
	}

	if got := envError(t, hub, "update_ignore", nil); !strings.Contains(got, "version") {
		t.Fatalf("缺 version 时该拒绝：%q", got)
	}
	envData(t, hub, "update_ignore", map[string]interface{}{"version": "9.9.9"})

	// 忽略的版本落在与 flet 侧同一个键上（旧实现写成 update.ignore_version，少个 d）。
	if got := jsonValue(t, settingRaw(t, hub, "update.ignored_version")); got != "9.9.9" {
		t.Fatalf("忽略的版本没落库（读回 %v）", got)
	}

	// 配了源但拉不动：必须回错。回 data=null 会被界面读成"已是最新版本"，
	// 那是把一次失败冒充成一次结论。地址是回环上一个没人听的端口，不出本机。
	envData(t, hub, "settings_write", map[string]interface{}{
		"key": "update.api_url", "value": "http://127.0.0.1:1/latest.json",
	})
	if got := envError(t, hub, "update_check", nil); !strings.Contains(got, "更新检查失败") {
		t.Fatalf("拉取失败该带原因回错：%q", got)
	}
}

// TestQRGenerateFFIReturnsDecodablePNG：回的是能解出 PNG 魔数的 base64，不是入参回显。
func TestQRGenerateFFIReturnsDecodablePNG(t *testing.T) {
	hub := appearanceTestHub(t)

	data := strMap(envData(t, hub, "qr_generate", map[string]interface{}{"text": "https://loopback.invalid.test/source.json"}))
	encoded, _ := data["result"].(string)
	if encoded == "" {
		t.Fatalf("qr.generate 没回 result：%v", data)
	}
	png, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		t.Fatalf("result 不是合法 base64：%v", err)
	}
	if len(png) < 8 {
		t.Fatalf("解出来的太短，不是一张 PNG：%d 字节", len(png))
	}
	if string(png[1:4]) != "PNG" {
		t.Fatalf("解出来的不是 PNG（前 8 字节 % x）", png[:8])
	}

	if got := envError(t, hub, "qr_generate", map[string]interface{}{"text": ""}); !strings.Contains(got, "不能为空") {
		t.Fatalf("空内容该拒绝并说清：%q", got)
	}
}

// TestQRDecodeFFIIsExplicitUnimplemented：识别未实现要说"未实现"，
// 回空串会在界面上长成"这张图里没有二维码"。
func TestQRDecodeFFIIsExplicitUnimplemented(t *testing.T) {
	hub := appearanceTestHub(t)
	if got := envError(t, hub, "qr_decode", map[string]interface{}{"image_data": "iVBORw0KGgo="}); !strings.Contains(got, "未实现") {
		t.Fatalf("qr.decode 的错误文本该写明未实现：%q", got)
	}
}
