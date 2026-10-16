package ruleparser

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/PuerkitoBio/goquery"
	"github.com/andybalholm/cascadia"
	"github.com/antchfx/htmlquery"
	"github.com/tidwall/gjson"
	"golang.org/x/net/html"
)

// SelectorError 选择器语法错误（cascadia 编译失败）。列表级规则遇到它显式报错
// （0 号原则：界面要能看到原因），字段级由 Field 回落空值。
type SelectorError struct {
	Selector string
	Err      error
}

func (e *SelectorError) Error() string {
	return fmt.Sprintf("无法解析选择器：%s（%v）", e.Selector, e.Err)
}

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

// Find 原始 CSS 选择器（不做 Legado 归一；jsoup 求值链与 JS DOM 面用）。
func (d *Document) Find(selector string) []*Node {
	nodes, _ := d.findRaw(selector)
	return nodes
}

func (d *Document) findRaw(selector string) ([]*Node, error) {
	matcher, err := cascadia.Compile(selector)
	if err != nil {
		return nil, &SelectorError{Selector: selector, Err: err}
	}
	var nodes []*Node
	d.doc.FindMatcher(matcher).Each(func(_ int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes, nil
}

// RootNodes 文档级选择上下文：整档作为起点（含 <html> 自身），
// 对齐 soup.select 在 BeautifulSoup 根上的行为。
func (d *Document) RootNodes() []*Node {
	return []*Node{{sel: d.doc.Selection}}
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

// Find 原始 CSS 选择器（不做 Legado 归一；只找后代、不含自身——对齐 soup.select）。
func (n *Node) Find(selector string) []*Node {
	nodes, _ := n.findRaw(selector)
	return nodes
}

func (n *Node) findRaw(selector string) ([]*Node, error) {
	matcher, err := cascadia.Compile(selector)
	if err != nil {
		return nil, &SelectorError{Selector: selector, Err: err}
	}
	var nodes []*Node
	n.sel.FindMatcher(matcher).Each(func(_ int, s *goquery.Selection) {
		nodes = append(nodes, &Node{sel: s})
	})
	return nodes, nil
}

// Children 直接子元素（对齐 Python `context.find_all(True, recursive=False)`）。
func (n *Node) Children() []*Node {
	nodes := make([]*Node, 0, 4)
	for _, item := range n.sel.Nodes {
		for child := item.FirstChild; child != nil; child = child.NextSibling {
			if child.Type == html.ElementNode {
				nodes = append(nodes, &Node{sel: &goquery.Selection{Nodes: []*html.Node{child}}})
			}
		}
	}
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

// Field 按 Legado 字段规则取一个值。多段/带运算符（`&&`/`%%`）/JSON/XPath 前缀
// 的规则走 Evaluate 管线（`##` 正则后缀、`||` 候选回退都吃）；单段沿用宽容旧
// 路径（`.title` 这种单段裸选择器读文本——真 Legado 会当属性名读空，语料里没有
// 这种规则，保留以防回归）。
func (n *Node) Field(rule, defAttr string) string {
	return fieldValue(n, rule, true)
}

// Field 与 Node.Field 同语义；文档级规则没有"当前节点"可裸读，纯 `@text`
// 这类无选择器段的值按 Python getResultLast 口径在文档整档上读。
func (d *Document) Field(rule, defAttr string) string {
	return fieldValue(d, rule, true)
}

// legacyFieldValue 单段规则的宽容取值：裸名直读当前节点属性/text，否则按选择器
// 链跑取首个非空（读文本）。
func legacyFieldValue(root interface{}, rule string) string {
	selector, attr := SplitFieldRule(rule)
	if attr == "" && selector == "" {
		return ""
	}
	switch typed := root.(type) {
	case *Node:
		if selector == "" {
			return pickLast([]*Node{typed}, attr)
		}
		found := typed.Select(selector)
		if len(found) == 0 {
			return ""
		}
		return pickLast(found, attr)
	case *Document:
		if selector == "" {
			return ""
		}
		found := typed.Select(selector)
		if len(found) == 0 {
			return ""
		}
		return pickLast(found, attr)
	}
	return ""
}

// pickLast 按 Legado getResultLast 的口径从命中集合里取一个尾段值
// （对齐 Python jsoup_values 的尾段分支）：
//
//   - 属性名：**逐个命中读属性，跳过没有/空白值的节点**再取首个非空——
//     `text.下一页@href` 命中的 html/body 没有 href，真值在最后的 <a> 上；
//     只看第一个命中的话取到的是空串（Python has_attr 跳过 / Jsoup attr() isBlank 跳过）。
//   - text/ownText：首个非空文本（jsoup 命中集合里空文本不进结果）。
//   - html/all：全部命中序列化后以 "\n" 相拼（Python 两个分支都走
//     `_jsoup_html_markup` = 含自身标签的外序列化——oracle 实测确认）；@html
//     先摘 script/style 再序列化，否则内联脚本会以纯文本形式混进正文。
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
			if value := node.OwnText(); strings.TrimSpace(value) != "" {
				return value
			}
		}
		return ""
	case "html":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			if markup := node.CleanHTML(); markup != "" {
				rendered = append(rendered, markup)
			}
		}
		return strings.Join(rendered, "\n")
	case "all":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			if markup := node.OuterHtml(); markup != "" {
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

// CleanHTML 外序列化，先摘掉 script/style 子节点（对齐 Python @html 分支：
// remove script/style 再 `_jsoup_html_markup`，oracle 实测为含自身标签的形态）。
func (n *Node) CleanHTML() string {
	clone := n.sel.Clone()
	clone.Find("script, style").Remove()
	rendered, err := goquery.OuterHtml(clone)
	if err != nil {
		return ""
	}
	return rendered
}

// OuterHtml 节点外序列化（@all 分支）。
func (n *Node) OuterHtml() string {
	rendered, err := goquery.OuterHtml(n.sel)
	if err != nil {
		return ""
	}
	return rendered
}

// TagName 元素名（小写，jsoup Element.tagName() 的形状）。
func (n *Node) TagName() string {
	return strings.ToLower(goquery.NodeName(n.sel))
}

// OwnText 取节点的**直接文本子节点**（不递归子元素），对齐 jsoup ownText()：
// 空白折叠、去零宽字符、<br> 记边界。
func (n *Node) OwnText() string {
	if len(n.sel.Nodes) == 0 {
		return ""
	}
	return jsoupOwnText(n.sel.Nodes[0])
}

// TextNodes 直接文本子节点（对齐 Python @textNodes：ASCII 修剪、非空、"\n" 相连）。
func (n *Node) TextNodes() string {
	if len(n.sel.Nodes) == 0 {
		return ""
	}
	return jsoupTextNodes(n.sel.Nodes[0])
}

// Attr 获取属性
func (n *Node) Attr(name string) string {
	val, _ := n.sel.Attr(name)
	return val
}

// Text 获取文本内容（对齐 Jsoup Element.text()：块级边界补空格、空白折叠、
// 去零宽字符、去 script/style——语义与 Python _jsoup_text 逐行对齐）。
func (n *Node) Text() string {
	parts := make([]string, 0, len(n.sel.Nodes))
	for _, node := range n.sel.Nodes {
		if text := jsoupText(node); text != "" {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, " ")
}

// Html 获取 HTML 内容（inner）
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

// pySpaceInner 是 Python `re` 在 str 模式下的 \s（Unicode 空白集）去掉外层方括号。
// RE2 的 \s 只有 ASCII，直接用会把 \x0b/\xa0/\u3000 当普通字符——而标签清洗的
// 换行归并和首尾修剪全靠这一集（正文段首的「　　」缩进就是这么来的）。
// Python 的 \d/\b 是 Unicode 感知、`\1` 反向引用 RE2 不支持，两处差异见下面注释。
const pySpaceInner = `\x09-\x0d\x1c-\x1f\x20\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`

var (
	nbspRunRE     = regexp.MustCompile(`(?:&nbsp;)+`)
	enspRunRE     = regexp.MustCompile(`&ensp;|&emsp;`)
	thinMarkRE    = regexp.MustCompile(`&thinsp;|&zwnj;|&zwj;`)
	blockTagRE    = regexp.MustCompile(`</?(?:div|p|br|hr|h\d|article|dd|dl)[^>]*>`)
	htmlCommentRE = regexp.MustCompile(`<!--[^>]*-->`)
	// RE2 无 lookahead：Python 的 `</?[a-zA-Z]+(?=[ >])[^<>]*>` 与之等价写成消费掉
	// 那个空格/尖括号（标签名后必跟空白或属性起始符才可能出现在真标签位上）。
	anyTagRE     = regexp.MustCompile(`</?[a-zA-Z]+[ >][^<>]*>`)
	nlWrapRunRE  = regexp.MustCompile(`[` + pySpaceInner + `]*\n+[` + pySpaceInner + `]*`)
	leadingNLRE  = regexp.MustCompile(`^[\n` + pySpaceInner + `]+`)
	trailingNLRE = regexp.MustCompile(`[\n` + pySpaceInner + `]+$`)
	// Python 用 `<(style|script)\b[^>]*>[\s\S]*?</\1\s*>` 一条搞定；RE2 没有 `\1`，
	// 拆成两条同一形状的模式，非贪婪与 IGNORECASE 语义不变。
	styleBlockRE  = regexp.MustCompile(`(?is)<style\b[^>]*>[\s\S]*?</style\s*>`)
	scriptBlockRE = regexp.MustCompile(`(?is)<script\b[^>]*>[\s\S]*?</script\s*>`)
)

// StripHTML 对齐 Python `_strip_html_tags`（九步顺序照抄，不做"优化"）：
// 空格实体归一 → 块级标签转换行 → 去注释 → 去剩余标签 → 换行归并成
// 「\n　　」→ 段首补「　　」→ 去尾部空白。注意它不删 style/script 正文，
// 那是 HTMLToText 的事。
func StripHTML(htmlStr string) string {
	text := nbspRunRE.ReplaceAllString(htmlStr, " ")
	text = enspRunRE.ReplaceAllString(text, " ")
	text = thinMarkRE.ReplaceAllString(text, "")
	text = blockTagRE.ReplaceAllString(text, "\n")
	text = htmlCommentRE.ReplaceAllString(text, "")
	text = anyTagRE.ReplaceAllString(text, "")
	text = nlWrapRunRE.ReplaceAllString(text, "\n　　")
	text = leadingNLRE.ReplaceAllString(text, "　　")
	return trailingNLRE.ReplaceAllString(text, "")
}

// HTMLToText 对齐 Python `html_to_text`：先按查看器的 HTML 渲染路径丢掉
// style/script 正文（Legado 的 Android 渲染器从不画这两块的内容，<useweb> 卡
// 整张样式表都靠它兜住），再走 _strip_html_tags。
func HTMLToText(htmlStr string) string {
	text := styleBlockRE.ReplaceAllString(htmlStr, "")
	text = scriptBlockRE.ReplaceAllString(text, "")
	return StripHTML(text)
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

// FirstElementNode 把一段 HTML 片段解析成文档序第一个元素节点（对齐 Python
// 列表管线物化分支的 `parse_markup(item).find()`——bs4 的 find() 无参调用
// 返回第一个后代 Tag）。整页字符串（`<html`/`<!doctype` 开头）按完整文档
// 解析取 <html>；片段走 HTML5 片段解析，避免 x/net/html 自动合成的
// html/body 包层顶到结果位上。
func FirstElementNode(markup string) *Node {
	trimmed := strings.TrimSpace(markup)
	lowered := strings.ToLower(trimmed)
	if strings.HasPrefix(lowered, "<html") || strings.HasPrefix(lowered, "<!doctype") {
		doc, err := html.Parse(strings.NewReader(markup))
		if err != nil {
			return nil
		}
		if found := firstElementIn(doc); found != nil {
			return wrapHTMLNode(found)
		}
		return nil
	}
	fragment, err := html.ParseFragment(strings.NewReader(markup), nil)
	if err != nil {
		return nil
	}
	for _, node := range fragment {
		if found := firstElementIn(node); found != nil {
			return wrapHTMLNode(found)
		}
	}
	return nil
}

func firstElementIn(node *html.Node) *html.Node {
	if node.Type == html.ElementNode {
		return node
	}
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		if found := firstElementIn(child); found != nil {
			return found
		}
	}
	return nil
}

func wrapHTMLNode(node *html.Node) *Node {
	return &Node{sel: &goquery.Selection{Nodes: []*html.Node{node}}}
}
