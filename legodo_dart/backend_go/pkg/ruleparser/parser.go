package ruleparser

import (
	"fmt"
	"regexp"
	"strconv"
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

// Select CSS 选择器（对齐 soupsieve）。
//
// 选择器串先过 Legado 归一：顶层 `@` 分段（`class.read@li@a` 是三层选择器链）、
// `class.`/`id.`/`tag.`/`text.` 前缀、`p.1` 下标——详见 normalizeSelectorChain。
func (d *Document) Select(selector string) []*Node {
	var nodes []*Node
	d.doc.Find(normalizeSelectorChain(selector)).Each(func(i int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes
}

// SelectOne 选择第一个匹配
func (d *Document) SelectOne(selector string) *Node {
	sel := d.doc.Find(normalizeSelectorChain(selector)).First()
	if sel.Length() == 0 {
		return nil
	}
	return &Node{sel: sel}
}

// Node DOM 节点包装器
type Node struct {
	sel *goquery.Selection
}

// Select 子节点 CSS 选择器（同 Document.Select 的归一）
func (n *Node) Select(selector string) []*Node {
	var nodes []*Node
	n.sel.Find(normalizeSelectorChain(selector)).Each(func(i int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes
}

// ---------------------------------------------------------------------------
// Legado 规则分段（对齐 Python _jsoup_part / split_rule_expression）
// ---------------------------------------------------------------------------

// splitTopLevel 在**方括号/圆括号/引号之外**按 sep 切分。
// 判据与 Legado RuleAnalyzer.splitRule/chompCodeBalanced 一致：`a[href*="@qq.com"]`
// 里的 @ 在属性选择器内部，不是分段点。
func splitTopLevel(value string, sep byte) []string {
	parts := make([]string, 0, 2)
	depth := 0
	var quote byte
	start := 0
	for i := 0; i < len(value); i++ {
		c := value[i]
		if quote != 0 {
			if c == '\\' {
				i++
			} else if c == quote {
				quote = 0
			}
			continue
		}
		switch c {
		case '\'', '"':
			quote = c
		case '[', '(':
			depth++
		case ']', ')':
			if depth > 0 {
				depth--
			}
		case sep:
			if depth == 0 {
				parts = append(parts, value[start:i])
				start = i + 1
			}
		}
	}
	parts = append(parts, value[start:])
	return parts
}

var (
	// `.1` 这种 Jsoup 下标（Python: \.(\d+)(?=\s|[>+~]|$) → :nth-of-type(N+1)）
	nthIndexRE = regexp.MustCompile(`\.(\d+)(\s|[>+~]|$)`)
	// 裸名（可作属性名/text/html/ownText 读的整词）
	plainNameRE = regexp.MustCompile(`^[A-Za-z_:][\w:.-]*$`)
)

// normalizeSelectorPart 归一单个选择器段（对齐 Python _jsoup_part 的前缀与下标分支）。
func normalizeSelectorPart(part string) string {
	part = strings.TrimSpace(part)
	switch {
	case strings.HasPrefix(part, "class."):
		return "." + strings.ReplaceAll(strings.TrimSpace(part[len("class."):]), " ", ".")
	case strings.HasPrefix(part, "id."):
		return "#" + strings.TrimSpace(part[len("id."):])
	case strings.HasPrefix(part, "tag."):
		return strings.TrimSpace(part[len("tag."):])
	case strings.HasPrefix(part, "text."):
		escaped := strings.ReplaceAll(part[len("text."):], `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		return `*:contains("` + escaped + `")`
	}
	part = nthIndexRE.ReplaceAllStringFunc(part, func(match string) string {
		sub := nthIndexRE.FindStringSubmatch(match)
		index, _ := strconv.Atoi(sub[1])
		return fmt.Sprintf(":nth-of-type(%d)%s", index+1, sub[2])
	})
	part = strings.TrimSuffix(part, "./")
	part = strings.TrimSuffix(part, ".")
	return part
}

// normalizeSelectorChain 把一条 Legado 选择器规则归一成 goquery 能吃的 CSS 链：
// 顶层每段各归一后以空格相接（`class.read@li@a` → `.read li a`）。
// 不含归一特征的普通 CSS 原样通过（`ul.list li a` 不变）。
func normalizeSelectorChain(rule string) string {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return ""
	}
	// `@css:` 前缀：整串就是 CSS，剥掉标记即可（段内不再按 @ 拆）。
	if strings.HasPrefix(strings.ToLower(rule), "@css:") {
		return strings.TrimSpace(rule[len("@css:"):])
	}
	segments := splitTopLevel(rule, '@')
	parts := make([]string, 0, len(segments))
	for _, seg := range segments {
		if normalized := normalizeSelectorPart(seg); normalized != "" {
			parts = append(parts, normalized)
		}
	}
	return strings.Join(parts, " ")
}

// SplitFieldRule 把 Legado 的字段规则拆成「选择器链 + 尾段」（对齐
// getResultList/getResultLast 的拆分口径）：
//
//   - 顶层最后一个 @ 之后是尾段——它**永远是** text/html/ownText 或属性名，
//     从不再当选择器跑（`class.detail@class.name@text` → `.detail .name` + `text`）。
//   - 单段裸名（`href`/`text`/`title`）：选择器链为空、尾段是该名，在**当前节点**上读。
//   - 单段非裸名（`.title`/`a.name`）：整串按选择器链跑、尾段为空（取文本）。
func SplitFieldRule(rule string) (selector, attr string) {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return "", ""
	}
	segments := splitTopLevel(rule, '@')
	if len(segments) == 1 {
		seg := strings.TrimSpace(segments[0])
		if plainNameRE.MatchString(seg) {
			return "", seg
		}
		return normalizeSelectorChain(seg), ""
	}
	attr = strings.TrimSpace(segments[len(segments)-1])
	selectors := segments[:len(segments)-1]
	parts := make([]string, 0, len(selectors))
	for _, seg := range selectors {
		if normalized := normalizeSelectorPart(seg); normalized != "" {
			parts = append(parts, normalized)
		}
	}
	return strings.Join(parts, " "), attr
}

// Field 按「选择器链@尾段」字段规则取一个值（Legado AnalyzeRule 的核心子集）。
//
// 语义对齐 Python getResultLast：尾段是 text/html/ownText 时读文本，其余当属性名；
// 单段裸名在当前节点上直接读（真源 chapterUrl="href" 就是这个形状），单段选择器
// 则按选择器跑并取首个命中节点的文本（与旧行为一致）。
// `##` 正则后缀、`!` 排除、`&&/||` 组合不在这个子集里——语料与 E2E 用到哪种再补哪种。
func (n *Node) Field(rule, defAttr string) string {
	selector, attr := SplitFieldRule(rule)
	if attr == "" && selector == "" {
		// 空规则：不猜（对齐 Python `if not rule: return ""`）。
		return ""
	}
	if selector == "" {
		// 单段裸名：在当前节点上直接读（getResultLast 的单节点入口）。
		return pickLast([]*Node{n}, attr)
	}
	found := n.Select(selector)
	if len(found) == 0 {
		return ""
	}
	return pickLast(found, attr)
}

// Field 与 Node.Field 同语义；选择器段为空（纯 `@text`）在文档级没有"当前节点"，
// 回 ""（文档级规则形如 `.list@html`，选择器段不会为空）。
func (d *Document) Field(rule, defAttr string) string {
	selector, attr := SplitFieldRule(rule)
	if selector == "" {
		return ""
	}
	found := d.Select(selector)
	if len(found) == 0 {
		return ""
	}
	return pickLast(found, attr)
}

// pickLast 按 Legado getResultLast 的口径从命中集合里取一个尾段值
// （对齐 Python jsoup_values 的尾段分支）：
//
//   - 属性名：**逐个命中读属性，跳过没有/空白值的节点**再取首个非空——
//     `text.下一页@href` 命中的 html/body 没有 href，真值在最后的 <a> 上；
//     只看第一个命中的话取到的是空串（Python has_attr 跳过 / Jsoup attr() isBlank 跳过）。
//   - text/ownText：首个非空文本（jsoup 命中集合里空文本不进结果）。
//   - html/all：全部命中序列化后以 "\n" 相拼（Jsoup Elements.outerHtml() /
//     Python combined 都是这个形状）；@html 先摘 script/style 再序列化——
//     否则广告页的内联脚本会以纯文本形式混进正文。
func pickLast(nodes []*Node, attr string) string {
	switch attr {
	case "", "text":
		for _, node := range nodes {
			if value := node.Text(); strings.TrimSpace(value) != "" {
				return value
			}
		}
		return ""
	case "ownText":
		for _, node := range nodes {
			if value := node.ownText(); strings.TrimSpace(value) != "" {
				return value
			}
		}
		return ""
	case "html":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			if markup := node.cleanHtml(); markup != "" {
				rendered = append(rendered, markup)
			}
		}
		return strings.Join(rendered, "\n")
	case "all":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			markup, _ := node.Html()
			if markup != "" {
				rendered = append(rendered, markup)
			}
		}
		return strings.Join(rendered, "\n")
	default:
		for _, node := range nodes {
			if value := strings.TrimSpace(node.Attr(attr)); value != "" {
				return value
			}
		}
		return ""
	}
}

// cleanHtml 节点的 inner HTML，先摘掉 script/style 子节点（对齐 AnalyzeByJSoup
// 在 outerHtml 前 remove script/style 的那一步）。
func (n *Node) cleanHtml() string {
	clone := n.sel.Clone()
	clone.Find("script, style").Remove()
	html, _ := clone.Html()
	return html
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
