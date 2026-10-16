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

// GetBookInfo 获取书籍详情（对齐 Python _apply_book_info 的详情面）。
//
// 三处与旧版不同、都是真源实测出来的：
//   - init 只有 JS 前缀（`@js:`/`<js>`）才当脚本跑；`'data'` 这类是选择器/JSON
//     路径，旧版直接 evaluate 会在 "data is not defined" 上硬报错（少年梦源实锤）。
//   - 字段规则是 JS 的走 fieldRaw 执行（lvsexs 的 tocUrl =
//     `@js:baseUrl.replace(/\/xs\/(\d+)\.html/, '/shu/$1/')`：详情页 /xs/ 上
//     根本没目录，目录在改写出的 /shu/ 页上），旧版把 `@js:` 当选择器抽空。
//   - tocUrl 没规则或抽不到时**回落详情页 url**（对齐 `book.toc_url = value or
//     base_url`）：qbmfxs/po5 两个真源没有 tocUrl 规则，目录就挂在详情页上。
func (re *RuleEngine) GetBookInfo(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error) {
	rule := source.GetBookInfoRule()

	// 发起请求
	resp, err := re.httpClient.Get(ctx, bookURL, nil)
	if err != nil {
		return nil, err
	}

	html := resp.BodyString()

	// 解析 HTML
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, err
	}

	// 详情根：init 是 JS 前缀 → 执行脚本；否则按选择器收窄（收不到就用整页）。
	// 文档级与节点级 Field 同形，收窄与否下面同一串规则照跑。
	var root interface{ Field(rule, defAttr string) string } = doc
	if rule.Init != nil {
		init := strings.TrimSpace(*rule.Init)
		switch {
		case init == "" || strings.HasPrefix(init, "@put:"):
			// @put: 是写变量，不是正文前处理；本轮不落变量注册表，跳过。
		case isJSRule(init):
			if _, err := re.jsRuntime.Execute(stripJSRule(init), &jsruntime.Context{
				BaseURL: bookURL,
				Source: map[string]interface{}{
					"bookSourceUrl": source.BookSourceURL,
				},
			}); err != nil {
				return nil, fmt.Errorf("执行 init JS 失败: %w", err)
			}
		default:
			if narrowed := doc.Select(init); len(narrowed) > 0 {
				root = narrowed[0]
			}
		}
	}

	book := &models.BookItem{
		URL:    bookURL,
		Origin: source.BookSourceURL,
	}

	// 提取书名
	if rule.Name != nil {
		book.Name = re.fieldRaw(root, *rule.Name, "text", bookURL)
	}

	// 提取作者
	if rule.Author != nil {
		book.Author = re.formatAuthor(re.fieldRaw(root, *rule.Author, "text", bookURL))
	}

	// 提取简介
	if rule.Intro != nil {
		book.Intro = re.formatIntro(re.fieldRaw(root, *rule.Intro, "html", bookURL))
	}

	// 提取封面
	if rule.CoverURL != nil {
		book.CoverURL = resolvePageURL(bookURL, re.fieldRaw(root, *rule.CoverURL, "src", bookURL))
	}

	// 提取目录 URL；缺失/为空回落详情页（目录多数就在详情页上）
	if rule.TocURL != nil {
		book.TocURL = resolvePageURL(bookURL, re.fieldRaw(root, *rule.TocURL, "href", bookURL))
	}
	if book.TocURL == "" {
		book.TocURL = bookURL
	}

	return book, nil
}

// isJSRule 规则文本是不是 JS（Legado 的 @js: / <js> 两种标记；只有这两种
// 才该送进 JS 运行时——选择器被当脚本 evaluate 就会炸在"未定义"上）。
func isJSRule(rule string) bool {
	rule = strings.TrimSpace(rule)
	return strings.HasPrefix(rule, "@js:") || strings.HasPrefix(rule, "<js>")
}

// stripJSRule 剥掉 JS 标记留下脚本体。
func stripJSRule(rule string) string {
	rule = strings.TrimSpace(rule)
	if strings.HasPrefix(rule, "@js:") {
		return rule[len("@js:"):]
	}
	if strings.HasPrefix(rule, "<js>") {
		body := rule[len("<js>"):]
		if idx := strings.Index(body, "</js>"); idx >= 0 {
			return body[:idx]
		}
		return body
	}
	return rule
}

// fieldRaw 字段规则的统一取值口：`@js:`/`<js>` 规则送 JS 运行时执行（对齐 Python
// evaluate 的 js 分支——裸 baseUrl 由 injectGlobals 注入为当页地址），其余照走
// 选择器。JS 出错回空：详情字段是"抽不到就回落"的语义（tocUrl 空了回落详情页），
// 一条坏规则不该让整本书 0 章。
func (re *RuleEngine) fieldRaw(root interface{ Field(rule, defAttr string) string }, rule, defAttr, baseURL string) string {
	if !isJSRule(rule) {
		return root.Field(rule, defAttr)
	}
	value, err := re.jsRuntime.Execute(stripJSRule(rule), &jsruntime.Context{BaseURL: baseURL})
	if err != nil || value == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprintf("%v", value))
}

// legadoWordCountRE Legado AppPattern.wordCountRegex 的逐字移植：
// 从 updateTime 尾巴里抽 "3240 字" 这类字数（对齐 Python _LEGADO_WORD_COUNT_RE）。
var legadoWordCountRE = regexp.MustCompile(`(?:^|字数[：:、]?|\s+)([0-9万千百\.]{1,6}字)`)

// legadoTruthy Legado 布尔规则的真值：只有显式 false 字样为假（对齐 _legado_truthy）。
func legadoTruthy(value string) bool {
	text := strings.TrimSpace(value)
	if text == "" || strings.EqualFold(text, "null") {
		return false
	}
	switch strings.ToLower(text) {
	case "false", "no", "not", "0":
		return false
	}
	return true
}

// tocRuleTruthy 目录项上的真值字段（isVolume/isVip/isPay）。
func tocRuleTruthy(node *ruleparser.Node, rule *string) bool {
	if rule == nil || strings.TrimSpace(*rule) == "" {
		return false
	}
	return legadoTruthy(node.Field(*rule, "text"))
}

// stripURLOptions 去掉 Legado 地址尾巴上的 `,{请求选项}`，留下地址本身——
// "这一页来过了没"按它认（对齐 Python _media_request 的 [0] 那半）。
func stripURLOptions(value string) string {
	value = strings.TrimSpace(value)
	if idx := strings.LastIndex(value, ",{"); idx > 0 {
		return strings.TrimSpace(value[:idx])
	}
	return value
}

// GetChapters 目录页取章（check 线入口：目录 URL 与书 URL 同键时等价于全流程）。
func (re *RuleEngine) GetChapters(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error) {
	return re.FetchChapterList(ctx, source, tocURL, tocURL)
}

// FetchChapterList 目录全流程（对齐 Python reading_engine.chapters 的目录段）：
// 单页起步、nextTocUrl 逐页发现；一章没取到且详情页还没来过就回详情页一次；
// 还是空就报 "chapter list is empty"。空名丢弃、javascript:/void( 地址置空、
// 卷标题补合成 url（名+页内序）、按 url 去重（保源顺序里的最后一条）、统一编 index。
func (re *RuleEngine) FetchChapterList(ctx context.Context, source *models.BookSource, tocURL, bookURL string) ([]*models.Chapter, error) {
	rule := source.GetTocRule()
	if rule.ChapterList == nil {
		return nil, fmt.Errorf("书源 %s 缺少目录规则", source.BookSourceURL)
	}
	listRule := strings.TrimSpace(*rule.ChapterList)
	reverseRule := strings.HasPrefix(listRule, "-")
	if strings.HasPrefix(listRule, "-") || strings.HasPrefix(listRule, "+") {
		listRule = strings.TrimSpace(listRule[1:])
	}
	if listRule == "" {
		return nil, fmt.Errorf("书源 %s 缺少目录规则", source.BookSourceURL)
	}
	if tocURL == "" {
		tocURL = bookURL
	}

	type tocPage struct {
		url      string
		discover bool // 是否跟这一页的 nextTocUrl（只有"一页一页翻"的那条链上才翻）
	}
	queue := []tocPage{{url: tocURL, discover: true}}
	visited := make(map[string]bool)
	var chapters []*models.Chapter

	for len(queue) > 0 {
		page := queue[0]
		queue = queue[1:]
		pageKey := stripURLOptions(page.url)
		if pageKey == "" || visited[pageKey] {
			continue
		}
		visited[pageKey] = true

		resp, err := re.httpClient.Get(ctx, page.url, nil)
		if err != nil {
			return nil, err
		}
		doc, err := ruleparser.ParseHTML(resp.BodyString())
		if err != nil {
			return nil, err
		}

		nodes := doc.Select(listRule)
		for pageIndex, node := range nodes {
			chapter := &models.Chapter{BookURL: bookURL}
			if rule.ChapterName != nil {
				chapter.Title = strings.TrimSpace(node.Field(*rule.ChapterName, "text"))
			}
			if rule.ChapterURL != nil {
				raw := strings.TrimSpace(node.Field(*rule.ChapterURL, "href"))
				if raw != "" {
					raw = resolvePageURL(page.url, raw)
					lowered := strings.ToLower(raw)
					if strings.HasPrefix(lowered, "javascript:") ||
						strings.HasPrefix(lowered, "void(") {
						raw = ""
					}
				}
				chapter.URL = raw
			}
			chapter.IsVolume = tocRuleTruthy(node, rule.IsVolume)
			chapter.IsVip = tocRuleTruthy(node, rule.IsVip)
			chapter.IsPay = tocRuleTruthy(node, rule.IsPay)
			if rule.UpdateTime != nil && strings.TrimSpace(*rule.UpdateTime) != "" {
				updateTime := strings.TrimSpace(node.Field(*rule.UpdateTime, "text"))
				chapter.Tag = updateTime
				// 字数拆分：updateTime 尾巴上的 "3240 字" 进 wordCount，剩余进 tag
				// （卷标题不拆，对齐 Python chapters 的 tocCountWords 段）。
				if chapter.IsVolume == false && updateTime != "" {
					if match := legadoWordCountRE.FindStringSubmatch(updateTime); match != nil {
						chapter.WordCountText = strings.TrimSpace(match[1])
						chapter.Tag = strings.TrimSpace(strings.Replace(updateTime, match[0], "", 1))
					}
				}
			}
			if chapter.Title == "" {
				continue
			}
			if chapter.URL == "" {
				if chapter.IsVolume {
					chapter.URL = chapter.Title + strconv.Itoa(pageIndex)
				} else {
					chapter.URL = page.url
				}
			}
			chapters = append(chapters, chapter)
		}

		if page.discover && rule.NextTocURL != nil && strings.TrimSpace(*rule.NextTocURL) != "" {
			nextURL := strings.TrimSpace(doc.Field(*rule.NextTocURL, "href"))
			lowered := strings.ToLower(nextURL)
			if nextURL != "" && !strings.HasPrefix(lowered, "javascript:") &&
				!strings.HasPrefix(lowered, "void(") {
				// 对齐 Legado getStringList(isUrl=true)：任何无 scheme 的下一跳都按
				// **当页地址**拼成绝对地址（NetworkUtils.getAbsoluteURL 走 URL(base,
				// rel)，裸 "2.html" 也算相对）。不回拼的话 http 层拿到裸相对串直接
				// 报 unsupported protocol scheme，翻页链在第一跳就断。
				nextURL = resolvePageURL(page.url, nextURL)
				key := stripURLOptions(nextURL)
				pending := false
				for _, queued := range queue {
					if stripURLOptions(queued.url) == key {
						pending = true
						break
					}
				}
				if key != "" && !visited[key] && !pending {
					queue = append(queue, tocPage{url: nextURL, discover: true})
				}
			}
		}

		// 目录页全空且没有下一页了：旧源把 tocUrl 指错到"第一章"上时，规则目录
		// 其实是详情页——回详情页一次（对齐 Python 的 fallback 分支；详情页
		// 已经来过就不重复）。
		if len(chapters) == 0 && len(queue) == 0 {
			if fallbackKey := stripURLOptions(bookURL); fallbackKey != "" && !visited[fallbackKey] {
				queue = append(queue, tocPage{url: bookURL, discover: true})
			}
		}
	}

	if len(chapters) == 0 {
		return nil, fmt.Errorf("TocEmptyError: chapter list is empty")
	}

	// 顺序与去重（对齐 Python chapters 尾段）：非 "-" 规则先整体反转、按 url
	// 去重（保源顺序里的最后一条）、再转回源顺序编 index——净效果就是源顺序。
	if !reverseRule {
		reverseChapters(chapters)
	}
	seen := make(map[string]bool, len(chapters))
	unique := make([]*models.Chapter, 0, len(chapters))
	for _, chapter := range chapters {
		if seen[chapter.URL] {
			continue
		}
		seen[chapter.URL] = true
		unique = append(unique, chapter)
	}
	reverseChapters(unique)
	for index, chapter := range unique {
		chapter.Index = index
	}
	return unique, nil
}

func reverseChapters(items []*models.Chapter) {
	for i, j := 0, len(items)-1; i < j; i, j = i+1, j-1 {
		items[i], items[j] = items[j], items[i]
	}
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
