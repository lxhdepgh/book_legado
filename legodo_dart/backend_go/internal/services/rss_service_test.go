package services

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sort"
	"sync"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
)

// ---------------------------------------------------------------------------
// 装配：临时库 + 事件总线 + 回环假 feed 服务器
// ---------------------------------------------------------------------------

func newRssTestService(t *testing.T) (*RSSService, *core.StorageService, *core.EventBus) {
	t.Helper()
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储服务失败: %v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })
	bus := core.NewEventBus()
	return NewRSSService(storage, bus, httpclient.NewClient()), storage, bus
}

// rssTestFeed 一份 RSS 2.0 语料：两条，一条带转义 HTML 正文、一条纯文本。
const rssTestFeed = `<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>测试 feed</title>
  <item>
    <title>带标签的第一条</title>
    <link>http://site.test/post/1</link>
    <author>作者甲</author>
    <pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate>
    <description>&lt;p&gt;第一段&lt;/p&gt;&lt;p&gt;&lt;strong&gt;加粗&lt;/strong&gt;&lt;/p&gt;&lt;p&gt;第二段&lt;/p&gt;</description>
  </item>
  <item>
    <title>纯文本的第二条</title>
    <link>http://site.test/post/2</link>
    <author>作者乙</author>
    <pubDate>Tue, 06 Oct 2026 09:00:00 GMT</pubDate>
    <description>没有标签的第二条</description>
  </item>
</channel></rss>`

// rssTestAtom 一份 Atom 语料：rel=self 的 link 要跳过，封面取 media:thumbnail。
const rssTestAtom = `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>测试 Atom</title>
  <entry>
    <title>Atom 第一条</title>
    <link rel="self" href="http://site.test/atom/1-self"/>
    <link rel="alternate" href="http://site.test/atom/1"/>
    <author><name>作者丙</name></author>
    <published>2026-10-05T09:00:00Z</published>
    <summary type="html">Atom 正文一</summary>
    <media:thumbnail url="http://site.test/atom/1.png"/>
  </entry>
  <entry>
    <title>Atom 第二条</title>
    <link href="http://site.test/atom/2"/>
    <updated>2026-10-06T09:00:00Z</updated>
    <summary>Atom 正文二</summary>
  </entry>
</feed>`

func newFeedSite(t *testing.T, feeds map[string]string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, ok := feeds[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/xml")
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return server
}

// addRssTestSource 建一条源并回它的 id（= url）。
func addRssTestSource(t *testing.T, svc *RSSService, baseURL, path, name string) string {
	t.Helper()
	id, err := svc.AddSource(name, baseURL+path, "测试订阅")
	if err != nil {
		t.Fatalf("加源失败: %v", err)
	}
	return id
}

// rssEvents 按**发布顺序**收集事件（SubscribeAll 同步派发；typed handler 走
// goroutine 没有到达顺序保证——判据"done 递增"读的是发布侧的顺序）。
type rssEvents struct {
	mu        sync.Mutex
	progress  []*core.RssRefreshProgressEvent
	refreshed []*core.RssRefreshedEvent
}

func (e *rssEvents) subscribe(bus *core.EventBus) {
	bus.SubscribeAll(func(event core.AppEvent) {
		e.mu.Lock()
		defer e.mu.Unlock()
		switch ev := event.(type) {
		case *core.RssRefreshProgressEvent:
			e.progress = append(e.progress, ev)
		case *core.RssRefreshedEvent:
			e.refreshed = append(e.refreshed, ev)
		}
	})
}

func mapKeys(m map[string]interface{}) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func assertKeys(t *testing.T, got []string, want []string, what string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s：键数应 %d 得 %d（%v）", what, len(want), len(got), got)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("%s：键集应 %v 得 %v", what, want, got)
		}
	}
}

// ---------------------------------------------------------------------------
// 事件面（P5 判据）
// ---------------------------------------------------------------------------

// TestRssRefreshAllProgressFrames 三个源（两活一死）跑一轮全刷：
// 进度帧按发布顺序 done 递增（0 起手 + 逐源"开抓前"一帧）、收尾帧 done=total
// 且是唯一带 failures 的；每源一发 RssRefreshedEvent。
func TestRssRefreshAllProgressFrames(t *testing.T) {
	svc, _, bus := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed, "/atom.xml": rssTestAtom})
	addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")
	addRssTestSource(t, svc, site.URL, "/atom.xml", "Atom 源")

	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	deadURL := dead.URL
	dead.Close()
	if _, err := svc.AddSource("死源", deadURL+"/dead.xml", ""); err != nil {
		t.Fatalf("加死源失败: %v", err)
	}

	events := &rssEvents{}
	events.subscribe(bus)
	svc.RefreshAll(context.Background(), 15043844)

	if len(events.progress) != 5 {
		t.Fatalf("3 个源应 5 帧进度（起手 + 逐源 + 收尾），得 %d: %+v", len(events.progress), events.progress)
	}
	// 起手帧：分母开头就定下、current 空、failures=0。
	first := events.progress[0]
	if first.Done != 0 || first.Total != 3 || first.Current != "" || first.Failures != 0 {
		t.Fatalf("起手帧应 (0,3,\"\",0)，得 %+v", first)
	}
	// 逐源帧：done=index 严格递增，current=接下来要刷的那条源名。
	mids := events.progress[1:4]
	for index, frame := range mids {
		if frame.Done != index {
			t.Fatalf("第 %d 个逐源帧 done 应=%d，得 %+v", index, index, frame)
		}
		if frame.Failures != 0 {
			t.Fatalf("中途帧不许带 failures（画出来是锯齿失败数），得 %+v", frame)
		}
	}
	if mids[0].Current != "Atom 源" || mids[1].Current != "死源" || mids[2].Current != "语料源" {
		// 排序是 custom_order ASC, name ASC —— 两条源 custom_order=0，按名字：
		// "Atom 源" < "死源" < "语料源"（汉字按 UTF-8 字节序）。
		t.Fatalf("逐源帧 current 应跟排序后的源名走，得 %q/%q/%q",
			mids[0].Current, mids[1].Current, mids[2].Current)
	}
	// 收尾帧：done=total、唯一带 failures 的一帧（死源=1）。
	final := events.progress[4]
	if final.Done != 3 || final.Total != 3 || final.Current != "" || final.Failures != 1 {
		t.Fatalf("收尾帧应 (3,3,\"\",1)，得 %+v", final)
	}
	for _, frame := range events.progress {
		if frame.RefreshID != 15043844 {
			t.Fatalf("每帧都要原样回带 refresh_id，得 %+v", frame)
		}
		if frame.Timestamp <= 0 {
			t.Fatalf("帧要带 timestamp，得 %+v", frame)
		}
	}
	// 两活源各一发 RssRefreshedEvent（死源没有）。
	if len(events.refreshed) != 2 {
		t.Fatalf("两活源应各发一发 refreshed，得 %d", len(events.refreshed))
	}
	for _, ev := range events.refreshed {
		if ev.NewCount < 1 {
			t.Fatalf("首轮刷新 new_count 应 >0（非恒 0），得 %+v", ev)
		}
	}
}

// TestRssRefreshNewCountIsReal 单源连抓两遍：首轮 new_count=2（真新增），
// 重刷 0（去重），且重刷不清已读/收藏标记。
func TestRssRefreshNewCountIsReal(t *testing.T) {
	svc, _, bus := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")

	events := &rssEvents{}
	events.subscribe(bus)

	count, err := svc.Refresh(context.Background(), sourceID)
	if err != nil {
		t.Fatalf("首刷失败: %v", err)
	}
	if count != 2 {
		t.Fatalf("首刷应新增 2 篇，得 %d", count)
	}
	if len(events.refreshed) != 1 || events.refreshed[0].NewCount != 2 ||
		events.refreshed[0].SourceID != sourceID {
		t.Fatalf("应发一发 RssRefreshedEvent{source_id, new_count=2}，得 %+v", events.refreshed)
	}
	if events.refreshed[0].Timestamp <= 0 {
		t.Fatalf("refreshed 帧要带 timestamp，得 %+v", events.refreshed[0])
	}

	// 标记首篇已读 + 收藏，重刷后不许被覆盖。
	rows := svc.ArticleRows(sourceID, false)
	if len(rows) != 2 {
		t.Fatalf("库中应 2 篇，得 %d", len(rows))
	}
	firstID := rows[0]["id"].(int64)
	if changed, err := svc.MarkRead(firstID); err != nil || !changed {
		t.Fatalf("MarkRead 应改到行，得 changed=%v err=%v", changed, err)
	}
	if changed, err := svc.Star(firstID, true); err != nil || !changed {
		t.Fatalf("Star 应改到行，得 changed=%v err=%v", changed, err)
	}

	count, err = svc.Refresh(context.Background(), sourceID)
	if err != nil {
		t.Fatalf("重刷失败: %v", err)
	}
	if count != 0 {
		t.Fatalf("重刷应新增 0 篇（去重），得 %d", count)
	}
	refreshed := svc.ArticleRows(sourceID, false)
	if len(refreshed) != 2 {
		t.Fatalf("重刷后仍应 2 篇，得 %d", len(refreshed))
	}
	for _, row := range refreshed {
		if row["id"].(int64) != firstID {
			continue
		}
		if row["read"] != true || row["starred"] != true {
			t.Fatalf("重刷不许覆盖已读/收藏标记，得 %+v", row)
		}
	}
}

// TestRssRefreshUnknownSourceIsZeroNotError 源不在库里回 (0, nil)：列表刚被
// 另一侧删掉时不该炸界面（对齐 Python + schema 原文）。
func TestRssRefreshUnknownSourceIsZeroNotError(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	count, err := svc.Refresh(context.Background(), "http://gone.test/feed.xml")
	if err != nil || count != 0 {
		t.Fatalf("源不存在应 (0, nil)，得 (%d, %v)", count, err)
	}
}

// TestRssRefreshFetchFailureIsError 抓不到 feed 是**错误**不是静默 0——
// 「0 新增」与「没抓着」在 wire 上是错误信封与标量 0 两回事。
func TestRssRefreshFetchFailureIsError(t *testing.T) {
	svc, _, bus := newRssTestService(t)
	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	deadURL := dead.URL
	dead.Close()
	sourceID := addRssTestSource(t, svc, deadURL, "/dead.xml", "死源")

	events := &rssEvents{}
	events.subscribe(bus)
	count, err := svc.Refresh(context.Background(), sourceID)
	if err == nil {
		t.Fatalf("抓取失败应回错误，得 (count=%d, err=nil)", count)
	}
	if len(events.refreshed) != 0 {
		t.Fatalf("抓取失败不该发 refreshed 帧，得 %+v", events.refreshed)
	}
}

// ---------------------------------------------------------------------------
// wire 形状（键集对齐 golden field_names）
// ---------------------------------------------------------------------------

// TestRssWireKeyShapes 源行 10 键 / 文章行 9 键 / 正文 10 键，逐一对
// golden rss.sources.json / rss.articles.json / rss.article.json 的 field_names。
func TestRssWireKeyShapes(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")
	if _, err := svc.Refresh(context.Background(), sourceID); err != nil {
		t.Fatalf("刷新失败: %v", err)
	}

	sourceRows := svc.SourceRows()
	if len(sourceRows) != 1 {
		t.Fatalf("应 1 条源行，得 %d", len(sourceRows))
	}
	assertKeys(t, mapKeys(sourceRows[0]),
		[]string{"custom_order", "enabled", "group", "id", "last_update_time",
			"name", "total", "type", "unread", "url"}, "源行键集")
	if sourceRows[0]["id"] != sourceID || sourceRows[0]["total"] != 2 || sourceRows[0]["unread"] != 2 {
		t.Fatalf("源行应 id=url、total=2、unread=2，得 %+v", sourceRows[0])
	}

	articleRows := svc.ArticleRows(sourceID, false)
	if len(articleRows) != 2 {
		t.Fatalf("应 2 条文章行，得 %d", len(articleRows))
	}
	assertKeys(t, mapKeys(articleRows[0]),
		[]string{"author", "id", "image", "pub_date", "read", "source_id",
			"starred", "title", "url"}, "文章行键集（不带正文）")
	// 排序 pub_date DESC：Tue 的那条在前。
	if articleRows[0]["title"] != "纯文本的第二条" {
		t.Fatalf("pub_date 倒序应 Tue 在前，得 %+v", articleRows[0])
	}

	detail := svc.ArticleDetail(articleRows[1]["id"].(int64))
	if detail == nil {
		t.Fatal("正文应有值")
	}
	assertKeys(t, mapKeys(detail),
		[]string{"author", "id", "image", "pub_date", "read", "source_id",
			"starred", "text", "title", "url"}, "正文键集（多一格 text）")
	if svc.ArticleDetail(999999) != nil {
		t.Fatal("没这篇文章应回 nil（wire 上 data=null）")
	}
}

// TestRssArticleDetailStripsTagsOnlyThere 正文 text 是去标签的那一份
// （plainText = Python bs4.get_text("\n") 同形），列表行一根正文都不带。
func TestRssArticleDetailStripsTagsOnlyThere(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")
	if _, err := svc.Refresh(context.Background(), sourceID); err != nil {
		t.Fatalf("刷新失败: %v", err)
	}

	var taggedID int64
	for _, row := range svc.ArticleRows(sourceID, false) {
		if row["title"] == "带标签的第一条" {
			taggedID = row["id"].(int64)
			if _, ok := row["text"]; ok {
				t.Fatalf("列表行不许带正文，得 %+v", row)
			}
		}
	}
	detail := svc.ArticleDetail(taggedID)
	if detail == nil {
		t.Fatal("正文应有值")
	}
	// golden rss.article.json 的答案格：三段按块换行、标签剥掉、块内不动。
	if detail["text"] != "第一段\n加粗\n第二段" {
		t.Fatalf("去标签文本应 \"第一段\\n加粗\\n第二段\"，得 %q", detail["text"])
	}
}

// TestRssAtomFeedParses Atom 形状：rel=self 的 link 跳过、
// author/name、published、summary、media:thumbnail 封面。
func TestRssAtomFeedParses(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/atom.xml": rssTestAtom})
	sourceID := addRssTestSource(t, svc, site.URL, "/atom.xml", "Atom 源")
	count, err := svc.Refresh(context.Background(), sourceID)
	if err != nil || count != 2 {
		t.Fatalf("Atom 首刷应新增 2，得 (%d, %v)", count, err)
	}

	byTitle := map[string]map[string]interface{}{}
	for _, row := range svc.ArticleRows(sourceID, false) {
		byTitle[row["title"].(string)] = row
	}
	first, ok := byTitle["Atom 第一条"]
	if !ok {
		t.Fatalf("缺 Atom 第一条，得 %v", byTitle)
	}
	if first["url"] != "http://site.test/atom/1" {
		t.Fatalf("应取 rel=alternate 的 href（跳过 self），得 %v", first["url"])
	}
	if first["author"] != "作者丙" {
		t.Fatalf("Atom author 应取 name 文本，得 %v", first["author"])
	}
	if first["pub_date"] != "2026-10-05T09:00:00Z" {
		t.Fatalf("Atom 日期应取 published，得 %v", first["pub_date"])
	}
	if first["image"] != "http://site.test/atom/1.png" {
		t.Fatalf("封面应取 media:thumbnail@url，得 %v", first["image"])
	}
	second := byTitle["Atom 第二条"]
	if second["pub_date"] != "2026-10-06T09:00:00Z" {
		t.Fatalf("无 published 应回退 updated，得 %v", second["pub_date"])
	}
}

// TestRssSourceRowsJoinCounts 角标是服务端 join 好的：未读只数 read_flag=0，
// 「只看未读」折的是同一格。
func TestRssSourceRowsJoinCounts(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")
	if _, err := svc.Refresh(context.Background(), sourceID); err != nil {
		t.Fatalf("刷新失败: %v", err)
	}

	rows := svc.ArticleRows(sourceID, false)
	if changed, err := svc.MarkRead(rows[0]["id"].(int64)); err != nil || !changed {
		t.Fatalf("MarkRead 应改到行，得 changed=%v err=%v", changed, err)
	}
	sourceRows := svc.SourceRows()
	if sourceRows[0]["total"] != 2 || sourceRows[0]["unread"] != 1 {
		t.Fatalf("角标应 total=2 unread=1，得 %+v", sourceRows[0])
	}
	if unreadOnly := svc.ArticleRows(sourceID, true); len(unreadOnly) != 1 {
		t.Fatalf("只看未读应 1 行，得 %d", len(unreadOnly))
	}
}

// TestRssAddRemoveSourceSemantics 同 url 重复添加是更新不是重复行；
// 删除级联文章、真删到行回 true、再删回 false。
func TestRssAddRemoveSourceSemantics(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")

	// 重复添加同一 url：更新 name，行数不变。
	again, err := svc.AddSource("改名后的源", sourceID, "另一组")
	if err != nil || again != sourceID {
		t.Fatalf("重复添加应回同一 id，得 (%q, %v)", again, err)
	}
	sourceRows := svc.SourceRows()
	if len(sourceRows) != 1 || sourceRows[0]["name"] != "改名后的源" {
		t.Fatalf("重复添加应是更新，得 %+v", sourceRows)
	}

	if _, err := svc.Refresh(context.Background(), sourceID); err != nil {
		t.Fatalf("刷新失败: %v", err)
	}
	removed, err := svc.RemoveSource(sourceID)
	if err != nil || !removed {
		t.Fatalf("删除应真删到行，得 (%v, %v)", removed, err)
	}
	if len(svc.SourceRows()) != 0 {
		t.Fatal("删源后源表应空")
	}
	if len(svc.ArticleRows(sourceID, false)) != 0 {
		t.Fatal("删源应级联删文章")
	}
	removed, err = svc.RemoveSource(sourceID)
	if err != nil || removed {
		t.Fatalf("再删应回 false（这条本就不在），得 (%v, %v)", removed, err)
	}
}

// TestRssAddSourceRejectsEmptyURL 空 url 是错误（对齐 Python ValueError）。
func TestRssAddSourceRejectsEmptyURL(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	if _, err := svc.AddSource("没有地址", "  ", ""); err == nil {
		t.Fatal("空 url 应回错误")
	}
}

// TestRssStarIsSettingNotToggle 收藏是设定值不是翻转。
func TestRssStarIsSettingNotToggle(t *testing.T) {
	svc, _, _ := newRssTestService(t)
	site := newFeedSite(t, map[string]string{"/feed.xml": rssTestFeed})
	sourceID := addRssTestSource(t, svc, site.URL, "/feed.xml", "语料源")
	if _, err := svc.Refresh(context.Background(), sourceID); err != nil {
		t.Fatalf("刷新失败: %v", err)
	}
	rows := svc.ArticleRows(sourceID, false)
	id := rows[0]["id"].(int64)

	if changed, err := svc.Star(id, true); err != nil || !changed {
		t.Fatalf("收藏应改到行，得 (%v, %v)", changed, err)
	}
	if changed, err := svc.Star(id, true); err != nil || !changed {
		t.Fatalf("再传 true 仍是设定值（改到行），得 (%v, %v)", changed, err)
	}
	if changed, err := svc.Star(id, false); err != nil || !changed {
		t.Fatalf("传 false 应取消收藏，得 (%v, %v)", changed, err)
	}
	if changed, _ := svc.MarkRead(999999); changed {
		t.Fatal("不存在的文章 MarkRead 应回 false")
	}
}

// ---------------------------------------------------------------------------
// 旧表迁移（pre-FFI 的 INTEGER id 形状 → Python 形状）
// ---------------------------------------------------------------------------

// TestRssTablesMigrationFromOldShape 老 Go 形状（自增 id / "group" 列 /
// text 列）建库后，新 StorageService 打开时应重建成 Python 形状并保住数据：
// 源 id = url、文章按 (source_id,url) 归位、read/star 旗标随行。
func TestRssTablesMigrationFromOldShape(t *testing.T) {
	dir := t.TempDir()
	old, err := core.NewStorageService(dir)
	if err != nil {
		t.Fatalf("建库失败: %v", err)
	}
	// 把表改回老形状（drop 重建成 pre-FFI DDL），塞两行源与一篇文章。
	db := old.DB()
	for _, stmt := range []string{
		`DROP TABLE rss_articles`,
		`DROP TABLE rss_sources`,
		`CREATE TABLE rss_sources (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, "group" TEXT,
			enabled INTEGER DEFAULT 1, rss_type INTEGER DEFAULT 0,
			last_update_time INTEGER DEFAULT 0, custom_order INTEGER DEFAULT 0)`,
		`CREATE TABLE rss_articles (
			id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
			title TEXT NOT NULL, url TEXT, author TEXT, pub_date TEXT, image TEXT,
			read INTEGER DEFAULT 0, starred INTEGER DEFAULT 0, text TEXT,
			created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
			UNIQUE(source_id, url))`,
		`INSERT INTO rss_sources (id, name, url, "group", enabled, rss_type, last_update_time, custom_order)
			VALUES (7, '老源', 'http://old.test/feed.xml', '老组', 1, 0, 1700000000.5, 3)`,
		`INSERT INTO rss_articles (source_id, title, url, author, pub_date, image, read, starred, text, created_at)
			VALUES (7, '老文章', 'http://old.test/post/1', '老作者', 'Mon, 05 Oct 2026 09:00:00 GMT', '', 1, 1, '<p>老正文</p>', 1700000000000)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("摆老形状失败 @%q: %v", stmt[:28], err)
		}
	}
	_ = old.Close()

	reopened, err := core.NewStorageService(dir)
	if err != nil {
		t.Fatalf("重开库失败（迁移应通过）: %v", err)
	}
	defer reopened.Close()
	svc := NewRSSService(reopened, core.NewEventBus(), httpclient.NewClient())

	rows := svc.SourceRows()
	if len(rows) != 1 {
		t.Fatalf("迁移后应 1 条源，得 %d", len(rows))
	}
	if rows[0]["id"] != "http://old.test/feed.xml" || rows[0]["group"] != "老组" ||
		rows[0]["custom_order"] != 3 || rows[0]["total"] != 1 || rows[0]["unread"] != 0 {
		t.Fatalf("源应迁成 Python 形状（id=url / source_group / 角标），得 %+v", rows[0])
	}
	articles := svc.ArticleRows("http://old.test/feed.xml", false)
	if len(articles) != 1 || articles[0]["read"] != true || articles[0]["starred"] != true {
		t.Fatalf("文章应归位且旗标随身，得 %+v", articles)
	}
	detail := svc.ArticleDetail(articles[0]["id"].(int64))
	if detail == nil || detail["text"] != "老正文" {
		t.Fatalf("老 text 列应迁成 content 且正文可读，得 %+v", detail)
	}
	if detail["pub_date"] != "Mon, 05 Oct 2026 09:00:00 GMT" {
		t.Fatalf("老日期串应原样保留，得 %v", detail["pub_date"])
	}
}

// TestPlainTextMatchesGoldenRuler 去标签那把尺的形状（bs4.get_text("\n") 同形）。
func TestPlainTextMatchesGoldenRuler(t *testing.T) {
	cases := map[string]string{
		"<p>第一段</p><p><strong>加粗</strong></p><p>第二段</p>": "第一段\n加粗\n第二段",
		"没有标签的正文":                                     "没有标签的正文",
		"<div>上<script>var x = 1;</script>下</div>":    "上\n下",
		"<style>.a{color:red}</style><p>只剩这段</p>":     "只剩这段",
		"<p>Hello &amp; world</p><p>&lt;code&gt;</p>": "Hello & world\n<code>",
		"<p></p>  <p>  </p><p>有内容</p>":                "有内容",
	}
	for input, want := range cases {
		if got := plainText(input); got != want {
			t.Fatalf("plainText(%q) 应 %q，得 %q", input, want, got)
		}
	}
}
