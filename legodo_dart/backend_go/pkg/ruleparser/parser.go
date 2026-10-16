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

// SplitFieldRule 把 Legado 的 `选择器@属性` 字段规则拆成两段。
//
// 判据：最后一个 @ 之后是**纯属性名**才算属性段。真源里
// `a[href*="@qq.com"]` 这种 @ 写在属性选择器里的规则不能被误拆成
// 选择器 `a[href*="` + 属性 `qq.com"]`——所以属性段含 CSS 结构字符
// （方括号、引号、等号、括号、斜杠、点、井号、空格、通配/组合符）时整串仍是选择器。
func SplitFieldRule(rule string) (selector, attr string) {
	rule = strings.TrimSpace(rule)
	idx := strings.LastIndex(rule, "@")
	if idx < 0 {
		return rule, ""
	}
	attr = strings.TrimSpace(rule[idx+1:])
	if attr == "" || strings.ContainsAny(attr, `[]"=()#./*^$~| ,`) {
		return rule, ""
	}
	return strings.TrimSpace(rule[:idx]), attr
}

// Field 按「选择器@属性」字段规则取一个值（Legado AnalyzeRule 的核心子集）。
//
// 支持：`selector@text` / `selector@html` / `selector@ownText` / `selector@属性名`，
// 以及不带 @ 的裸选择器（属性用 defAttr：文本类字段传 "text"、url 类传 "href"/"src"）。
// `@text`（选择器段为空）取当前节点本身；取不到时回 ""。
// `##` 正则后缀、`!` 排除、`&&/||` 组合不在这个子集里——语料与 E2E 用到哪种再补哪种。
func (n *Node) Field(rule, defAttr string) string {
	selector, attr := SplitFieldRule(rule)
	if attr == "" {
		attr = defAttr
	}
	target := n
	if selector != "" {
		found := n.Select(selector)
		if len(found) == 0 {
			return ""
		}
		target = found[0]
	}
	return target.pick(attr)
}

// Field 与 Node.Field 同语义；选择器段为空（纯 `@text`）在文档级没有"当前节点"，
// 回 ""（文档级规则形如 `.list@html`，选择器段不会为空）。
func (d *Document) Field(rule, defAttr string) string {
	selector, attr := SplitFieldRule(rule)
	if attr == "" {
		attr = defAttr
	}
	if selector == "" {
		return ""
	}
	found := d.Select(selector)
	if len(found) == 0 {
		return ""
	}
	return found[0].pick(attr)
}

func (n *Node) pick(attr string) string {
	switch attr {
	case "", "text":
		return n.Text()
	case "html":
		html, _ := n.Html()
		return html
	case "ownText":
		return n.ownText()
	default:
		return n.Attr(attr)
	}
}

// ownText 取节点的**直接文本子节点**（不递归子元素），对齐 jsoup ownText()。
func (n *Node) ownText() string {
	if len(n.sel.Nodes) == 0 {
		return ""
	}
	var b strings.Builder
	for child := n.sel.Nodes[0].FirstChild; child != nil; child = child.NextSibling {
		if child.Type == html.TextNode {
			b.WriteString(child.Data)
		}
	}
	return b.String()
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
