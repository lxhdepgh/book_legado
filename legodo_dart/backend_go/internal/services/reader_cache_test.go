package services

// reader-cache 的盘上契约测试：键公式、目录/正文文件形状、版本守卫。
// 断言里的 sha256 常量是**独立算好的**（不是"再跑一遍同一个函数"）——第一对
// 就是真库真目录名（qbmfxs 那本《我的功法无限提升》），它若漂移，Dart 侧又会
// 看不见 flet 时代落的目录。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/models"
)

const (
	fixtureSourceURL = "https://src.test/g3"
	fixtureBookURL   = "https://src.test/g3/book/1.html"
	fixtureChapter   = "https://src.test/g3/ch/2.html"

	// 真库对：https://www.qbmfxs.com + https://www.qbmfxs.com/list_1/65596.html
	realDigest = "cf480f102438a8ef59127e62d28f49e85b1a3f76f2adb7730253103cba5953e6"
	// sha256("https://src.test/g3\0https://src.test/g3/book/1.html")
	fixtureDigest = "8f1fc67181c9bc51ce6555fd8aeb47e5bae72f86138d4e8d3b749f3e4fcbf2a7"
	// sha256("https://src.test/g3/ch/2.html")
	fixtureContentDigest = "039d20320ddaec5f10acf068dc2dc65dd738a8aedb66c83f073cb8094f4f1c23"
)

func TestCacheDigestMatchesPythonFormula(t *testing.T) {
	if got := cacheDigest("https://www.qbmfxs.com", "https://www.qbmfxs.com/list_1/65596.html"); got != realDigest {
		t.Errorf("真库目录名对不上（Python _digest 漂移）：got %s want %s", got, realDigest)
	}
	if got := cacheDigest(fixtureSourceURL, fixtureBookURL); got != fixtureDigest {
		t.Errorf("(source,book) 键公式漂移：got %s want %s", got, fixtureDigest)
	}
	if got := cacheDigest(fixtureChapter); got != fixtureContentDigest {
		t.Errorf("章 url 键公式漂移：got %s want %s", got, fixtureContentDigest)
	}
}

// 目录往返 + 版本守卫 + Python 真文件形状（v1、多余键、无 word_count）可读。
func TestCatalogRoundTripAndPythonCompat(t *testing.T) {
	cache := NewReadingCache(t.TempDir())
	chapters := []*models.Chapter{
		{BookURL: fixtureBookURL, Index: 0, Title: "第一章 起点", URL: "https://src.test/g3/ch/1.html",
			WordCountText: "3240字", Tag: "更新时间：2024-01-02", IsVip: true},
		{BookURL: fixtureBookURL, Index: 1, Title: "第二卷", URL: "第二卷1", IsVolume: true},
	}
	cache.SaveCatalog(fixtureSourceURL, fixtureBookURL, chapters)

	// 盘上形状：version 2 + snake_case 行（Python asdict(ChapterItem) 的口径）。
	raw, err := os.ReadFile(filepath.Join(cache.bookDir(fixtureSourceURL, fixtureBookURL), "catalog.json"))
	if err != nil {
		t.Fatalf("catalog.json 没落盘：%v", err)
	}
	var onDisk struct {
		Version  int                      `json:"version"`
		SavedAt  int64                    `json:"saved_at"`
		Chapters []map[string]interface{} `json:"chapters"`
	}
	if err := json.Unmarshal(raw, &onDisk); err != nil {
		t.Fatalf("盘上 JSON 解析失败：%v", err)
	}
	if onDisk.Version != 2 || onDisk.SavedAt == 0 || len(onDisk.Chapters) != 2 {
		t.Fatalf("盘上形状不符：version=%d saved_at=%d n=%d", onDisk.Version, onDisk.SavedAt, len(onDisk.Chapters))
	}
	for _, key := range []string{"name", "url", "index", "is_volume", "is_vip", "is_pay", "tag", "word_count"} {
		if _, has := onDisk.Chapters[0][key]; !has {
			t.Errorf("盘上行缺键 %s（Python asdict 口径）：%v", key, onDisk.Chapters[0])
		}
	}

	loaded, ok := cache.LoadCatalog(fixtureSourceURL, fixtureBookURL)
	if !ok || len(loaded) != 2 {
		t.Fatalf("写回读不回：ok=%v n=%d", ok, len(loaded))
	}
	if loaded[0].Title != "第一章 起点" || loaded[0].URL != "https://src.test/g3/ch/1.html" ||
		loaded[0].WordCountText != "3240字" || loaded[0].Tag != "更新时间：2024-01-02" || !loaded[0].IsVip {
		t.Errorf("行0 字段映射漂移：%+v", loaded[0])
	}
	if !loaded[1].IsVolume || loaded[1].URL != "第二卷1" {
		t.Errorf("行1 卷字段漂移：%+v", loaded[1])
	}

	// Python flet 时代落的真形状：v1、无 word_count、"index" 从 0 起、多余键
	// （base_url/start/variables 之类）一律无视。
	pythonV1 := `{"version": 1, "saved_at": 1786092540, "chapters": [
	  {"name": "第1章 穿越", "url": "https://m.douyinxs.com/bqg/1315794/343263366.html",
	   "index": 0, "is_volume": false, "is_vip": false, "is_pay": false, "tag": "",
	   "base_url": "", "book_url": "", "resource_url": "", "start": null, "end": null,
	   "variables": {}}]}`
	pythonDir := cache.bookDir("https://m.douyinxs.com", "https://m.douyinxs.com/bqg/1315794")
	if err := os.MkdirAll(pythonDir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(pythonDir, "catalog.json"), []byte(pythonV1), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	pyLoaded, ok := cache.LoadCatalog("https://m.douyinxs.com", "https://m.douyinxs.com/bqg/1315794")
	if !ok || len(pyLoaded) != 1 || pyLoaded[0].Title != "第1章 穿越" ||
		pyLoaded[0].URL != "https://m.douyinxs.com/bqg/1315794/343263366.html" {
		t.Fatalf("Python v1 文件读不回：ok=%v %+v", ok, pyLoaded)
	}

	// 版本守卫：不认识的版本当没有（对齐 Python load_catalog 的 None 支）。
	unknown := filepath.Join(cache.bookDir("https://src.test/x", "https://src.test/x/book"))
	if err := os.MkdirAll(unknown, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(unknown, "catalog.json"), []byte(`{"version": 9, "chapters": []}`), 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
	if _, ok := cache.LoadCatalog("https://src.test/x", "https://src.test/x/book"); ok {
		t.Error("version 9 应被当没有")
	}
	// 文件不在 / 内容不是 JSON：都是"没有"，不是报错。
	if _, ok := cache.LoadCatalog(fixtureSourceURL, "https://src.test/g3/book/none.html"); ok {
		t.Error("缺文件应回 ok=false")
	}
}

// 正文读盘：只出文本块（字节块跳过——媒体经 media 那条路，不在本轮）。
func TestLoadContentTextBlocksAndGuards(t *testing.T) {
	cache := NewReadingCache(t.TempDir())
	contentDir := filepath.Join(cache.bookDir(fixtureSourceURL, fixtureBookURL), "content")
	if err := os.MkdirAll(contentDir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	write := func(name, body string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(contentDir, name), []byte(body), 0644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	write(fixtureContentDigest+".json", `{"version": 1, "saved_at": 1, "title": "第二章 盘上标题",
	  "image_style": "", "raw_content": "",
	  "blocks": [
	    {"kind": "text", "value": "盘上正文甲\n盘上正文乙", "bytes": false, "headers": {}},
	    {"kind": "image", "value": "abc", "bytes": true, "headers": {}, "title": "", "url": ""},
	    {"kind": "text", "value": "尾段", "bytes": false}
	  ]}`)

	content, ok := cache.LoadContent(fixtureSourceURL, fixtureBookURL, fixtureChapter)
	if !ok {
		t.Fatal("盘上正文应能读到")
	}
	if content.Title != "第二章 盘上标题" {
		t.Errorf("title 应从盘上取：%q", content.Title)
	}
	// 文本块按序用 "\n" 拼回（块内本来就带换行的正文不再被吞）。
	if want := "盘上正文甲\n盘上正文乙\n尾段"; content.Content != want {
		t.Errorf("文本块拼回不符：got %q want %q", content.Content, want)
	}
	if content.ChapterURL != fixtureChapter {
		t.Errorf("chapterURL=%q", content.ChapterURL)
	}

	// 只有字节块 → 文本为空 → 当没有（上层会走联网）。
	write(cacheDigest("https://src.test/g3/ch/9.html")+".json",
		`{"version": 2, "blocks": [{"kind": "image", "value": "", "bytes": true}]}`)
	if _, ok := cache.LoadContent(fixtureSourceURL, fixtureBookURL, "https://src.test/g3/ch/9.html"); ok {
		t.Error("只有字节块应回 ok=false")
	}

	// 不认识的版本 / 缺文件：都是"没有"。
	write(cacheDigest("https://src.test/g3/ch/8.html")+".json", `{"version": 7, "blocks": [{"kind": "text", "value": "x"}]}`)
	if _, ok := cache.LoadContent(fixtureSourceURL, fixtureBookURL, "https://src.test/g3/ch/8.html"); ok {
		t.Error("version 7 应被当没有")
	}
	if _, ok := cache.LoadContent(fixtureSourceURL, fixtureBookURL, "https://src.test/g3/ch/7.html"); ok {
		t.Error("缺文件应回 ok=false")
	}
}
