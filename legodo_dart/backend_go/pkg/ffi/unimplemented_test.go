package main

// 「这条能力在本引擎里没有实现」这句话自身的护栏。
//
// 这一族 case 原先一律回成功：导出回 `{path:"",success:true}`、WebDAV 备份回 `true`、
// 朗读回 `true`、书架导入回 `{imported:0}`、`source.audit` 起一个协程给每条启用的
// 书源把 `respond_time` 覆成 ~0（它一次网都没联）再回 `{started:true}`。
// 界面读到的是"做完了"，磁盘/远端/库里什么都没发生——而且那一发还在往库里写假数据。
//
// 桩换成显式错误之后，这一支要钉的五件事：
//
//  1. **两种拼法拿同一句话**：`webbridge.start` 与 `webbridge_start` 都要红得一样。
//     只测下划线那一遍的话，点号拼法漏登记会一路绿到线上（route 的 case 两种都认）。
//  2. **表里的键与线上会发的那批一一对得上**：多一格（登记了没人发的名字）、
//     少一格（发过来掉回 default 那句英文 `unimplemented:`）都红。
//  3. **不许带回载荷**：`data` 那格必须空——`{success:true}` 之类不能再回来。
//  4. **每一句都带「未实现：」**：这是界面与日志认的标记，措辞可以改，标记不能丢。
//  5. **`source.audit` 不动库**（钉的是"没发生"）：那一发过去写的是假的响应时间，
//     所以先把那一行钉成 1234 再发它，读回来还得是 1234。用 0 起测分不出"没写"和"写了假的"。
//
// 正对照在最后：同一只 hub 上 `source_list` 照旧回 ok，`import_text` 照旧落库。
// 没有这一条，"全都红了"和"该红的红了"是同一个读数。

import (
	"encoding/json"
	"sort"
	"strings"
	"testing"
	"time"
)

// unimplementedWire 抄自 route 的 case 名单（`dot` 为空 = 那一发没有点号拼法）。
var unimplementedWire = []struct{ snake, dot string }{
	{"browser_open_page", "browser.open_page"},
	{"browser_navigate", "browser.navigate"},
	{"browser_close", "browser.close"},
	{"webbridge_start", "webbridge.start"},
	{"webbridge_stop", "webbridge.stop"},
	{"webbridge_status", "webbridge.status"},
	{"export_epub", "export.epub"},
	{"export_txt", "export.txt"},
	{"webdav_backup", "webdav.backup"},
	{"webdav_restore", "webdav.restore"},
	{"webdav_list_backups", "webdav.list_backups"},
	{"read_aloud_start", "read_aloud.start"},
	{"read_aloud_pause", "read_aloud.pause"},
	{"read_aloud_stop", "read_aloud.stop"},
	{"read_aloud_progress", "read_aloud.progress"},
	{"bookshelf_import", ""},
	{"source_audit", "source.audit"},
}

func TestUnimplementedBothSpellingsFailWithTheSameSentence(t *testing.T) {
	hub := appearanceTestHub(t)
	for _, entry := range unimplementedWire {
		want, ok := unimplementedActions[entry.snake]
		if !ok {
			t.Fatalf("%s 在线上会发，表里却没有这一格：它掉回 default 的英文那句", entry.snake)
		}
		for _, name := range []string{entry.snake, entry.dot} {
			if name == "" {
				continue
			}
			env := route(hub, name, map[string]interface{}{})
			if env.Ok {
				raw, _ := json.Marshal(env)
				t.Fatalf("%s 本该回错误，却回了 ok=true：%s", name, raw)
			}
			if env.Error != want {
				t.Fatalf("%s 的句子与表里不一致（掉回 default 或两种拼法各写一份）：\n got %q\nwant %q",
					name, env.Error, want)
			}
			if env.Data != nil {
				t.Fatalf("%s 未实现还带回载荷 %v：界面读的就是这一格", name, env.Data)
			}
		}
	}
}

func TestUnimplementedTableHasExactlyTheWireNames(t *testing.T) {
	// 并集比对，两个方向分开数：表多一格与名单多一格的读数不一样，折成一个长度比对会看不出错在哪。
	var table []string
	for key := range unimplementedActions {
		table = append(table, key)
	}
	sort.Strings(table)
	var wire []string
	for _, entry := range unimplementedWire {
		wire = append(wire, entry.snake)
	}
	sort.Strings(wire)
	if got, want := strings.Join(table, "|"), strings.Join(wire, "|"); got != want {
		t.Fatalf("表与名单不对齐：\n表   %s\n名单 %s", got, want)
	}
	for key, msg := range unimplementedActions {
		if !strings.Contains(msg, "未实现：") {
			t.Fatalf("%s 的句子没有「未实现：」这个标记：%q", key, msg)
		}
	}
}

// TestSourceAuditWritesNothingToTheLibrary 钉的是删除那个协程这件事。
// 判据不取"它报错了"（那由上一条测试钉），取"库里那一行没被动过"。
//
// 钉子必须从 `Save` 这一格进：那个协程写的就是 `svc.Save(&src)`，它同时落库和覆缓存。
// 我先前用裸 SQL `UPDATE` 钉，结果缓存那格读回的是导入时那个 0——分不出"没发生"和
// "绕过了缓存直接写库"，红在了无关的地方。走 `Save` 之后两层都对得上，一句「钉成 1234」
// 就同时钉住了库和界面读的那一份。
func TestSourceAuditWritesNothingToTheLibrary(t *testing.T) {
	hub := appearanceTestHub(t)
	const url = "http://audit-untouched.test"
	svc := hub.SourceService()

	outcome := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": `[{"bookSourceUrl":"` + url + `","bookSourceName":"别改我","enabled":true}]`,
		"kind": "book_source",
	})
	if outcome["count"] != float64(1) {
		t.Fatalf("对照组（导入）没落库：%v", outcome)
	}
	src, ok := svc.Get(url)
	if !ok {
		t.Fatalf("导入的那一条不在缓存里，钉子没处下")
	}
	// 已知的非 0：导入那一条本身带的是 0（源里没写 respondTime），
	// 拿 0 起测就与"审计写了假的 ~0"同形。
	src.RespondTime = 1234
	if err := svc.Save(src); err != nil {
		t.Fatalf("钉 respond_time 失败：%v", err)
	}

	errMsg := envError(t, hub, "source_audit", map[string]interface{}{
		"source": map[string]interface{}{"bookSourceUrl": url},
	})
	if !strings.Contains(errMsg, "不会改动任何一条书源") {
		t.Fatalf("source.audit 那句没交代「不会改动库里」：%q", errMsg)
	}

	// 那一发过去起的是后台协程：等一拍再读，"没发生"才是读数而不是抢跑赢了。
	// 它原先一次网都不联，50ms 足够它写完再回来。
	time.Sleep(50 * time.Millisecond)

	var got int64
	if err := hub.StorageService().QueryRow(
		`SELECT respond_time FROM book_sources WHERE url = ?`, url).Scan(&got); err != nil {
		t.Fatalf("读回那一行失败：%v", err)
	}
	if got != 1234 {
		t.Fatalf("source.audit 又往库里写了假的响应时间：%d（钉的是 1234）", got)
	}
	for _, cached := range svc.List() {
		if cached.BookSourceURL == url && cached.RespondTime != 1234 {
			t.Fatalf("source.audit 又覆了界面读的那一份缓存：%d（钉的是 1234）", cached.RespondTime)
		}
	}
}

func TestUnimplementedLeavesRealActionsAlone(t *testing.T) {
	hub := appearanceTestHub(t)
	if _, ok := unimplementedActions["source_list"]; ok {
		t.Fatal("source_list 是实现了的，不许进未实现表")
	}
	// 正对照：同一只 hub 上这两个口照常工作。（不判 `data` 那一格：空库回的是空切片，
	// 而信封上写着 `omitempty`，空切片会被整个抹掉——那不等于没回话。）
	if env := route(hub, "source_list", nil); !env.Ok {
		t.Fatalf("source_list 被这张表一起带红了：error=%q", env.Error)
	}
	out := importOutcome(t, hub, "import_text", map[string]interface{}{
		"text": `[{"bookSourceUrl":"http://still-real.test"}]`,
	})
	if out["count"] != float64(1) {
		t.Fatalf("import_text 被这张表一起带红了：%v", out)
	}
}
