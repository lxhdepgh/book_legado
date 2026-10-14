package engine

import (
	"context"
	"fmt"
	"net/url"
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

// SearchBook 搜索书籍（对齐 Legado WebBook.search）
func (re *RuleEngine) SearchBook(ctx context.Context, source *models.BookSource, keyword string) ([]*models.BookItem, error) {
	rule := source.GetSearchRule()
	if rule.BookList == nil {
		return nil, fmt.Errorf("书源 %s 缺少搜索规则", source.BookSourceURL)
	}

	// 构建搜索 URL
	searchURL, err := re.buildSearchURL(source, keyword)
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
		book := re.extractBookInfo(node, rule, source)
		if book != nil {
			books = append(books, book)
		}
	}

	return books, nil
}

// buildSearchURL 构建搜索 URL（对齐 Legado AnalyzeUrl）
func (re *RuleEngine) buildSearchURL(source *models.BookSource, keyword string) (string, error) {
	searchURL := ""
	if source.SearchURL != nil {
		searchURL = *source.SearchURL
	} else {
		return "", fmt.Errorf("未配置搜索 URL")
	}

	// 替换关键词占位符
	searchURL = strings.ReplaceAll(searchURL, "{{key}}", url.QueryEscape(keyword))
	searchURL = strings.ReplaceAll(searchURL, "searchKey", url.QueryEscape(keyword))

	// 执行 JS（如果有）
	if source.LoginCheckJS != nil && strings.Contains(searchURL, "@js:") {
		// TODO: 执行 URL 中的 JS 表达式
	}

	return searchURL, nil
}

// extractBookInfo 从 DOM 节点提取书籍信息
func (re *RuleEngine) extractBookInfo(node *ruleparser.Node, rule *models.SearchRule, source *models.BookSource) *models.BookItem {
	book := &models.BookItem{
		Origin:     source.BookSourceURL,
		OriginName: source.BookSourceName,
	}

	// 书名
	if rule.Name != nil {
		nameNodes := node.Select(*rule.Name)
		if len(nameNodes) > 0 {
			book.Name = nameNodes[0].Text()
		}
	}

	// 作者
	if rule.Author != nil {
		authorNodes := node.Select(*rule.Author)
		if len(authorNodes) > 0 {
			book.Author = re.formatAuthor(authorNodes[0].Text())
		}
	}

	// 简介
	if rule.Intro != nil {
		introNodes := node.Select(*rule.Intro)
		if len(introNodes) > 0 {
			book.Intro = re.formatIntro(introNodes[0].Text())
		}
	}

	// 封面
	if rule.CoverURL != nil {
		coverNodes := node.Select(*rule.CoverURL)
		if len(coverNodes) > 0 {
			book.CoverURL = coverNodes[0].Attr("src")
		}
	}

	// 书籍 URL
	if rule.BookURL != nil {
		urlNodes := node.Select(*rule.BookURL)
		if len(urlNodes) > 0 {
			book.URL = urlNodes[0].Attr("href")
		}
	}

	// 最新章节
	if rule.LastChapter != nil {
		chapterNodes := node.Select(*rule.LastChapter)
		if len(chapterNodes) > 0 {
			book.LastChapter = chapterNodes[0].Text()
		}
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
		nodes := doc.Select(*rule.Name)
		if len(nodes) > 0 {
			book.Name = nodes[0].Text()
		}
	}

	// 提取作者
	if rule.Author != nil {
		nodes := doc.Select(*rule.Author)
		if len(nodes) > 0 {
			book.Author = re.formatAuthor(nodes[0].Text())
		}
	}

	// 提取简介
	if rule.Intro != nil {
		nodes := doc.Select(*rule.Intro)
		if len(nodes) > 0 {
			htmlContent, _ := nodes[0].Html()
			book.Intro = re.formatIntro(htmlContent)
		}
	}

	// 提取封面
	if rule.CoverURL != nil {
		nodes := doc.Select(*rule.CoverURL)
		if len(nodes) > 0 {
			book.CoverURL = nodes[0].Attr("src")
		}
	}

	// 提取目录 URL
	if rule.TocURL != nil {
		nodes := doc.Select(*rule.TocURL)
		if len(nodes) > 0 {
			book.TocURL = nodes[0].Attr("href")
		}
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
			nameNodes := node.Select(*rule.ChapterName)
			if len(nameNodes) > 0 {
				chapter.Title = strings.TrimSpace(nameNodes[0].Text())
			}
		}

		// 章节 URL
		if rule.ChapterURL != nil {
			urlNodes := node.Select(*rule.ChapterURL)
			if len(urlNodes) > 0 {
				chapter.URL = urlNodes[0].Attr("href")
			}
		}

		// VIP 标识
		if rule.IsVip != nil {
			vipNodes := node.Select(*rule.IsVip)
			chapter.IsVip = len(vipNodes) > 0
		}

		chapters = append(chapters, chapter)
	}

	return chapters, nil
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
		nodes := doc.Select(*rule.Title)
		if len(nodes) > 0 {
			content.Title = strings.TrimSpace(nodes[0].Text())
		}
	}

	// 提取正文
	nodes := doc.Select(*rule.Content)
	if len(nodes) > 0 {
		htmlContent, _ := nodes[0].Html()
		content.Content = ruleparser.HTMLToText(htmlContent)
	}

	// 处理分页（subContent）
	if rule.SubContent != nil {
		nodes := doc.Select(*rule.SubContent)
		if len(nodes) > 0 {
			htmlContent, _ := nodes[0].Html()
			subContent := ruleparser.HTMLToText(htmlContent)
			content.SubContent = &subContent
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
