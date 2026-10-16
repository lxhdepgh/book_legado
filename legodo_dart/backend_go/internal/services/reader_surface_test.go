package services

// P6：reader 面的形状核对测试 —— 断言口径是 **Dart 解析器吃的那些键**
// （lib/core/rpc/ffi_engine_adapter.dart 路由到的模型 fromJson），不是 Go 自己
// 觉得顺手的样子。golden 在 tests/gateway/golden/，键集与数值口径都照它核。

import (
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

const (
	readerSourceURL      = "https://src.test/g3"
	readerBookURL        = "https://src.test/g3/book/1.html"
	readerPreviewBookURL = "https://src.test/g3/book/preview.html"
	readerSearchKeyword  = "深潜测试词" // 5 字 → MATCH 路径
	// 一章 8 段：7×725 + 720 字 + 7 个段间换行 = 5802 —— 与 golden
	// reader.position_save--clamped 的收口数（chapter_pos→5802、段号→7）同一把尺。
	readerChapterTotal = 5802
)

// readerRig 最小装配：真 Storage（临时目录）+ 真书架/检索服务 + 注入
// chapterLoader 的 ReadingService；不装规则引擎（缓存命中路径与它无关）。
type readerRig struct {
	hub         *ServiceHub
	reading     *ReadingService
	shelf       *BookshelfService
	search      *ContentSearchService
	chapters    []*models.Chapter
	loaderCalls int
}

func newReaderRig(t *testing.T, chapters []*models.Chapter) *readerRig {
	t.Helper()
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("create storage: %v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })

	hub := NewServiceHub()
	hub.eventBus = core.NewEventBus()
	hub.storageSvc = storage
	src := &models.BookSource{BookSourceURL: readerSourceURL, BookSourceName: "G3阅读源", Enabled: true}
	hub.sourceSvc = &SourceService{cache: map[string]*models.BookSource{readerSourceURL: src}}
	hub.bookshelfSvc = NewBookshelfService(storage, hub.eventBus)
	hub.searchSvc = NewContentSearchService(storage, hub.eventBus)

	rig := &readerRig{hub: hub, chapters: chapters}
	reading := NewReadingService(hub)
	reading.chapterLoader = func(*models.BookSource, *models.BookItem) ([]*models.Chapter, error) {
		rig.loaderCalls++
		return rig.chapters, nil
	}
	hub.readingSvc = reading
	rig.reading = reading
	rig.shelf = hub.bookshelfSvc
	rig.search = hub.searchSvc
	return rig
}

func readerBook(url string) *models.BookItem {
	return &models.BookItem{
		URL: url, Name: "G3 试读书", Author: "作者戊", Intro: "简介",
		Origin: readerSourceURL, TocURL: readerSourceURL + "/toc.html",
		Kind: "连载", LastChapter: "第 6 章",
	}
}

func readerChapters(bookURL string) []*models.Chapter {
	chapters := make([]*models.Chapter, 0, 3)
	for i := 1; i <= 3; i++ {
		chapters = append(chapters, &models.Chapter{
			BookURL: bookURL,
			Index:   i - 1,
			Title:   fmt.Sprintf("第%d章 测试章", i),
			URL:     fmt.Sprintf("%s/ch/%d.html", readerSourceURL, i),
		})
	}
	return chapters
}

// readerChapterContent 一章正文（8 段，总长 readerChapterTotal）；keyword 落在
// 第 2 段段首，检索命中时可连 offset 一起核。
func readerChapterContent(chapterURL, keyword string) *models.ChapterContent {
	paragraphs := make([]string, 0, 8)
	for i := 0; i < 8; i++ {
		length := 725
		if i == 7 {
			length = 720
		}
		text := strings.Repeat("文", length)
		if keyword != "" && i == 2 {
			text = keyword + strings.Repeat("文", length-len([]rune(keyword)))
		}
		paragraphs = append(paragraphs, text)
	}
	return &models.ChapterContent{
		ChapterURL: chapterURL, Title: "章标题",
		Content: strings.Join(paragraphs, "\n"),
	}
}

// assertExactKeys 键集**两个方向**都钉：多一个键漏一个键都要红（对照
// whitelist-shape-guards-two-directions）。
func assertExactKeys(t *testing.T, where string, got map[string]interface{}, want ...string) {
	t.Helper()
	gotKeys := make([]string, 0, len(got))
	for key := range got {
		gotKeys = append(gotKeys, key)
	}
	sort.Strings(gotKeys)
	sorted := append([]string(nil), want...)
	sort.Strings(sorted)
	if strings.Join(gotKeys, ",") != strings.Join(sorted, ",") {
		t.Errorf("%s 键集不符：got %v want %v", where, gotKeys, sorted)
	}
}

func mustOpenReader(t *testing.T, rig *readerRig, bookURL string, shelve bool) map[string]interface{} {
	t.Helper()
	data, err := rig.reading.OpenByURL(readerSourceURL, readerBook(bookURL), shelve, true)
	if err != nil {
		t.Fatalf("open %s: %v", bookURL, err)
	}
	return data
}

// ---------------------------------------------------------------------------
// reader.open
// ---------------------------------------------------------------------------

func TestReaderOpenShape(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	data := mustOpenReader(t, rig, readerPreviewBookURL, false)

	assertExactKeys(t, "reader.open", data, "book", "sourceUrl", "bookUrl", "chapterCount", "position")
	if data["sourceUrl"] != readerSourceURL || data["bookUrl"] != readerPreviewBookURL {
		t.Errorf("urls 不符：%v / %v", data["sourceUrl"], data["bookUrl"])
	}
	if data["chapterCount"] != 3 {
		t.Errorf("chapterCount=%v want 3", data["chapterCount"])
	}

	// book 是 35 键的整快照（BookSnapshot），Dart 侧 ReaderSession.fromJson 吃它。
	bookMap, ok := data["book"].(map[string]interface{})
	if !ok {
		t.Fatalf("book 不是对象：%T", data["book"])
	}
	if len(bookMap) != 35 {
		t.Errorf("book 键数=%d want 35", len(bookMap))
	}
	for _, key := range []string{"name", "author", "url", "cover_url", "origin", "toc_url"} {
		if _, has := bookMap[key]; !has {
			t.Errorf("book 缺键 %s", key)
		}
	}

	// position 是 9 键默认存档（font_size 16），resume=true 但库里没有记录。
	position, ok := data["position"].(map[string]interface{})
	if !ok {
		t.Fatalf("position 不是对象：%T", data["position"])
	}
	assertExactKeys(t, "reader.open position", position,
		"chapter_url", "chapter_name", "chapter_index", "scroll_offset", "scroll_ratio",
		"chapter_pos", "chapter_paragraph", "font_size", "last_read_at")
	if position["chapter_index"] != 0 || position["font_size"] != 16 {
		t.Errorf("默认位置不符：%v", position)
	}
}

// TestReaderOpenOriginFallback 书架点开没有源 url：按 book.origin 反查书源。
func TestReaderOpenOriginFallback(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	data, err := rig.reading.OpenByURL("", readerBook(readerBookURL), true, true)
	if err != nil {
		t.Fatalf("open by origin: %v", err)
	}
	if data["sourceUrl"] != readerSourceURL {
		t.Errorf("sourceUrl=%v want %v（按 origin 反查）", data["sourceUrl"], readerSourceURL)
	}
}

func TestReaderOpenUnknownSource(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	book := readerBook(readerBookURL)
	book.Origin = ""
	if _, err := rig.reading.OpenByURL("https://nope.test/x", book, true, true); err == nil ||
		!strings.Contains(err.Error(), "书源不存在") {
		t.Fatalf("未知书源应报「书源不存在」，got %v", err)
	}
}

// ---------------------------------------------------------------------------
// reader.staged / staged_promote / staged_discard
// ---------------------------------------------------------------------------

// stagedRecordKeys = golden reader.staged.json 的 34 键（Dart StagedRecord.fromJson
// 与 raw 通吃的全套）。
var stagedRecordKeys = []string{
	"in_bookshelf", "name", "author", "intro", "url", "cover_url", "toc_url", "kind",
	"last_chapter", "word_count", "download_urls", "variables", "source_url", "source_name",
	"bookUrl", "tocUrl", "origin", "originName", "latestChapterTitle", "totalChapterNum",
	"lastCheckTime", "lastCheckCount", "durChapterTitle", "durChapterIndex", "durChapterPos",
	"durChapterTime", "canUpdate", "order", "group", "groupName", "latestChapterTime",
	"cachedChapterCount", "cacheTotalChapterCount", "cacheErrorCount",
}

func TestReaderStagedRecordShape(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerPreviewBookURL, false)

	record, ok := rig.reading.StagedRecordBySession()
	if !ok {
		t.Fatal("试读暂存应能取到记录")
	}
	assertExactKeys(t, "reader.staged", record, stagedRecordKeys...)
	if record["in_bookshelf"] != false {
		t.Errorf("in_bookshelf=%v want false", record["in_bookshelf"])
	}
	if record["source_url"] != readerSourceURL || record["source_name"] != "G3阅读源" {
		t.Errorf("源标识不符：%v / %v", record["source_url"], record["source_name"])
	}
	for _, key := range []string{"url", "bookUrl"} {
		if record[key] != readerPreviewBookURL {
			t.Errorf("%s=%v want %v", key, record[key], readerPreviewBookURL)
		}
	}
	if record["latestChapterTitle"] != "第 6 章" {
		t.Errorf("latestChapterTitle=%v", record["latestChapterTitle"])
	}
	if record["durChapterTitle"] != nil {
		t.Errorf("durChapterTitle=%v want nil", record["durChapterTitle"])
	}
	if record["canUpdate"] != true || record["groupName"] != "" {
		t.Errorf("canUpdate/groupName 不符：%v / %v", record["canUpdate"], record["groupName"])
	}
	if record["download_urls"] != nil {
		t.Errorf("download_urls=%v want nil", record["download_urls"])
	}

	// 在架的书不该被问（staged 只认 notShelf 那一条）。
	rigShelf := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rigShelf, readerBookURL, true)
	if _, ok := rigShelf.reading.StagedRecordBySession(); ok {
		t.Error("在架的书不该有 staged 记录")
	}
	// 开过又没记录的会话（没开过时）同样 false。
	rigNone := newReaderRig(t, readerChapters(readerBookURL))
	if _, ok := rigNone.reading.StagedRecordBySession(); ok {
		t.Error("没有会话时不该有 staged 记录")
	}
}

func TestReaderStagedPromoteAndDiscard(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerPreviewBookURL, false)

	if !rig.reading.PromoteStaged() {
		t.Fatal("暂存转正应成功")
	}
	if _, ok := rig.reading.StagedRecordBySession(); ok {
		t.Error("转正后不该再是 staged")
	}
	if record, ok := rig.shelf.FindShelfRecord(readerSourceURL, readerPreviewBookURL); !ok ||
		record["in_bookshelf"] != true {
		t.Errorf("转正后记录应 in_bookshelf=true：%v %v", record, ok)
	}
	if rig.reading.PromoteStaged() {
		t.Error("已在架再转正应回 false")
	}

	// 丢弃：记录、进度与缓存一起清掉。
	rigDrop := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rigDrop, readerPreviewBookURL, false)
	ch2 := readerSourceURL + "/ch/2.html"
	rigDrop.reading.CacheChapterDocument(ch2, readerChapterContent(ch2, ""))
	if _, err := rigDrop.reading.SavePositionByURL(readerSourceURL, readerPreviewBookURL,
		map[string]interface{}{"chapter_index": 1, "chapter_url": ch2}); err != nil {
		t.Fatalf("seed progress: %v", err)
	}
	if !rigDrop.reading.DiscardStaged() {
		t.Fatal("丢弃应成功")
	}
	if _, ok := rigDrop.shelf.FindShelfRecord(readerSourceURL, readerPreviewBookURL); ok {
		t.Error("丢弃后不该还有书架记录")
	}
	if progress := rigDrop.reading.LoadProgress(readerPreviewBookURL); progress != nil {
		t.Error("丢弃后 books 表进度行也该清掉")
	}
	if rigDrop.reading.DiscardStaged() {
		t.Error("再丢一次应回 false")
	}
}

// ---------------------------------------------------------------------------
// reader.close
// ---------------------------------------------------------------------------

func TestReaderCloseShape(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerPreviewBookURL, false)

	// url 对不上：什么都不关，回 {closed:false, staged:null}。
	mismatch := rig.reading.CloseByURL("https://other.test", "https://other.test/book", true)
	assertExactKeys(t, "reader.close--mismatch", mismatch, "closed", "staged")
	if mismatch["closed"] != false || mismatch["staged"] != nil {
		t.Errorf("mismatch 应 {closed:false, staged:nil}，got %v", mismatch)
	}
	if rig.reading.GetSession() == nil {
		t.Fatal("mismatch 不该关掉会话")
	}

	// 正常关试读书：staged 非空（关之前取的），进度已落库。
	out := rig.reading.CloseByURL(readerSourceURL, readerPreviewBookURL, true)
	assertExactKeys(t, "reader.close", out, "closed", "staged")
	if out["closed"] != true {
		t.Fatalf("closed=%v want true", out["closed"])
	}
	staged, ok := out["staged"].(map[string]interface{})
	if !ok {
		t.Fatalf("staged 应是 34 键记录：%T", out["staged"])
	}
	assertExactKeys(t, "reader.close staged", staged, stagedRecordKeys...)
	if rig.reading.GetSession() != nil {
		t.Error("关完会话应清空")
	}
	if progress := rig.reading.LoadProgress(readerPreviewBookURL); progress == nil {
		t.Error("save_progress=true 时关之前应落库进度")
	}

	// 在架的书：staged 恒 null。
	rigShelf := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rigShelf, readerBookURL, true)
	shelved := rigShelf.reading.CloseByURL(readerSourceURL, readerBookURL, true)
	if shelved["closed"] != true || shelved["staged"] != nil {
		t.Errorf("在架的书关会话 staged 应 null：%v", shelved)
	}
}

// ---------------------------------------------------------------------------
// reader.position_get / position_save
// ---------------------------------------------------------------------------

var positionKeys = []string{
	"chapter_url", "chapter_name", "chapter_index", "scroll_offset", "scroll_ratio",
	"chapter_pos", "chapter_paragraph", "font_size", "last_read_at",
}

func TestReaderPositionSaveClampAndArchive(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerBookURL, true)
	ch2 := readerSourceURL + "/ch/2.html"
	rig.reading.CacheChapterDocument(ch2, readerChapterContent(ch2, ""))

	start := time.Now().Unix()
	// 客户端报的坐标越界：比例 3.5、偏移 15801、段号 48 —— 全部按章内字符流收口；
	// chapter_url 空着，按 chapter_index 从目录认回。
	out, err := rig.reading.SavePositionByURL(readerSourceURL, readerBookURL, map[string]interface{}{
		"chapter_index": 1, "chapter_url": "", "chapter_name": "",
		"scroll_offset": 321.0, "scroll_ratio": 3.5, "chapter_pos": 15801,
		"chapter_paragraph": 48, "font_size": 18, "last_read_at": 1791615034,
	})
	if err != nil {
		t.Fatalf("save: %v", err)
	}
	assertExactKeys(t, "reader.position_save", out, positionKeys...)
	if out["chapter_url"] != ch2 || out["chapter_name"] != "第2章 测试章" {
		t.Errorf("锚定不符：%v / %v", out["chapter_url"], out["chapter_name"])
	}
	if out["scroll_ratio"] != 1.0 {
		t.Errorf("scroll_ratio=%v want 1（夹 [0,1]）", out["scroll_ratio"])
	}
	if out["chapter_pos"] != readerChapterTotal {
		t.Errorf("chapter_pos=%v want %d（夹到章长）", out["chapter_pos"], readerChapterTotal)
	}
	if out["chapter_paragraph"] != 7 {
		t.Errorf("chapter_paragraph=%v want 7（夹到末段）", out["chapter_paragraph"])
	}
	if out["font_size"] != 18 || out["last_read_at"] != int64(1791615034) {
		t.Errorf("font_size/last_read_at 不符：%v / %v", out["font_size"], out["last_read_at"])
	}

	// 回读走**存档形状**：chapter_url/name 恒空（books 表没有这两列），
	// last_read_at 是入库时刻（不是客户端报的那个值）。
	archive, ok := rig.reading.ReadPositionByURL(readerSourceURL, readerBookURL)
	if !ok {
		t.Fatal("刚存过应读得到位置")
	}
	assertExactKeys(t, "reader.position_get", archive, positionKeys...)
	if archive["chapter_url"] != "" || archive["chapter_name"] != "" {
		t.Errorf("存档形状 chapter_url/name 应恒空：%v / %v", archive["chapter_url"], archive["chapter_name"])
	}
	if archive["chapter_paragraph"] != 7 || archive["chapter_pos"] != readerChapterTotal {
		t.Errorf("回读收口值不符：%v", archive)
	}
	stored := archive["last_read_at"].(int64)
	if stored == 1791615034 || stored < start {
		t.Errorf("last_read_at 应为入库时刻：%v（start=%d）", stored, start)
	}

	// 负数/缺省收口：偏移与比例回 0、段号 nil（= 没存过段号，不是第 0 段）、字号 16。
	negative, err := rig.reading.SavePositionByURL(readerSourceURL, readerBookURL, map[string]interface{}{
		"chapter_index": 1, "scroll_offset": -5.0, "scroll_ratio": -2.0,
		"chapter_pos": -3, "chapter_paragraph": -1, "font_size": 0,
	})
	if err != nil {
		t.Fatalf("save negative: %v", err)
	}
	if negative["scroll_offset"] != 0.0 || negative["scroll_ratio"] != 0.0 ||
		negative["chapter_pos"] != 0 || negative["font_size"] != 16 {
		t.Errorf("负数收口不符：%v", negative)
	}
	if negative["chapter_paragraph"] != nil {
		t.Errorf("负段号应回 nil：%v", negative["chapter_paragraph"])
	}

	// 没有记录的书：position_get 回"没有"，不是"位置为 0"。
	if _, ok := rig.reading.ReadPositionByURL(readerSourceURL, "https://src.test/g3/book/9.html"); ok {
		t.Error("没记录时应回 ok=false")
	}
}

func TestReaderPositionSaveNoSession(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	if _, err := rig.reading.SavePositionByURL(readerSourceURL, readerBookURL,
		map[string]interface{}{}); err == nil || !strings.Contains(err.Error(), "当前会话不是这本书") {
		t.Fatalf("无会话保存应报 ReaderSessionError，got %v", err)
	}
}

// ---------------------------------------------------------------------------
// reader.toc / reader.content
// ---------------------------------------------------------------------------

func TestReaderTocShapeAndRefresh(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerBookURL, true)

	chapters, err := rig.reading.TocByURL(readerSourceURL, readerBookURL, false)
	if err != nil {
		t.Fatalf("toc: %v", err)
	}
	if len(chapters) != 3 || rig.loaderCalls != 1 {
		t.Fatalf("toc 行数=%d loaderCalls=%d（不刷新不该再载）", len(chapters), rig.loaderCalls)
	}

	// refresh=true：忽略缓存重载并顶掉会话里那一份。
	rig.chapters = append(rig.chapters, &models.Chapter{
		BookURL: readerBookURL, Index: 3, Title: "第4章 测试章",
		URL: readerSourceURL + "/ch/4.html",
	})
	refreshed, err := rig.reading.TocByURL(readerSourceURL, readerBookURL, true)
	if err != nil {
		t.Fatalf("toc refresh: %v", err)
	}
	if len(refreshed) != 4 || rig.loaderCalls != 2 {
		t.Fatalf("刷新后行数=%d loaderCalls=%d", len(refreshed), rig.loaderCalls)
	}
	if sess := rig.reading.GetSession(); sess == nil || len(sess.CachedChaps) != 4 {
		t.Error("刷新应顶掉会话里的目录")
	}

	// 会话不是这本书：报错，不回空目录。
	if _, err := rig.reading.TocByURL(readerSourceURL, "https://src.test/g3/book/9.html", false); err == nil ||
		!strings.Contains(err.Error(), "ReaderSessionError") {
		t.Fatalf("非会话书应报错，got %v", err)
	}
}

func TestReaderChapterDocumentShape(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerBookURL, true)
	ch2 := readerSourceURL + "/ch/2.html"
	rig.reading.CacheChapterDocument(ch2, readerChapterContent(ch2, ""))

	// 缓存命中路径：规则引擎是 nil 也照样出正文（这条就是"缓存真的被用上"的证据）。
	doc, err := rig.reading.ChapterDocument(readerSourceURL, readerBookURL, ch2)
	if err != nil {
		t.Fatalf("content: %v", err)
	}
	assertExactKeys(t, "reader.content", doc,
		"chapterUrl", "chapterIndex", "chapterName", "title", "imageStyle",
		"blocks", "paragraphCount", "totalChars")
	if doc["chapterIndex"] != 1 || doc["chapterName"] != "第2章 测试章" || doc["chapterUrl"] != ch2 {
		t.Errorf("章标识不符：%v", doc)
	}
	if doc["paragraphCount"] != 8 || doc["totalChars"] != readerChapterTotal {
		t.Errorf("段数/总长不符：%v / %v", doc["paragraphCount"], doc["totalChars"])
	}
	blocks, ok := doc["blocks"].([]interface{})
	if !ok || len(blocks) != 1 {
		t.Fatalf("blocks 形状不符：%T len=%d", doc["blocks"], len(blocks))
	}
	block, _ := blocks[0].(map[string]interface{})
	assertExactKeys(t, "reader.content block", block, "kind", "value", "url", "binary", "mediaKey", "title", "headers")
	if block["kind"] != "text" || block["binary"] != false {
		t.Errorf("块内容不符：%v", block)
	}

	// 没缓存又没引擎：当场报错，而不是回半截正文。
	if _, err := rig.reading.ChapterDocument(readerSourceURL, readerBookURL, readerSourceURL+"/ch/3.html"); err == nil {
		t.Error("未缓存章节且无引擎应报错")
	}
	// 会话不是这本书：报错。
	if _, err := rig.reading.ChapterDocument(readerSourceURL, "https://src.test/g3/book/9.html", ch2); err == nil ||
		!strings.Contains(err.Error(), "当前会话不是这本书") {
		t.Error("非会话书取正文应报错")
	}
}

// ---------------------------------------------------------------------------
// reader.search_content
// ---------------------------------------------------------------------------

func TestReaderSearchContentShape(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rig, readerBookURL, true)
	for _, ch := range rig.chapters {
		rig.reading.CacheChapterDocument(ch.URL, readerChapterContent(ch.URL, readerSearchKeyword))
	}

	data := rig.reading.SearchContentByURL(readerBookURL, readerSearchKeyword, readerSourceURL)
	assertExactKeys(t, "reader.search_content", data, "hits", "written", "indexedChapters", "ftsAvailable")
	if data["ftsAvailable"] != true {
		t.Fatalf("ftsAvailable=%v（FTS5 不可用则检索整本降级）", data["ftsAvailable"])
	}
	if data["written"] != 24 {
		t.Errorf("written=%v want 24（3 章 × 8 段）", data["written"])
	}
	if data["indexedChapters"] != 3 {
		t.Errorf("indexedChapters=%v want 3", data["indexedChapters"])
	}

	hits, ok := data["hits"].([]map[string]interface{})
	if !ok {
		t.Fatalf("hits 形状不符：%T", data["hits"])
	}
	// 3 章各中一处第 2 段（golden reader.search_content 同为 3 行、一章一行）；
	// chapter_index 取 TOC 下标（0 起，与 reader.content 的 chapterIndex 同一口径）。
	if len(hits) != 3 {
		t.Fatalf("hits 数=%d want 3", len(hits))
	}
	for i, hit := range hits {
		assertExactKeys(t, fmt.Sprintf("hit[%d]", i), hit, "chapter_index", "para_index", "offset", "snippet")
		if hit["chapter_index"] != i || hit["para_index"] != 2 {
			t.Errorf("hit[%d] 定位不符：%v", i, hit)
		}
		// offset 取自位置契约那把尺：前两段各 725 字 + 2 个换行格。
		if hit["offset"] != float64(2*(725+1)) {
			t.Errorf("hit[%d].offset=%v want %v", i, hit["offset"], float64(2*(725+1)))
		}
	}
	first := hits[0]
	if snippet, _ := first["snippet"].(string); !strings.Contains(snippet, "<mark>"+readerSearchKeyword+"</mark>") {
		t.Errorf("snippet 高亮不符：%q", first["snippet"])
	}

	// 幂等：索引已覆盖缓存，不再重建。
	again := rig.reading.SearchContentByURL(readerBookURL, readerSearchKeyword, readerSourceURL)
	if again["written"] != 0 {
		t.Errorf("二次检索 written=%v want 0", again["written"])
	}

	// 短词（<3 字）走 LIKE 扫描：同样 3 章各中一处。
	short := rig.reading.SearchContentByURL(readerBookURL, "深潜", readerSourceURL)
	if shortHits, ok := short["hits"].([]map[string]interface{}); !ok || len(shortHits) != 3 {
		t.Errorf("短词 LIKE 应命中 3 处：%v", short["hits"])
	}

	// 查不到：空列表而不是 nil（JSON null 会让 Dart 侧的类型转炸）。
	none := rig.reading.SearchContentByURL(readerBookURL, "没有这句词", readerSourceURL)
	noneHits, ok := none["hits"].([]map[string]interface{})
	if !ok || noneHits == nil || len(noneHits) != 0 {
		t.Errorf("无命中应为空 slice：%T %v", none["hits"], noneHits)
	}

	// 没有缓存可依据：不重建（written=0），如实报 0 章。
	rigEmpty := newReaderRig(t, readerChapters(readerBookURL))
	mustOpenReader(t, rigEmpty, readerBookURL, true)
	empty := rigEmpty.reading.SearchContentByURL(readerBookURL, readerSearchKeyword, readerSourceURL)
	if empty["written"] != 0 || empty["indexedChapters"] != 0 {
		t.Errorf("无缓存不该建索引：%v", empty)
	}
}

// ---------------------------------------------------------------------------
// bookshelf.list
// ---------------------------------------------------------------------------

func TestBookshelfListShapeWithStaged(t *testing.T) {
	rig := newReaderRig(t, readerChapters(readerBookURL))
	// 会话只装一本书：进度得趁在架那本还开着存，之后再开试读那本顶掉会话。
	mustOpenReader(t, rig, readerBookURL, true)
	ch2 := readerSourceURL + "/ch/2.html"
	rig.reading.CacheChapterDocument(ch2, readerChapterContent(ch2, ""))
	if _, err := rig.reading.SavePositionByURL(readerSourceURL, readerBookURL,
		map[string]interface{}{"chapter_index": 1, "chapter_url": ch2, "chapter_name": "第2章 测试章"}); err != nil {
		t.Fatalf("seed progress: %v", err)
	}
	mustOpenReader(t, rig, readerPreviewBookURL, false)

	shelved, err := rig.shelf.List(false)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(shelved) != 1 || shelved[0]["url"] != readerBookURL {
		t.Fatalf("默认列表应只有在架那一本：%v", shelved)
	}
	row := shelved[0]
	if row["in_bookshelf"] != true {
		t.Errorf("in_bookshelf=%v", row["in_bookshelf"])
	}
	for _, key := range []string{"name", "author", "cover_url", "url", "source_url", "origin",
		"reading", "durChapterTitle", "bookUrl", "tocUrl"} {
		if _, has := row[key]; !has {
			t.Errorf("书架行缺键 %s", key)
		}
	}
	reading, ok := row["reading"].(map[string]interface{})
	if !ok {
		t.Fatalf("reading 子对象缺失：%T", row["reading"])
	}
	if reading["chapter_name"] != "第2章 测试章" {
		t.Errorf("reading.chapter_name=%v（Dart 用它显示「读到」）", reading["chapter_name"])
	}

	withStaged, err := rig.shelf.List(true)
	if err != nil {
		t.Fatalf("list include_staged: %v", err)
	}
	if len(withStaged) != 2 {
		t.Fatalf("include_staged 应含试读暂存那本：%d 行", len(withStaged))
	}
	stagedFound := false
	for _, item := range withStaged {
		if item["url"] == readerPreviewBookURL {
			stagedFound = true
			if item["in_bookshelf"] != false {
				t.Errorf("暂存行 in_bookshelf=%v want false", item["in_bookshelf"])
			}
		}
	}
	if !stagedFound {
		t.Error("include_staged 列表里应能找到试读暂存那本")
	}
}
