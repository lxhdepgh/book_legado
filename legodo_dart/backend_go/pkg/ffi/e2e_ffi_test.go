package main

// P8 的 FFI 面端到端：真 `Initialize` → 假源（127.0.0.1 httptest）过真规则引擎 →
// 事件经 `EventRing` 由 `route("engine_poll")` 按游标领走。判据原文是
// 「Initialize → search_start → poll SearchFinishedEvent」，换源轮同理。
//
// 服务层已各有一条 E2E（search_model_test / switch_service_test），但那是总线直连。
// Dart 的真路径还多两截接线：Initialize 把环挂上总线、engine_poll 从环里领——
// 这两截断了，服务层那两条照样绿而界面上一帧都收不到（记忆
// `refactor-regression-orphan-methods`：断的是接线，不是功能本体）。
//
// 断言口径是 Dart 侧解析器吃的那些键，不是 Go 自己觉得顺手的样子：
//   - 受理回包 {started, action}（JobAccepted.fromJson，ffi_engine_adapter.dart:498）；
//   - 轮询帧 {events, last_seq, lost}（ffi_engine_adapter.dart:237 的 PollBatch）；
//   - SearchFinishedEvent 载荷 search_id（**字符串**——Dart 按字符串比本轮）/items/finished；
//   - items 行 = BookSnapshot 35 键（BookRow.fromJson 读 name/author/intro/url/
//     origin/origin_name/cover_url，find_books_notifier.dart:325）；
//   - 换源候选行 {source_url, source_name, book}（SwitchCandidate.fromMap，
//     source_switch_models.dart:41）。
//
// 零外网：假源全程 127.0.0.1。

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const (
	ffiFakeSourceURL  = "https://ffi-e2e-source.test"
	ffiFakeSourceName = "E2E源"
)

// bookSnapshotKeys 是 BookSnapshot 的 35 键（对齐 golden `browse.books` /
// `search.start` 的 field_names）。两个方向都钉：多一格漏一格都红。
var bookSnapshotKeys = []string{
	"name", "author", "intro", "url", "cover_url", "toc_url", "kind",
	"last_chapter", "latest_chapter_title", "word_count", "download_urls",
	"origin", "origin_name", "type", "total_chapter_num", "dur_chapter_index",
	"dur_chapter_title", "can_update", "custom_intro", "custom_tag",
	"custom_cover_url", "charset", "group", "latest_chapter_time",
	"last_check_time", "last_check_count", "dur_chapter_pos", "dur_chapter_time",
	"order", "origin_order", "read_config", "info_html", "toc_html",
	"variables", "origins",
}

// ffiInit 走**真 Initialize**（含 StartAll 与把 EventRing 挂上总线的两截接线）。
// 与 bookmark_handlers_test 同款：TempDir 上的 legado.db 不显式关库，Windows 上删不掉。
func ffiInit(t *testing.T) {
	t.Helper()
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", t.TempDir())
	Initialize()
	h := getHub()
	if h == nil {
		t.Fatal("Initialize 没装上 hub")
	}
	t.Cleanup(func() {
		Cleanup()
		if s := h.StorageService(); s != nil {
			_ = s.Close()
		}
	})
}

// ffiRoute 把 route() 的回包拍成 Dart 拿到的那种 map（先 JSON 往返，与 Call 同一形状）。
func ffiRoute(t *testing.T, action string, p map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(route(getHub(), action, p))
	if err != nil {
		t.Fatalf("marshal %s envelope: %v", action, err)
	}
	var env map[string]interface{}
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal %s envelope: %v", action, err)
	}
	if ok, _ := env["ok"].(bool); !ok {
		t.Fatalf("%s 失败：%v", action, env["error"])
	}
	return env
}

// ffiPoll 以游标轮询 engine_poll 直到 want 命中；帧形状每轮都核
// （Dart 的 PollBatch 解码靠这三格，缺一格那条流就断）。
func ffiPoll(t *testing.T, want func(name string, payload map[string]interface{}) bool) map[string]interface{} {
	t.Helper()
	var cursor float64
	seen := make([]string, 0, 16)
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		env := ffiRoute(t, "engine_poll", map[string]interface{}{"since": cursor})
		data, _ := env["data"].(map[string]interface{})
		assertExactKeys(t, "engine_poll 帧", data, "events", "last_seq", "lost")
		if data["lost"] == true {
			t.Fatalf("游标 %v 掉出环（lost=true）：事件被静默丢了", cursor)
		}
		cursor, _ = data["last_seq"].(float64)
		events, _ := data["events"].([]interface{})
		for _, raw := range events {
			entry, _ := raw.(map[string]interface{})
			name, _ := entry["name"].(string)
			payload, _ := entry["payload"].(map[string]interface{})
			seen = append(seen, name)
			if want(name, payload) {
				return payload
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("10s 内没等到目标事件，环里见过：%v", seen)
	return nil
}

// newFFIFakeSourceSite 假书源主站：一行书「斗破苍穹」，搜索/换源共用。
func newFFIFakeSourceSite(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<html><body><div class="book">
			<a class="name" href="/book/1">斗破苍穹</a>
			<span class="author">天蚕土豆</span></div></body></html>`)
	}))
}

// fakeFFISource 对齐 Dart 发出 source.save 的载荷（ffi_engine_adapter.dart:367
// 的 `{'source': sourceJson}`，sourceJson 就是书源本体的 camelCase JSON）。
func fakeFFISource(searchBase string) map[string]interface{} {
	return map[string]interface{}{
		"bookSourceUrl":  ffiFakeSourceURL,
		"bookSourceName": ffiFakeSourceName,
		"enabled":        true,
		"searchUrl":      searchBase + "/search?key={{key}}&page={{page}}",
		"ruleSearch": map[string]interface{}{
			"bookList": ".book",
			"name":     "a.name@text",
			"author":   "span.author@text",
			"bookUrl":  "a.name@href",
		},
	}
}

// TestSearchStartEndToEndViaFFI：找书一整轮——受理回包 → 逐源增量（finished=false）
// → 收口（finished=true，items 非空且行是 35 键快照）。
func TestSearchStartEndToEndViaFFI(t *testing.T) {
	server := newFFIFakeSourceSite(t)
	defer server.Close()
	ffiInit(t)

	saved := ffiRoute(t, "source_save", map[string]interface{}{"source": fakeFFISource(server.URL)})
	if data, _ := saved["data"].(map[string]interface{}); data["saved"] != true {
		t.Fatalf("source.save 应回 saved=true：%v", saved["data"])
	}

	accepted := ffiRoute(t, "search_start", map[string]interface{}{
		"keyword": "斗破", "scope": "", "search_id": float64(4242),
	})
	job, _ := accepted["data"].(map[string]interface{})
	assertExactKeys(t, "search.start 受理回包", job, "started", "action")
	if job["started"] != true || job["action"] != "search.start" {
		t.Fatalf("受理回包形状错：%v", job)
	}

	partials := 0
	final := ffiPoll(t, func(name string, payload map[string]interface{}) bool {
		if name != "SearchFinishedEvent" {
			return false
		}
		if payload["search_id"] != "4242" {
			return false
		}
		if finished, _ := payload["finished"].(bool); !finished {
			partials++
			return false
		}
		return true
	})
	if partials < 1 {
		t.Error("一条逐源增量（finished=false）都没见到：界面上的实时条数就没来源了")
	}

	items, _ := final["items"].([]interface{})
	if len(items) == 0 {
		t.Fatalf("收口事件 items 不该空：%v", final)
	}
	row, _ := items[0].(map[string]interface{})
	assertExactKeys(t, "搜索行", row, bookSnapshotKeys...)
	if row["name"] != "斗破苍穹" || row["author"] != "天蚕土豆" {
		t.Errorf("书名/作者不符：%v / %v", row["name"], row["author"])
	}
	if row["origin"] != ffiFakeSourceURL || row["origin_name"] != ffiFakeSourceName {
		t.Errorf("origin/origin_name 不是那条源：%v / %v（入架按 origin 找回书源）",
			row["origin"], row["origin_name"])
	}
	if url, _ := row["url"].(string); !strings.HasSuffix(url, "/book/1") {
		t.Errorf("url 应是假源那条书链：%v", row["url"])
	}
}

// TestSwitchScanEndToEndViaFFI：换源轮——受理回包 → 收口事件带候选行
// {source_url, source_name, book}，候选书行同样是 35 键快照。
func TestSwitchScanEndToEndViaFFI(t *testing.T) {
	server := newFFIFakeSourceSite(t)
	defer server.Close()
	ffiInit(t)

	ffiRoute(t, "source_save", map[string]interface{}{"source": fakeFFISource(server.URL)})

	accepted := ffiRoute(t, "switch_scan", map[string]interface{}{
		"book": map[string]interface{}{
			"name": "斗破", "author": "天蚕土豆",
			"source_url": "https://own-site.test",
			"url":        "https://own-site.test/book/1",
		},
		"switch_id": float64(5555), "keyword": "", "group": "",
	})
	job, _ := accepted["data"].(map[string]interface{})
	assertExactKeys(t, "switch.scan 受理回包", job, "started", "action")
	if job["started"] != true || job["action"] != "switch.scan" {
		t.Fatalf("受理回包形状错：%v", job)
	}

	final := ffiPoll(t, func(name string, payload map[string]interface{}) bool {
		return name == "SourceSwitchFinishedEvent" && payload["switch_id"] == "5555"
	})
	if final["cancelled"] != false || final["error"] != "" {
		t.Fatalf("扫完的收口形状错：%v", final)
	}
	candidates, _ := final["candidates"].([]interface{})
	if len(candidates) == 0 {
		t.Fatalf("假源回了同名书，候选不该空：%v", final)
	}
	cand, _ := candidates[0].(map[string]interface{})
	assertExactKeys(t, "候选行", cand, "source_url", "source_name", "book")
	if cand["source_url"] != ffiFakeSourceURL || cand["source_name"] != ffiFakeSourceName {
		t.Errorf("候选的源不符：%v / %v", cand["source_url"], cand["source_name"])
	}
	book, _ := cand["book"].(map[string]interface{})
	assertExactKeys(t, "候选书行", book, bookSnapshotKeys...)
	if book["name"] != "斗破苍穹" {
		t.Errorf("候选书名不符：%v", book["name"])
	}
}
