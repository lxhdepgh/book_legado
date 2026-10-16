package engine

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
	"github.com/legodo-booksource/legodo_go/pkg/ruleparser"
)

// RuleEngine 规则引擎（对齐 Python rule_engine.py + Legado AnalyzeRule）
type RuleEngine struct {
	httpClient *httpclient.ClientSession
	jsRuntime  *jsruntime.Runtime
}

// NewRuleEngine 创建规则引擎
func NewRuleEngine(client *httpclient.ClientSession, js *jsruntime.Runtime) *RuleEngine {
	return &RuleEngine{
		httpClient: client,
		jsRuntime:  js,
	}
}

// resolvePageURL 把页面上抽到的地址按**当页 URL** 解析成绝对地址（对齐 Legado
// AnalyzeUrl.getAbsoluteURL / Python extract_value 里那一步 urljoin）。
//
// 为什么非他不可：语料与真源的 href 全是相对路径（`/g3/ch/2.html`），不解析的话
// 下一跳取页拿的是相对地址，http 层直接报 `unsupported protocol scheme ""`——
// 整条「搜索→详情→目录→正文」链在第一个相对 href 上就断了。
// 判据是 `IsAbs`（有 scheme 才原样保留：http/https 之外 data:/javascript: 这类
// 也不能拼），空值不回拼。
func resolvePageURL(baseURL, value string) string {
	raw := strings.TrimSpace(value)
	if raw == "" || baseURL == "" {
		return raw
	}
	base, err := url.Parse(baseURL)
	if err != nil {
		return raw
	}
	ref, err := url.Parse(raw)
	if err != nil || ref.IsAbs() {
		return raw
	}
	return base.ResolveReference(ref).String()
}

// SearchBook 搜索书籍（对齐 Legado WebBook.search）；
// page 从 1 起，同一次搜索翻页时由 SearchModel 递增后传入。
func (re *RuleEngine) SearchBook(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
	rule := source.GetSearchRule()
	if rule.BookList == nil {
		return nil, fmt.Errorf("书源 %s 缺少搜索规则", source.BookSourceURL)
	}

	// 构建搜索 URL
	searchURL, err := re.buildSearchURL(source, keyword, page)
	if err != nil {
		return nil, fmt.Errorf("构建搜索 URL 失败: %w", err)
	}

	// 发起请求
	resp, err := re.httpClient.Get(ctx, searchURL, nil)
	if err != nil {
		return nil, fmt.Errorf("搜索请求失败: %w", err)
	}

	html := resp.BodyString()

	// 解析 HTML
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, fmt.Errorf("解析 HTML 失败: %w", err)
	}

	// 提取书籍列表
	nodes := doc.Select(*rule.BookList)
	var books []*models.BookItem
	for _, node := range nodes {
		book := re.extractBookInfo(node, rule, source, searchURL)
		if book != nil {
			books = append(books, book)
		}
	}

	return books, nil
}

// buildSearchURL 构建搜索 URL（对齐 Legado AnalyzeUrl / Python resolve_search_url）。
// 模板替换顺序与 Python 一致：关键词 → 页码 → baseUrl → <第一页,后续页> 备选。
func (re *RuleEngine) buildSearchURL(source *models.BookSource, keyword string, page int) (string, error) {
	searchURL := ""
	if source.SearchURL != nil {
		searchURL = *source.SearchURL
	} else {
		return "", fmt.Errorf("未配置搜索 URL")
	}
	if page < 1 {
		page = 1
	}

	// 替换关键词占位符（先原样替换，最后整体编码非 ASCII —— 与 Python 相同）
	for _, variable := range []string{"{{key}}", "{{searchKey}}", "{{keyword}}", "{{keys}}"} {
		searchURL = strings.ReplaceAll(searchURL, variable, keyword)
	}
	searchURL = strings.ReplaceAll(searchURL, "{{page+1}}", strconv.Itoa(page+1))
	searchURL = strings.ReplaceAll(searchURL, "{{page-1}}", strconv.Itoa(max(page-1, 0)))
	searchURL = strings.ReplaceAll(searchURL, "{{page}}", strconv.Itoa(page))
	searchURL = strings.ReplaceAll(searchURL, "{{pg}}", strconv.Itoa(page))
	searchURL = strings.ReplaceAll(searchURL, "{{baseUrl}}", strings.TrimRight(source.BookSourceURL, "/")+"/")

	// Legado 页码备选：<first-page,second-page,later-pages>，超出列表后复用最后一格。
	searchURL = pageAlternation.ReplaceAllStringFunc(searchURL, func(match string) string {
		inner := match[1 : len(match)-1]
		choices := strings.Split(inner, ",")
		if len(choices) == 0 {
			return ""
		}
		idx := page - 1
		if idx < 0 {
			idx = 0
		}
		if idx >= len(choices) {
			idx = len(choices) - 1
		}
		return strings.TrimSpace(choices[idx])
	})

	// 还有未替换的模板（复杂表达式）无法处理
	if strings.Contains(searchURL, "{{") && strings.Contains(searchURL, "}}") {
		if strings.Contains(searchURL, "{{source.key}}") || strings.Contains(searchURL, "{{sourceKey}}") {
			searchURL = strings.ReplaceAll(searchURL, "{{source.key}}", source.BookSourceURL)
			searchURL = strings.ReplaceAll(searchURL, "{{sourceKey}}", source.BookSourceURL)
		}
		if strings.Contains(searchURL, "{{") && strings.Contains(searchURL, "}}") {
			return "", fmt.Errorf("搜索地址格式暂时无法解析")
		}
	}

	// data: URL 是 Legado 内部传输编码，原样保留
	if strings.HasPrefix(searchURL, "data:") {
		return searchURL, nil
	}

	// 相对路径拼 base_url
	if !strings.HasPrefix(searchURL, "http://") && !strings.HasPrefix(searchURL, "https://") {
		base, err := url.Parse(strings.TrimRight(source.BookSourceURL, "/") + "/")
		if err != nil {
			return "", fmt.Errorf("书源 URL 无效: %w", err)
		}
		ref, err := url.Parse(searchURL)
		if err != nil {
			return "", fmt.Errorf("搜索地址无法解析: %w", err)
		}
		searchURL = base.ResolveReference(ref).String()
	}

	if !strings.HasPrefix(searchURL, "http://") && !strings.HasPrefix(searchURL, "https://") {
		return "", fmt.Errorf("搜索地址不是 http/https")
	}

	return encodeNonASCIIURL(searchURL), nil
}

var pageAlternation = regexp.MustCompile(`<([^<>]*)>`)

// encodeNonASCIIURL 把 URL 中的非 ASCII 字符编成百分号形式（对齐 Python encode_url）。
// 全是 ASCII 时原样返回，避免对已有编码做二次转义。
func encodeNonASCIIURL(raw string) string {
	asciiOnly := true
	for i := 0; i < len(raw); i++ {
		if raw[i] >= 0x80 {
			asciiOnly = false
			break
		}
	}
	if asciiOnly {
		return raw
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme == "" {
		return escapeNonASCII(raw, ":/?#[]@!$&'()*+,;=-._~%")
	}
	parsed.Path = escapeNonASCII(parsed.Path, "/%")
	parsed.RawPath = ""
	parsed.RawQuery = escapeNonASCII(parsed.RawQuery, "=&%")
	parsed.Fragment = escapeNonASCII(parsed.Fragment, "")
	return parsed.String()
}

// escapeNonASCII 保留 ASCII 中安全集合内的字符，其余（含空格与非 ASCII 字节）逐字节百分号编码。
func escapeNonASCII(value, safe string) string {
	var b strings.Builder
	for i := 0; i < len(value); i++ {
		c := value[i]
		if c < 0x80 && (isURLSafeByte(c) || strings.IndexByte(safe, c) >= 0) {
			b.WriteByte(c)
			continue
		}
		b.WriteString(fmt.Sprintf("%%%02X", c))
	}
	return b.String()
}

func isURLSafeByte(c byte) bool {
	switch {
	case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		return true
	}
	return strings.IndexByte("_.-~", c) >= 0
}

// extractBookInfo 从 DOM 节点提取书籍信息；baseURL 是这一页的地址，相对 href 按它解析。
func (re *RuleEngine) extractBookInfo(node *ruleparser.Node, rule *models.SearchRule, source *models.BookSource, baseURL string) *models.BookItem {
	book := &models.BookItem{
		Origin:     source.BookSourceURL,
		OriginName: source.BookSourceName,
	}

	// 书名
	if rule.Name != nil {
		book.Name = node.Field(*rule.Name, "text")
	}

	// 作者
	if rule.Author != nil {
		book.Author = re.formatAuthor(node.Field(*rule.Author, "text"))
	}

	// 简介
	if rule.Intro != nil {
		book.Intro = re.formatIntro(node.Field(*rule.Intro, "text"))
	}

	// 封面
	if rule.CoverURL != nil {
		book.CoverURL = resolvePageURL(baseURL, node.Field(*rule.CoverURL, "src"))
	}

	// 书籍 URL
	if rule.BookURL != nil {
		book.URL = resolvePageURL(baseURL, node.Field(*rule.BookURL, "href"))
	}

	// 最新章节
	if rule.LastChapter != nil {
		book.LastChapter = node.Field(*rule.LastChapter, "text")
	}

	return book
}

// formatAuthor 格式化作者（对齐 Python _format_book_author）
func (re *RuleEngine) formatAuthor(value string) string {
	text := strings.TrimSpace(value)
	// 移除"作者："前缀
	text = strings.TrimPrefix(text, "作者：")
	text = strings.TrimPrefix(text, "作者:")
	// 移除"著"后缀
	text = strings.TrimSuffix(text, "著")
	return strings.TrimSpace(text)
}

// formatIntro 格式化简介（对齐 Python _format_book_intro）
func (re *RuleEngine) formatIntro(value string) string {
	text := strings.TrimSpace(value)
	// 检查是否有特殊标记
	if strings.HasPrefix(text, "<usehtml>") ||
		strings.HasPrefix(text, "<md>") ||
		strings.HasPrefix(text, "<useweb>") {
		return text // 保留原始标记
	}
	// 去除 HTML 标签
	return ruleparser.StripHTML(text)
}

// GetBookInfo 获取书籍详情（对齐 Legado WebBook.getBookInfo）
func (re *RuleEngine) GetBookInfo(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error) {
	rule := source.GetBookInfoRule()

	// 发起请求
	resp, err := re.httpClient.Get(ctx, bookURL, nil)
	if err != nil {
		return nil, err
	}

	html := resp.BodyString()

	// 执行初始化 JS（如果有）
	if rule.Init != nil {
		_, err := re.jsRuntime.Execute(*rule.Init, &jsruntime.Context{
			BaseURL: bookURL,
			Source: map[string]interface{}{
				"bookSourceUrl": source.BookSourceURL,
			},
		})
		if err != nil {
			return nil, fmt.Errorf("执行 init JS 失败: %w", err)
		}
	}

	// 解析 HTML
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, err
	}

	book := &models.BookItem{
		URL:    bookURL,
		Origin: source.BookSourceURL,
	}

	// 提取书名
	if rule.Name != nil {
		book.Name = doc.Field(*rule.Name, "text")
	}

	// 提取作者
	if rule.Author != nil {
		book.Author = re.formatAuthor(doc.Field(*rule.Author, "text"))
	}

	// 提取简介
	if rule.Intro != nil {
		book.Intro = re.formatIntro(doc.Field(*rule.Intro, "html"))
	}

	// 提取封面
	if rule.CoverURL != nil {
		book.CoverURL = resolvePageURL(bookURL, doc.Field(*rule.CoverURL, "src"))
	}

	// 提取目录 URL
	if rule.TocURL != nil {
		book.TocURL = resolvePageURL(bookURL, doc.Field(*rule.TocURL, "href"))
	}

	return book, nil
}

// GetChapters 获取章节列表（对齐 Legado WebBook.getChapterList）
func (re *RuleEngine) GetChapters(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error) {
	rule := source.GetTocRule()
	if rule.ChapterList == nil {
		return nil, fmt.Errorf("书源 %s 缺少目录规则", source.BookSourceURL)
	}

	// 发起请求
	resp, err := re.httpClient.Get(ctx, tocURL, nil)
	if err != nil {
		return nil, err
	}

	html := resp.BodyString()

	// 解析 HTML
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, err
	}

	// 提取章节列表
	nodes := doc.Select(*rule.ChapterList)
	var chapters []*models.Chapter
	for i, node := range nodes {
		chapter := &models.Chapter{
			BookURL: tocURL,
			Index:   i,
		}

		// 章节名
		if rule.ChapterName != nil {
			chapter.Title = strings.TrimSpace(node.Field(*rule.ChapterName, "text"))
		}

		// 章节 URL
		if rule.ChapterURL != nil {
			chapter.URL = resolvePageURL(tocURL, node.Field(*rule.ChapterURL, "href"))
		}

		// VIP 标识
		if rule.IsVip != nil {
			selector, _ := ruleparser.SplitFieldRule(*rule.IsVip)
			chapter.IsVip = len(node.Select(selector)) > 0
		}

		chapters = append(chapters, chapter)
	}

	return chapters, nil
}

// BrowseBooks 浏览发现书籍（对齐 Legado Explore）
func (re *RuleEngine) BrowseBooks(ctx context.Context, source *models.BookSource, searchURL string, category string) ([]*models.BookItem, error) {
	rule := source.RuleExplore
	if rule == nil || rule.BookList == nil {
		return nil, fmt.Errorf("书源 %s 缺少发现规则", source.BookSourceURL)
	}

	urlToFetch := searchURL
	if urlToFetch == "" {
		// Try to use ExploreURL from source as base
		if source.ExploreURL != nil {
			urlToFetch = strings.ReplaceAll(*source.ExploreURL, "{{page}}", "1")
		} else {
			return nil, fmt.Errorf("未配置发现 URL")
		}
	}

	resp, err := re.httpClient.Get(ctx, urlToFetch, nil)
	if err != nil {
		return nil, fmt.Errorf("浏览请求失败: %w", err)
	}

	html := resp.BodyString()
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, fmt.Errorf("解析 HTML 失败: %w", err)
	}

	nodes := doc.Select(*rule.BookList)
	var books []*models.BookItem
	for _, node := range nodes {
		book := &models.BookItem{
			Origin:     source.BookSourceURL,
			OriginName: source.BookSourceName,
		}

		// Extract using ExploreRule fields (same structure as SearchRule)
		if rule.Name != nil {
			book.Name = node.Field(*rule.Name, "text")
		}
		if rule.Author != nil {
			book.Author = re.formatAuthor(node.Field(*rule.Author, "text"))
		}
		if rule.Intro != nil {
			book.Intro = re.formatIntro(node.Field(*rule.Intro, "text"))
		}
		if rule.CoverURL != nil {
			book.CoverURL = resolvePageURL(urlToFetch, node.Field(*rule.CoverURL, "src"))
		}
		if rule.BookURL != nil {
			book.URL = resolvePageURL(urlToFetch, node.Field(*rule.BookURL, "href"))
		}
		if book.Name != "" {
			books = append(books, book)
		}
	}

	return books, nil
}

// GetContent 获取章节内容（对齐 Legado WebBook.getContent）
func (re *RuleEngine) GetContent(ctx context.Context, source *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error) {
	rule := source.GetContentRule()
	if rule.Content == nil {
		return nil, fmt.Errorf("书源 %s 缺少正文规则", source.BookSourceURL)
	}

	// 发起请求
	resp, err := re.httpClient.Get(ctx, chapter.URL, nil)
	if err != nil {
		return nil, err
	}

	html := resp.BodyString()

	// 解析 HTML
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, err
	}

	content := &models.ChapterContent{
		ChapterURL: chapter.URL,
		Title:      chapter.Title,
		ParsedAt:   time.Now(),
	}

	// 提取标题
	if rule.Title != nil {
		if title := strings.TrimSpace(doc.Field(*rule.Title, "text")); title != "" {
			content.Title = title
		}
	}

	// 提取正文
	content.Content = ruleparser.HTMLToText(doc.Field(*rule.Content, "html"))

	// 处理分页（subContent）
	if rule.SubContent != nil {
		if sub := ruleparser.HTMLToText(doc.Field(*rule.SubContent, "html")); sub != "" {
			content.SubContent = &sub
		}
	}

	// 执行 WebJS（如果有）
	if rule.WebJS != nil {
		result, err := re.jsRuntime.Execute(*rule.WebJS, &jsruntime.Context{
			BaseURL: chapter.URL,
			Source: map[string]interface{}{
				"bookSourceUrl": source.BookSourceURL,
			},
		})
		if err == nil {
			if str, ok := result.(string); ok {
				content.Content = str
			}
		}
	}

	return content, nil
}
