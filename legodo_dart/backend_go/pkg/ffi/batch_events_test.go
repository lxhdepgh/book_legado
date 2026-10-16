package main

// 第一百一十六轮 M4：批量动作在 wire 上**只发一条** SourceChangedEvent。
//
// 这一族为什么要单独一层测（服务面那条在 internal/services）：断的是接线而不是本体。
// 原先 `source.batch_enable` 在服务之外逐 url 调 `Enable`，每次一条事件——全选 1371 条
// 批量禁用就是 1371 条，界面每收一条各重取一次整表（实测单价 7.4 ms）。改成整批一条之后，
// 只要 handler 上还挂着逐条那个口，服务层的计数测试照样全绿而线上照旧发 1371 条。
//
// 判据口径是 Dart 现读的那三格帧（`engine_poll` → PollBatch）与 `source_list` 的行键，
// 不是 Go 自己觉得顺手的样子。零外网：全程不碰真站点。

import (
	"fmt"
	"strings"
	"testing"
)

// changeFramesAfter 领走游标之后的那一批发给界面的帧，只数 SourceChangedEvent。
// 环是同步写入的（发布即落环），所以一发就够，不需要等。
func changeFramesAfter(t *testing.T, cursor float64) ([]map[string]interface{}, float64) {
	t.Helper()
	env := ffiRoute(t, "engine_poll", map[string]interface{}{"since": cursor})
	data, _ := env["data"].(map[string]interface{})
	next, _ := data["last_seq"].(float64)
	if data["lost"] == true {
		t.Fatalf("游标 %v 掉出环（lost=true）：这批事件被静默丢了", cursor)
	}
	var out []map[string]interface{}
	events, _ := data["events"].([]interface{})
	for _, raw := range events {
		entry, _ := raw.(map[string]interface{})
		if entry["name"] != "SourceChangedEvent" {
			continue
		}
		payload, _ := entry["payload"].(map[string]interface{})
		out = append(out, payload)
	}
	return out, next
}

// drainChanges 把环读到干净，返回读完时的游标：后面的计数只算动作自己那几发。
func drainChanges(t *testing.T) float64 {
	t.Helper()
	var cursor float64
	for round := 0; round < 50; round++ {
		_, next := changeFramesAfter(t, cursor)
		if next == cursor {
			return cursor
		}
		cursor = next
	}
	t.Fatalf("环读了 50 轮还没读干净")
	return cursor
}

// sourceRowsByName 走 source.list 读库，键是 url（批量之后要核对的是库里的形状，
// 不是缓存自己的说法）。
func sourceRowsByName(t *testing.T) map[string]map[string]interface{} {
	t.Helper()
	data, _ := envData(t, getHub(), "source_list", nil).([]interface{})
	out := map[string]map[string]interface{}{}
	for _, item := range data {
		row := strMap(item)
		url, _ := row["bookSourceUrl"].(string)
		if url == "" {
			t.Fatalf("source.list 里有行没有 bookSourceUrl：%v", row)
		}
		out[url] = row
	}
	return out
}

// seedSources 用真 `import_text` 落 N 条源（不用测试替身：替身会把被测的那条写路径换掉），
// 返回它们的 url。导入收尾那条事件由 drainChanges 清掉。
func seedSources(t *testing.T, n int) []string {
	t.Helper()
	urls := make([]string, 0, n)
	items := make([]string, 0, n)
	for i := 0; i < n; i++ {
		url := fmt.Sprintf("http://batch-%d.test", i)
		urls = append(urls, url)
		items = append(items, fmt.Sprintf(
			`{"bookSourceUrl":%q,"bookSourceName":"批量源%d","bookSourceGroup":"组甲%d","enabled":true}`, url, i, i))
	}
	out := importOutcome(t, getHub(), "import_text", map[string]interface{}{
		"text": "[" + strings.Join(items, ",") + "]", "kind": "book_source", "mode": "append",
	})
	if out["count"] != float64(n) {
		t.Fatalf("预置 %d 条源没落库：%v", n, out)
	}
	return urls
}

// urlsArg 批量动作的入参是 JSON 数组，Go 侧要的是 []interface{}。
func urlsArg(urls []string) []interface{} {
	out := make([]interface{}, 0, len(urls))
	for _, url := range urls {
		out = append(out, url)
	}
	return out
}

// TestBatchEnablePublishesOneChange 钉批量启停的形状：整批一条、SourceID 留空（整库级），
// 而且**三条真的都改了**——只发一条又什么都没写，比发 1371 条更糟。
func TestBatchEnablePublishesOneChange(t *testing.T) {
	ffiInit(t)
	urls := seedSources(t, 3)
	cursor := drainChanges(t)

	out := envData(t, getHub(), "source_batch_enable", map[string]interface{}{
		"urls": urlsArg(urls), "enabled": false,
	})
	counted, _ := strMap(out)["updated"]
	if counted != float64(3) {
		t.Fatalf("batch_enable 该回报改动 3 条：%v", out)
	}

	changes, _ := changeFramesAfter(t, cursor)
	if len(changes) != 1 {
		t.Fatalf("整批该恰好发一条 SourceChangedEvent，实发 %d：%v", len(changes), changes)
	}
	if changes[0]["source_id"] != "" || changes[0]["change_type"] != "enable" {
		t.Fatalf("批量那条该是整库级（source_id 空）+ change_type=enable：%v", changes[0])
	}

	rows := sourceRowsByName(t)
	for _, url := range urls {
		if rows[url]["enabled"] != false {
			t.Fatalf("%s 该已被批量停用：%v", url, rows[url])
		}
	}
}

// TestBatchEnableEmptySendsNothing 钉"没发生"：一条都没改成就不该发声
// （界面收到一条就重取一次整表，空批也发声等于白送一次 7 ms 的重取）。
func TestBatchEnableEmptySendsNothing(t *testing.T) {
	ffiInit(t)
	seedSources(t, 2)
	cursor := drainChanges(t)

	for _, urls := range [][]interface{}{nil, {}, {42}} {
		envData(t, getHub(), "source_batch_enable", map[string]interface{}{
			"urls": urls, "enabled": false,
		})
	}
	if changes, _ := changeFramesAfter(t, cursor); len(changes) != 0 {
		t.Fatalf("空批/非字符串元素不该发声，实发 %d：%v", len(changes), changes)
	}
	for url, row := range sourceRowsByName(t) {
		if row["enabled"] != true {
			t.Fatalf("空批却把 %s 改停了：%v", url, row)
		}
	}
}

// TestBatchDeletePublishesOneChange 同一条规矩用在删除上。
func TestBatchDeletePublishesOneChange(t *testing.T) {
	ffiInit(t)
	urls := seedSources(t, 3)
	cursor := drainChanges(t)

	out := envData(t, getHub(), "source_batch_delete", map[string]interface{}{
		"urls": urlsArg(urls[:2]),
	})
	if deleted, _ := strMap(out)["deleted"]; deleted != float64(2) {
		t.Fatalf("batch_delete 该回报删除 2 条：%v", out)
	}

	changes, _ := changeFramesAfter(t, cursor)
	if len(changes) != 1 || changes[0]["change_type"] != "delete" || changes[0]["source_id"] != "" {
		t.Fatalf("整批删除该恰好一条整库级事件：%v", changes)
	}
	rows := sourceRowsByName(t)
	if len(rows) != 1 || rows[urls[2]] == nil {
		t.Fatalf("该只剩第三条源：%v", rows)
	}
}

// TestSingleEnableStillNamesTheSource 是上面两条的对照臂：单条动作**没有**被顺手改成
// 整库级。两臂差量才是判据——只看批量那臂绿，说不清是合成生效还是发布口整个断了。
func TestSingleEnableStillNamesTheSource(t *testing.T) {
	ffiInit(t)
	urls := seedSources(t, 2)
	cursor := drainChanges(t)

	envData(t, getHub(), "source_enable", map[string]interface{}{
		"url": urls[0], "enabled": false,
	})

	changes, _ := changeFramesAfter(t, cursor)
	if len(changes) != 1 {
		t.Fatalf("单条启停该发一条：%d %v", len(changes), changes)
	}
	if changes[0]["source_id"] != urls[0] {
		t.Fatalf("单条那条该指名是哪一源变了：%v", changes[0])
	}
}

// TestSourceGroupTouchesOnlyThatSource 钉 `source_group` 的形状：wire 上带的是**一条**
// 源的 url。原先的 handler 把 p["url"] 整个不看，拿新分组刷掉**全表**每一条源
// （顺带逐条发事件）——改一条源的分组，从此所有源同名。
func TestSourceGroupTouchesOnlyThatSource(t *testing.T) {
	ffiInit(t)
	urls := seedSources(t, 3)
	cursor := drainChanges(t)

	envData(t, getHub(), "source_group", map[string]interface{}{
		"url": urls[0], "group": "只改这一条",
	})

	changes, _ := changeFramesAfter(t, cursor)
	if len(changes) != 1 || changes[0]["source_id"] != urls[0] {
		t.Fatalf("改一条源的分组该只发一条、只指名这一条：%v", changes)
	}
	rows := sourceRowsByName(t)
	if group, _ := rows[urls[0]]["bookSourceGroup"].(string); group != "只改这一条" {
		t.Fatalf("%s 的分组没改成：%v", urls[0], rows[urls[0]])
	}
	for _, other := range urls[1:] {
		if group, _ := rows[other]["bookSourceGroup"].(string); group == "只改这一条" {
			t.Fatalf("%s 的分组被顺手刷掉了——handler 又去看不到 url 了", other)
		}
	}
}

// TestSourceGroupMissingUrlFails 缺 url 当场报，不能退化成"刷全表"。
func TestSourceGroupMissingUrlFails(t *testing.T) {
	ffiInit(t)
	seedSources(t, 2)
	before := sourceRowsByName(t)

	msg := envError(t, getHub(), "source_group", map[string]interface{}{"group": "没有 url"})
	if !strings.Contains(msg, "url") {
		t.Fatalf("拒绝原因没提 url：%q", msg)
	}
	for url, row := range sourceRowsByName(t) {
		if fmt.Sprint(row["bookSourceGroup"]) != fmt.Sprint(before[url]["bookSourceGroup"]) {
			t.Fatalf("被拒的调用照样改了 %s 的分组：%v", url, row)
		}
	}
}
