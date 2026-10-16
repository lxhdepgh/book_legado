package ruleparser

import (
	"regexp"
	"strings"

	"golang.org/x/net/html"
)

// jsoup 文本端口：Jsoup Element.text()/ownText() 的空白语义逐行对齐
// Python legado_rule_parser 的 _jsoup_text/_jsoup_own_text。
//
// jsoup 在这些"块级格式化"标签边界补空格（Python _BLOCK_TAGS | _JSOUP_BLOCK_TAGS
// 的并集，注释注明该集合比 CSS 块级列表更宽：svg/math/script/style/title 等都算）。
var jsoupTextBoundaryTags = func() map[string]bool {
	names := []string{
		"address", "article", "aside", "blockquote", "body", "caption",
		"center", "dd", "details", "dir", "div", "dl", "dt", "fieldset",
		"figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
		"h5", "h6", "header", "hgroup", "hr", "html", "legend", "li",
		"main", "menu", "nav", "ol", "p", "pre", "section", "summary",
		"table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
		"head", "frameset", "script", "noscript", "style", "meta",
		"link", "title", "frame", "noframes", "colgroup", "col", "video",
		"audio", "canvas", "plaintext", "template", "svg", "math", "applet",
		"marquee", "listing",
		"ins", "del",
	}
	set := make(map[string]bool, len(names))
	for _, name := range names {
		set[name] = true
	}
	return set
}()

var jsoupWhitespaceRE = regexp.MustCompile(`[ \t\n\f\r]+`)

const asciiTrimCutset = "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09" +
	"\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18" +
	"\x19\x1a\x1b\x1c\x1d\x1e\x1f\x20"

// appendJsoupText 追加一段文本：空白折叠、去零宽字符、相邻已按空格收尾时吞掉
// 重复的前导空格（Python _append_jsoup_text）。
func appendJsoupText(buffer *strings.Builder, value string) {
	value = jsoupWhitespaceRE.ReplaceAllString(value, " ")
	value = strings.ReplaceAll(value, "\u200b", "")
	value = strings.ReplaceAll(value, "\u00ad", "")
	if value == "" {
		return
	}
	if strings.HasSuffix(buffer.String(), " ") && strings.HasPrefix(value, " ") {
		value = value[1:]
	}
	buffer.WriteString(value)
}

// jsoupBoundary 块边界：缓冲区非空且未以空格/不换行空格收尾时补一个空格。
func jsoupBoundary(buffer *strings.Builder) {
	current := buffer.String()
	if current == "" || strings.HasSuffix(current, " ") || strings.HasSuffix(current, "\u00a0") {
		return
	}
	buffer.WriteString(" ")
}

// jsoupText 对齐 jsoup Element.text()：块级边界补空格、空白折叠、跳过
// script/style 内容、首尾按 jsoup 的空白集修剪。
func jsoupText(node *html.Node) string {
	buffer := &strings.Builder{}
	var walk func(current *html.Node)
	walk = func(current *html.Node) {
		switch current.Type {
		case html.CommentNode, html.DoctypeNode:
			return
		case html.TextNode:
			parent := current.Parent
			if parent != nil && (strings.EqualFold(parent.Data, "script") ||
				strings.EqualFold(parent.Data, "style")) {
				return
			}
			appendJsoupText(buffer, current.Data)
			return
		case html.ElementNode:
			name := strings.ToLower(current.Data)
			block := jsoupTextBoundaryTags[name]
			if name == "br" || block {
				jsoupBoundary(buffer)
			}
			for child := current.FirstChild; child != nil; child = child.NextSibling {
				walk(child)
			}
			if block {
				jsoupBoundary(buffer)
			}
		default:
			for child := current.FirstChild; child != nil; child = child.NextSibling {
				walk(child)
			}
		}
	}
	walk(node)
	return strings.Trim(buffer.String(), " \t\n\f\r\u00a0")
}

// jsoupOwnText 对齐 jsoup Element.ownText()：只看直接文本子节点与 <br> 边界。
func jsoupOwnText(node *html.Node) string {
	buffer := &strings.Builder{}
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		switch child.Type {
		case html.CommentNode:
			continue
		case html.TextNode:
			appendJsoupText(buffer, child.Data)
		case html.ElementNode:
			if strings.EqualFold(child.Data, "br") {
				jsoupBoundary(buffer)
			}
		}
	}
	return strings.Trim(buffer.String(), " \t\n\f\r\u00a0")
}

// jsoupTextNodes 对齐 Python @textNodes 分支：直接文本子节点（含注释）逐个
// ASCII 修剪、非空者以 "\n" 相连。
func jsoupTextNodes(node *html.Node) string {
	parts := make([]string, 0, 4)
	for child := node.FirstChild; child != nil; child = child.NextSibling {
		if child.Type != html.TextNode && child.Type != html.CommentNode {
			continue
		}
		if trimmed := strings.Trim(child.Data, asciiTrimCutset); trimmed != "" {
			parts = append(parts, trimmed)
		}
	}
	return strings.Join(parts, "\n")
}
