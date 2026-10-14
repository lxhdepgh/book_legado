package ruleparser

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/PuerkitoBio/goquery"
	"github.com/antchfx/htmlquery"
	"github.com/tidwall/gjson"
	"golang.org/x/net/html"
)

// ParseHTML 解析 HTML 为 Document（对齐 BeautifulSoup）
func ParseHTML(htmlStr string) (*Document, error) {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(htmlStr))
	if err != nil {
		return nil, fmt.Errorf("解析 HTML 失败: %w", err)
	}
	return &Document{doc: doc}, nil
}

// Document HTML 文档包装器
type Document struct {
	doc *goquery.Document
}

// Select CSS 选择器（对齐 soupsieve）
func (d *Document) Select(selector string) []*Node {
	var nodes []*Node
	d.doc.Find(selector).Each(func(i int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes
}

// SelectOne 选择第一个匹配
func (d *Document) SelectOne(selector string) *Node {
	sel := d.doc.Find(selector).First()
	if sel.Length() == 0 {
		return nil
	}
	return &Node{sel: sel}
}

// Node DOM 节点包装器
type Node struct {
	sel *goquery.Selection
}

// Select 子节点 CSS 选择器
func (n *Node) Select(selector string) []*Node {
	var nodes []*Node
	n.sel.Find(selector).Each(func(i int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes
}

// Attr 获取属性
func (n *Node) Attr(name string) string {
	val, _ := n.sel.Attr(name)
	return val
}

// Text 获取文本内容（对齐 Jsoup text()）
func (n *Node) Text() string {
	return n.sel.Text()
}

// Html 获取 HTML 内容
func (n *Node) Html() (string, error) {
	return n.sel.Html()
}

// XPath XPath 查询（对齐 lxml）
func (d *Document) XPath(xpathExpr string) []*XPathNode {
	htmlContent, _ := d.doc.Find("html").First().Html()
	doc, err := htmlquery.Parse(strings.NewReader(htmlContent))
	if err != nil {
		return nil
	}
	nodes := htmlquery.Find(doc, xpathExpr)
	var result []*XPathNode
	for _, node := range nodes {
		result = append(result, &XPathNode{node: node})
	}
	return result
}

// XPathNode XPath 节点
type XPathNode struct {
	node *html.Node
}

// Text 获取文本
func (n *XPathNode) Text() string {
	return htmlquery.InnerText(n.node)
}

// ParseJSONPath 解析 JSONPath（对齐 jsonpath-ng）
func ParseJSONPath(jsonStr, path string) []interface{} {
	result := gjson.Get(jsonStr, path)
	if !result.Exists() {
		return nil
	}

	// 判断是数组还是单值
	if result.IsArray() {
		arr := result.Array()
		values := make([]interface{}, len(arr))
		for i, v := range arr {
			values[i] = v.Value()
		}
		return values
	}

	return []interface{}{result.Value()}
}

// StripHTML 去除 HTML 标签（对齐 Python _strip_html_tags）
func StripHTML(htmlStr string) string {
	text := htmlStr
	// 移除 &nbsp;
	text = strings.ReplaceAll(text, "&nbsp;", " ")
	// 移除常见块级标签为换行
	blockTags := []string{"div", "p", "br", "hr", "h1", "h2", "h3", "article", "dd", "dl"}
	for _, tag := range blockTags {
		openPattern := fmt.Sprintf("</?%s[^>]*>", tag)
		text = replaceRegex(text, openPattern, "\n")
	}
	// 移除所有剩余标签
	text = replaceRegex(text, "<[^>]+>", "")
	// 清理多余空白
	text = strings.TrimSpace(text)
	return text
}

// HTMLToText 将 HTML 转换为纯文本
func HTMLToText(htmlStr string) string {
	return StripHTML(htmlStr)
}

// Helper: 简单正则替换
func replaceRegex(text, pattern, replacement string) string {
	re := regexp.MustCompile(pattern)
	return re.ReplaceAllString(text, replacement)
}

// Helper: 安全获取 HTML
func safeHtml(s *goquery.Selection) string {
	html, err := s.Html()
	if err != nil {
		return ""
	}
	return html
}
