package main

// 导入这一族的 FFI 接线测试。
//
// 服务面测的是"落库对不对"，这一层只钉两件事：
//   1. **动作接上了能力**——`import_url` 那一格原本直接 `return okResult({"imported":0})`，
//      一个字节都不拉；桩换成真服务后，只要 case 上还挂着那句回车，服务测试照样全绿。
//   2. **回包形状是 Dart 现读的那五格** `{kind,count,errors,warnings,placeholder}`
//      （`lib/core/rpc/ffi_engine_adapter.dart` 的 importText/importUrl 逐格读这五个键，
//      键名对不上时 `count` 会静默读成 0，界面就报"导入了 0 条"）。

import (
	"strings"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/services"
)

// importOutcome 把回包按 Dart 那五格摊平，顺手钉住"没有第六格、也不少格"。
func importOutcome(t *testing.T, hub *services.ServiceHub, action string, p map[string]interface{}) map[string]interface{} {
	t.Helper()
	out := strMap(envData(t, hub, action, p))
	if out == nil {
		t.Fatalf("%s 该回一个对象：%v", action, out)
	}
	want := []string{"count", "errors", "kind", "placeholder", "warnings"}
	if len(out) != len(want) {
		t.Fatalf("%s 回包格数不是 5（Dart 逐格读这五个键）：%v", action, keysOf(out))
	}
	for _, key := range want {
		if _, ok := out[key]; !ok {
			t.Fatalf("%s 回包缺 %s：%v", action, key, keysOf(out))
		}
	}
	return out
}

func keysOf(m map[string]interface{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// sourceURLs 走 source.list 那个口读库里的源，用作"报了 N 条就得真在库里"的对照。
func sourceURLs(t *testing.T, hub *services.ServiceHub) []string {
	t.Helper()
	data, _ := envData(t, hub, "source_list", nil).([]interface{})
	urls := make([]string, 0, len(data))
	for _, item := range data {
		row := strMap(item)
		url, _ := row["bookSourceUrl"].(string)
		if url == "" {
			t.Fatalf("source.list 里有行没有 bookSourceUrl：%v", row)
		}
		urls = append(urls, url)
	}
	return urls
}

func TestImportTextFFIWritesThroughToSourceList(t *testing.T) {
	hub := appearanceTestHub(t)
	if rows := sourceURLs(t, hub); len(rows) != 0 {
		t.Fatalf("新库起手不该有书源：%v", rows)
	}

	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": `[{"bookSourceUrl":"http://ffi-import.test","bookSourceName":"FFI 导的源"}]`,
		"kind": "book_source",
		"mode": "append",
	})
	if out["count"] != float64(1) {
		t.Fatalf("count 该是 1：%v", out)
	}
	if out["placeholder"] != false {
		t.Fatalf("书源导入不是占位：%v", out)
	}
	if errs, _ := out["errors"].([]interface{}); len(errs) != 0 {
		t.Fatalf("干净导入带回错误：%v", errs)
	}
	urls := sourceURLs(t, hub)
	if len(urls) != 1 || urls[0] != "http://ffi-import.test" {
		t.Fatalf("回包报了 1 条，source.list 里却是 %v", urls)
	}
}

// TestImportKindAndModeDefaults 钉 FFI 这一层的缺省：kind 缺省按唯一有落点的那一类
// （Python 控制器的 IMPORT_KIND），mode 缺省 append（Python 门面签名）。
// 缺键不能掉进"未知资源类型"或"未知导入模式"——那是参数没读到，不是用户说错。
func TestImportKindAndModeDefaults(t *testing.T) {
	hub := appearanceTestHub(t)
	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": `{"bookSourceUrl":"http://defaults.test","bookSourceName":"只给文本"}`,
	})
	if out["kind"] != "book_source" {
		t.Fatalf("kind 缺省没落到 book_source：%v", out)
	}
	if out["count"] != float64(1) {
		t.Fatalf("mode 缺省该按 append 导：%v", out)
	}
	if urls := sourceURLs(t, hub); len(urls) != 1 {
		t.Fatalf("缺省参数的导入没落库：%v", urls)
	}
}

func TestImportPendingKindIsPlaceholderOverFFI(t *testing.T) {
	hub := appearanceTestHub(t)
	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": `[{"bookSourceUrl":"http://never.test"}]`,
		"kind": "rss",
	})
	if out["placeholder"] != true {
		t.Fatalf("rss 该回占位：%v", out)
	}
	if out["count"] != float64(0) {
		t.Fatalf("占位类型不该报条数：%v", out)
	}
	warnings, _ := out["warnings"].([]interface{})
	if len(warnings) != 1 || !strings.Contains(asString(warnings[0]), "RSS 源请在") {
		t.Fatalf("占位该带一句去哪一页加：%v", warnings)
	}
	if urls := sourceURLs(t, hub); len(urls) != 0 {
		t.Fatalf("占位类型却写了库：%v", urls)
	}
}

func TestImportUnknownKindIsErrorOverFFI(t *testing.T) {
	hub := appearanceTestHub(t)
	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": "[]", "kind": "booksource",
	})
	if out["placeholder"] != false {
		t.Fatalf("拼错的类型不该算占位：%v", out)
	}
	errs, _ := out["errors"].([]interface{})
	if len(errs) != 1 || !strings.Contains(asString(errs[0]), "未知资源类型") {
		t.Fatalf("拼错的类型该报一句：%v", errs)
	}
}

// TestImportUrlIsNotAStub：这一格原本是 `okResult({"imported":0})`——不拉、不写、不报错。
// 走一个关着的回环端口，要求听到"拉取失败"，而不是那句好看的回车。
func TestImportUrlIsNotAStub(t *testing.T) {
	hub := appearanceTestHub(t)
	out := importOutcome(t, hub, "import_url", map[string]interface{}{
		"url":  "http://127.0.0.1:1/bookSource.json",
		"kind": "book_source",
	})
	errs, _ := out["errors"].([]interface{})
	if len(errs) != 1 || !strings.HasPrefix(asString(errs[0]), "拉取失败：") {
		t.Fatalf("拉不到该说拉取失败：%v", out)
	}
	if out["count"] != float64(0) {
		t.Fatalf("没拉到东西却报了条数：%v", out)
	}
}

func TestImportUrlRequiresUrl(t *testing.T) {
	hub := appearanceTestHub(t)
	for _, p := range []map[string]interface{}{
		{"kind": "book_source"},
		{"url": "", "kind": "book_source"},
		{"url": "   ", "kind": "book_source"},
	} {
		got := envError(t, hub, "import_url", p)
		if !strings.Contains(got, "url") {
			t.Fatalf("缺 url 时该指出缺哪一格：%q", got)
		}
	}
}

// TestImportEmptyTextIsNotSilent 空文本（典型：文件读出来是空的）要说话，
// 不能回一个 count=0、errors=[] 的"什么都没发生"。
func TestImportEmptyTextIsNotSilent(t *testing.T) {
	hub := appearanceTestHub(t)
	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": "", "kind": "book_source",
	})
	errs, _ := out["errors"].([]interface{})
	if len(errs) != 1 || !strings.Contains(asString(errs[0]), "JSON 解析失败") {
		t.Fatalf("空文本该报解析失败：%v", out)
	}
}

func asString(v interface{}) string {
	s, _ := v.(string)
	return s
}
