package engine

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// 这组 pin 的是「页面上抽到的相对地址按当页解析成绝对地址」——语料
// （tests/gateway/fake_source_reader.py）与真源的 href 全是相对的，不解析的话
// 下一跳取页在 http 层就断（unsupported protocol scheme ""），整条
// 搜索→详情→目录→正文链在第一个相对 href 上停住。此前引擎四处只取 Attr 原样，
// 单测都喂绝对地址所以照不出来；FFI 活臂接真语料才暴露。
func TestRelativeURLsResolveAgainstPageURL(t *testing.T) {
	pages := map[string]string{
		"/search": `<ul class="list">
<li><a href="/book/1.html">相对书</a><i>作者甲</i></li>
<li><a href="http://abs.example/book/2.html">绝对书</a><i>作者乙</i></li>
</ul>`,
		// toc 挂**目录相对**（无前导斜杠）：解出来应是 /book/toc.html，不是 /toc.html
		"/book/1.html": `<html><body><h1>相对书</h1>
<a class="toc" href="toc.html">目录</a></body></html>`,
		"/book/toc.html": `<ul>
<li><a href="ch/1.html">第1章</a></li>
<li><a href='/book/ch/2.html,{"webView":true}'>第2章</a></li>
<li><a href="http://abs.example/ch/3.html">第3章</a></li>
</ul>`,
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page, ok := pages[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(page))
	}))
	defer srv.Close()

	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	searchURL := srv.URL + "/search?key={{key}}"
	strp := func(s string) *string { return &s }
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		SearchURL:     &searchURL,
		RuleSearch: &models.SearchRule{
			BookList: strp("ul.list li"),
			Name:     strp("a@text"),
			BookURL:  strp("a@href"),
			Author:   strp("i@text"),
		},
		RuleBookInfo: &models.BookInfoRule{
			Name:   strp("h1@text"),
			TocURL: strp("a.toc@href"),
		},
		RuleToc: &models.TocRule{
			ChapterList: strp("li"),
			ChapterName: strp("a@text"),
			ChapterURL:  strp("a@href"),
		},
	}
	ctx := context.Background()

	books, err := re.SearchBook(ctx, source, "书", 1)
	if err != nil {
		t.Fatalf("SearchBook: %v", err)
	}
	if len(books) != 2 {
		t.Fatalf("want 2 books, got %d", len(books))
	}
	if want := srv.URL + "/book/1.html"; books[0].URL != want {
		t.Errorf("相对 bookUrl 没按搜索页解析：got %q want %q", books[0].URL, want)
	}
	if want := "http://abs.example/book/2.html"; books[1].URL != want {
		t.Errorf("绝对 bookUrl 被改写了：got %q want %q", books[1].URL, want)
	}

	info, err := re.GetBookInfo(ctx, source, books[0].URL)
	if err != nil {
		t.Fatalf("GetBookInfo: %v", err)
	}
	if want := srv.URL + "/book/toc.html"; info.TocURL != want {
		t.Errorf("目录相对 tocUrl 没按详情页解析：got %q want %q", info.TocURL, want)
	}

	chapters, err := re.GetChapters(ctx, source, info.TocURL)
	if err != nil {
		t.Fatalf("GetChapters: %v", err)
	}
	if len(chapters) != 3 {
		t.Fatalf("want 3 chapters, got %d", len(chapters))
	}
	if want := srv.URL + "/book/ch/1.html"; chapters[0].URL != want {
		t.Errorf("目录相对 chapterUrl 没按目录页解析：got %q want %q", chapters[0].URL, want)
	}
	// 章 url 尾巴上的 `,{"webView":true}` 选项要活着过解析（Legado 语义：选项挂在
	// url 上、由取页的一方处置）。Go 这边 `{`/`"` 会被 url 包转义成 %XX，判据按
	// 「起点是解析后的地址 + 选项字样还在」两段认，不钉整串编码形状。
	option := chapters[1].URL
	if !strings.HasPrefix(option, srv.URL+"/book/ch/2.html,") || !strings.Contains(option, "webView") {
		t.Errorf("带 webView 选项的 chapterUrl 解析后形状不对：got %q", option)
	}
	if want := "http://abs.example/ch/3.html"; chapters[2].URL != want {
		t.Errorf("绝对 chapterUrl 被改写了：got %q want %q", chapters[2].URL, want)
	}
}

// 假源站工厂：URL→HTML 的固定页表 + 命中计数（计数用于"翻页去重不能无限转"）。
func newPageSite(t *testing.T, pages map[string]string) (*httptest.Server, func(string) int) {
	t.Helper()
	var mu sync.Mutex
	hits := map[string]int{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page, ok := pages[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		mu.Lock()
		hits[r.URL.Path]++
		mu.Unlock()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(page))
	}))
	t.Cleanup(srv.Close)
	return srv, func(path string) int {
		mu.Lock()
		defer mu.Unlock()
		return hits[path]
	}
}

// 三个 HTML 真源（qbmfxs/po5/lvsexs）的 ruleBookInfo 都没有 tocUrl——目录就挂
// 在详情页上。不回落的话目录管线拿着空 toc_url 在 http 层就断（unsupported
// protocol scheme ""），整本书 0 章。init 的第二个语义：只有 JS 前缀才当脚本，
// 'data' 这类按选择器收窄（当脚本跑会炸 "data is not defined"，少年梦源实锤）。
func TestGetBookInfoTocURLFallbackAndInit(t *testing.T) {
	pages := map[string]string{
		"/book/1.html": `<html><body>
<h1 class="name">全页书名</h1>
<div class="detail"><h1 class="name">详情内书名</h1></div>
</body></html>`,
	}
	srv, _ := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	ctx := context.Background()
	bookURL := srv.URL + "/book/1.html"

	// 无 tocUrl 规则：toc_url 回落书 url；h1@text 取文档里第一个 h1。
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleBookInfo:  &models.BookInfoRule{Name: strp("h1@text")},
	}
	info, err := re.GetBookInfo(ctx, source, bookURL)
	if err != nil {
		t.Fatalf("GetBookInfo: %v", err)
	}
	if info.TocURL != bookURL {
		t.Errorf("tocUrl 缺失应回落书 url：got %q want %q", info.TocURL, bookURL)
	}
	if info.Name != "全页书名" {
		t.Errorf("Name=%q（无 init 应取全页首个 h1）", info.Name)
	}

	// init 是选择器：root 收窄到 .detail 里，h1 就是详情内的那个。
	narrow := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleBookInfo:  &models.BookInfoRule{Init: strp(".detail"), Name: strp("h1@text")},
	}
	info, err = re.GetBookInfo(ctx, narrow, bookURL)
	if err != nil {
		t.Fatalf("GetBookInfo(init 选择器): %v", err)
	}
	if info.Name != "详情内书名" {
		t.Errorf("init 选择器没收窄 root：Name=%q", info.Name)
	}

	// init 是裸词 'data'：旧版把它当 JS 跑 → "data is not defined" 硬错；
	// 现在按选择器处理（选不中就用整页），必须不报错。
	legacy := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleBookInfo:  &models.BookInfoRule{Init: strp("data"), Name: strp("h1@text")},
	}
	info, err = re.GetBookInfo(ctx, legacy, bookURL)
	if err != nil {
		t.Fatalf("init='data' 不该进 JS 运行时：%v", err)
	}
	if info.Name != "全页书名" {
		t.Errorf("init 选不中时应留全页 root：Name=%q", info.Name)
	}
}

// lvsexs 实况：详情页 /xs/454135.html 上没有目录（class.read 0 命中），目录在
// ruleBookInfo.tocUrl 的 JS 规则改写出的 /shu/454135/ 页上。旧版把 `@js:` 当
// 选择器 → 抽空 → toc_url 回落详情页 → 目录 0 章（探针 B 鲜取实锤）。JS 分支
// 要拿**裸全局** baseUrl，结果按详情页回拼成绝对地址。
func TestGetBookInfoTocURLJSRule(t *testing.T) {
	pages := map[string]string{
		"/xs/1.html": `<html><body><h1 class="detail">书名</h1></body></html>`,
	}
	srv, _ := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	bookURL := srv.URL + "/xs/1.html"
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleBookInfo: &models.BookInfoRule{
			Name:   strp("h1.detail@text"),
			TocURL: strp(`@js:baseUrl.replace(/\/xs\/(\d+)\.html/, '/shu/$1/')`),
		},
	}
	info, err := re.GetBookInfo(context.Background(), source, bookURL)
	if err != nil {
		t.Fatalf("GetBookInfo: %v", err)
	}
	if want := srv.URL + "/shu/1/"; info.TocURL != want {
		t.Errorf("tocUrl JS 规则结果不对：got %q want %q", info.TocURL, want)
	}
	if info.Name != "书名" {
		t.Errorf("Name=%q（JS 字段与选择器字段要能混用）", info.Name)
	}
}

// 目录页空且没有下一页时回详情页取一次（旧源 tocUrl 指错到"第一章"上的场景）；
// 相对章址必须按**详情页**解析。两页都空则报 TocEmptyError，而不是回空目录。
func TestFetchChapterListDetailFallbackAndEmpty(t *testing.T) {
	pages := map[string]string{
		"/first.html":  `<html><body><p>这不是目录页</p></body></html>`,
		"/book/1.html": `<html><body><h1>书名</h1>
<ul class="list"><li><a href="c/1.html">第1章</a></li><li><a href="c/2.html">第2章</a></li></ul></body></html>`,
		"/empty.html": `<html><body><p>哪儿都没有目录</p></body></html>`,
		"/book/9.html": `<html><body><p>详情页也没目录</p></body></html>`,
	}
	srv, hits := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleToc: &models.TocRule{
			ChapterList: strp("ul.list li"),
			ChapterName: strp("a@text"),
			ChapterURL:  strp("a@href"),
		},
	}
	ctx := context.Background()
	bookURL := srv.URL + "/book/1.html"

	chapters, err := re.FetchChapterList(ctx, source, srv.URL+"/first.html", bookURL)
	if err != nil {
		t.Fatalf("FetchChapterList(回落): %v", err)
	}
	if len(chapters) != 2 {
		t.Fatalf("回落详情页应得 2 章，得 %d", len(chapters))
	}
	// "c/1.html" 相对的是**详情页** /book/1.html → /book/c/1.html；
	// 若错按 /first.html 解析会得到 /c/1.html。
	if want := srv.URL + "/book/c/1.html"; chapters[0].URL != want {
		t.Errorf("回落章的相对地址没按详情页解析：got %q want %q", chapters[0].URL, want)
	}
	if chapters[0].Title != "第1章" || chapters[1].Title != "第2章" {
		t.Errorf("章名不符：%q / %q", chapters[0].Title, chapters[1].Title)
	}
	if got := hits("/book/1.html"); got != 1 {
		t.Errorf("详情页请求数=%d want 1（回落只来一次）", got)
	}

	// 目录页与详情页都空 → TocEmptyError（回空目录看起来像书坏了）。
	_, err = re.FetchChapterList(ctx, source, srv.URL+"/empty.html", srv.URL+"/book/9.html")
	if err == nil || !strings.Contains(err.Error(), "chapter list is empty") {
		t.Fatalf("全空应报 TocEmptyError，得 %v", err)
	}
	if got := hits("/book/9.html"); got != 1 {
		t.Errorf("回落后详情页请求数=%d want 1", got)
	}
}

// 目录行语义（真源语料的拼盘）：相对章址按页解析、javascript: 置空后回落页
// url、空名丢弃、卷标题合成 url、字数从 updateTime 尾巴拆出、卷不拆字数、
// isVip 走 Legado 真值。
func TestFetchChapterListRowSemantics(t *testing.T) {
	pages := map[string]string{
		"/toc.html": `<ul class="list">
<li><a href="/c/1.html">第1章 起点</a><span>3240字</span></li>
<li><a href="/c/2.html">第2章 终章</a><span>更新时间：2024-01-02</span><em>vip</em></li>
<li><a href="javascript:void(0)">点击广告</a></li>
<li><a href="/c/3.html"></a></li>
<li><a href="">第二卷</a><i>true</i><span>3240字</span></li>
</ul>`,
	}
	srv, _ := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleToc: &models.TocRule{
			ChapterList: strp("ul.list li"),
			ChapterName: strp("a@text"),
			ChapterURL:  strp("a@href"),
			UpdateTime:  strp("span@text"),
			IsVip:       strp("em@text"),
			IsVolume:    strp("i@text"),
			NextTocURL:  strp("a.next@href"), // 页面没有下一页链接：静默不翻
		},
	}
	bookURL := srv.URL + "/book/1.html"
	ctx := context.Background()

	chapters, err := re.FetchChapterList(ctx, source, srv.URL+"/toc.html", bookURL)
	if err != nil {
		t.Fatalf("FetchChapterList: %v", err)
	}
	if len(chapters) != 4 {
		t.Fatalf("空名应丢弃，应得 4 行，得 %d：%+v", len(chapters), chapters)
	}
	for i, chapter := range chapters {
		if chapter.Index != i || chapter.BookURL != bookURL {
			t.Errorf("[%d] index/bookUrl 不符：%d / %q", i, chapter.Index, chapter.BookURL)
		}
	}

	first := chapters[0]
	if first.Title != "第1章 起点" || first.URL != srv.URL+"/c/1.html" {
		t.Errorf("行0 name/url 不符：%q / %q", first.Title, first.URL)
	}
	if first.WordCountText != "3240字" || first.Tag != "" {
		t.Errorf("行0 字数拆解不符：wordCount=%q tag=%q", first.WordCountText, first.Tag)
	}
	if first.IsVip || first.IsVolume {
		t.Errorf("行0 不该是 vip/卷：%+v", first)
	}

	second := chapters[1]
	if second.Tag != "更新时间：2024-01-02" || second.WordCountText != "" {
		t.Errorf("行1 无字数尾巴时 tag 应保留原文：tag=%q wordCount=%q", second.Tag, second.WordCountText)
	}
	if !second.IsVip {
		t.Error("行1 em 里写了 vip，IsVip 应为 true")
	}

	third := chapters[2]
	if third.Title != "点击广告" || third.URL != srv.URL+"/toc.html" {
		t.Errorf("javascript 链接应置空并回落页 url：%q / %q", third.Title, third.URL)
	}

	volume := chapters[3]
	if !volume.IsVolume || volume.URL != "第二卷4" {
		t.Errorf("卷行应合成 url=名+页内序：%q isVolume=%v", volume.URL, volume.IsVolume)
	}
	// 卷不拆字数（对齐 Python tocCountWords 段）：原文整段留在 tag。
	if volume.WordCountText != "" || volume.Tag != "3240字" {
		t.Errorf("卷行不该拆字数：wordCount=%q tag=%q", volume.WordCountText, volume.Tag)
	}
}

// 同一 url 出现两次：按 Legado BookChapter 的 url 判等去重，保留**源顺序里的
// 最后一条**（Python chapters 尾段 reverse→去重→reverse 的净效果）。
func TestFetchChapterListDedupKeepsLastOccurrence(t *testing.T) {
	pages := map[string]string{
		"/toc.html": `<ul class="list">
<li><a href="/c/9.html">第9章 旧名</a></li>
<li><a href="/c/9.html">第9章 新名</a></li>
</ul>`,
	}
	srv, _ := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleToc: &models.TocRule{
			ChapterList: strp("ul.list li"),
			ChapterName: strp("a@text"),
			ChapterURL:  strp("a@href"),
		},
	}
	chapters, err := re.FetchChapterList(context.Background(), source, srv.URL+"/toc.html", srv.URL+"/book/1.html")
	if err != nil {
		t.Fatalf("FetchChapterList: %v", err)
	}
	if len(chapters) != 1 || chapters[0].Title != "第9章 新名" || chapters[0].Index != 0 {
		t.Fatalf("去重应保最后一条：%+v", chapters)
	}
}

// nextTocUrl 逐页发现：第一页的"下一页"相对链接按页解析；第二页的"下一页"
// 指回来（循环页）必须被 visited 挡住——真源的"下一页"常是这种自引用。
func TestFetchChapterListNextPageDiscovery(t *testing.T) {
	pages := map[string]string{
		"/toc.html": `<ul class="list"><li><a href="/c/1.html">第1章</a></li></ul>
<a class="next" href="toc2.html">下一页</a>`,
		"/toc2.html": `<ul class="list"><li><a href="/c/2.html">第2章</a></li></ul>
<a class="next" href="toc.html">下一页</a>`,
	}
	srv, hits := newPageSite(t, pages)
	re := NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	strp := func(s string) *string { return &s }
	source := &models.BookSource{
		BookSourceURL: srv.URL,
		RuleToc: &models.TocRule{
			ChapterList: strp("ul.list li"),
			ChapterName: strp("a@text"),
			ChapterURL:  strp("a@href"),
			NextTocURL:  strp("a.next@href"),
		},
	}
	chapters, err := re.FetchChapterList(context.Background(), source, srv.URL+"/toc.html", srv.URL+"/book/1.html")
	if err != nil {
		t.Fatalf("FetchChapterList: %v", err)
	}
	if len(chapters) != 2 {
		t.Fatalf("两页应得 2 章，得 %d：%+v", len(chapters), chapters)
	}
	if chapters[0].Title != "第1章" || chapters[1].Title != "第2章" {
		t.Errorf("跨页顺序不符：%q / %q", chapters[0].Title, chapters[1].Title)
	}
	if hits("/toc.html") != 1 || hits("/toc2.html") != 1 {
		t.Errorf("页请求数应为 1/1（自引用不能被再跟），得 %d/%d",
			hits("/toc.html"), hits("/toc2.html"))
	}
}
