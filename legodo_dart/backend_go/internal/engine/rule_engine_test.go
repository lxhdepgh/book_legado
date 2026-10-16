package engine

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
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
			Name:   strp("h1"),
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
