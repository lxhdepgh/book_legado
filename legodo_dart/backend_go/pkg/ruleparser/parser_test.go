package ruleparser

// Legado 规则分段的语义测试（对齐 Python jsoup_values / _jsoup_part 的实跑结论）：
//   - LIST 上下文每段都是选择器（`class.read@li@a` → `.read li a`）；
//   - FIELD 上下文最后一段**永远**是 text/html/ownText/属性名，从不做选择器；
//   - 单段裸名（href/text/title）= 当前节点直接读（真源 chapterUrl="href" 的形状）；
//   - `@` 在方括号/引号里不是分段点；`a[href*="@qq.com"]` 整串是一条选择器。
// 真源语料（qbmfxs 的 ".catalog .list li a"、lvsexs 的 "class.read@li@a"）在
// 917 源普查里各只此一种拼法，回归先钉这两条 + 真语料会踩的分支。

import (
	"strings"
	"testing"
)

const parserFixture = `<html><body>
<div class="catalog"><ul class="list">
<li><a href="/book/1/1.html">第一章</a></li>
<li><a href="/book/1/2.html">第二章</a></li>
</ul></div>
<div class="read"><ul>
<li><a href="/x/1.html">甲</a></li>
<li><a href="/x/2.html">乙</a></li>
</ul></div>
<a class="toc" href="toc.html">目录</a>
<a class="next" href="page2.html">下一页</a>
<div class="ps"><p>第一段</p><p>第二段</p></div>
<dt><a>x</a>直连文本</dt>
<a href="mailto:a@qq.com">联系</a>
<span data-x="a1b2" hidden>下一章</span>
</body></html>`

func mustDoc(t *testing.T) *Document {
	t.Helper()
	doc, err := ParseHTML(parserFixture)
	if err != nil {
		t.Fatalf("ParseHTML: %v", err)
	}
	return doc
}

func TestSelectListContextNormalization(t *testing.T) {
	doc := mustDoc(t)

	// 真源 qbmfxs：普通 CSS 原样通过。
	plain := doc.Select(".catalog .list li a")
	if len(plain) != 2 || plain[0].Text() != "第一章" {
		t.Fatalf("普通 CSS 链选炸了：n=%d", len(plain))
	}

	// 真源 lvsexs：`class.read@li@a` 在 LIST 上下文是三层选择器。
	legado := doc.Select("class.read@li@a")
	if len(legado) != 2 || legado[1].Text() != "乙" {
		t.Fatalf("class. 前缀链选炸了：n=%d", len(legado))
	}
	if legado[0].Attr("href") != "/x/1.html" {
		t.Errorf("href 不对：%q", legado[0].Attr("href"))
	}

	// 下标拼法：`.1` → :nth-of-type(2)。
	paras := doc.Select("div.ps p.1")
	if len(paras) != 1 || paras[0].Text() != "第二段" {
		t.Fatalf("p.1 下标归一炸了：n=%d", len(paras))
	}
}

func TestSelectorChainNormalizationShapes(t *testing.T) {
	cases := map[string]string{
		"class.read@li@a": ".read li a",
		"class.vip":       ".vip",
		"id.main":         "#main",
		"tag.li":          "li",
		"p.1":             "p:nth-of-type(2)",
		"@css:.foo > .bar": ".foo > .bar",
		"ul.list li a":    "ul.list li a",
	}
	for rule, want := range cases {
		if got := normalizeSelectorChain(rule); got != want {
			t.Errorf("normalizeSelectorChain(%q)=%q want %q", rule, got, want)
		}
	}
}

func TestSplitFieldRuleShapes(t *testing.T) {
	cases := []struct {
		rule, selector, attr string
	}{
		// 单段裸名：当前节点直接读（真源 chapterUrl="href" / chapterName="text"）。
		{"href", "", "href"},
		{"text", "", "text"},
		{"title", "", "title"},
		// 多段：前 n-1 段是选择器链，尾段永远是 text/属性名（哪怕它长得像选择器）。
		{"a.toc@href", "a.toc", "href"},
		{"class.detail@class.name@text", ".detail .name", "text"},
		{"class.detail@h1", ".detail", "h1"},
		// `@` 在方括号/引号里不是分段点。
		{`a[href*="@qq.com"]`, `a[href*="@qq.com"]`, ""},
		{`a[href*="@qq.com"]@text`, `a[href*="@qq.com"]`, "text"},
		// text. 前缀 → contains 伪类。
		{"text.下一页@href", `*:contains("下一页")`, "href"},
	}
	for _, c := range cases {
		selector, attr := SplitFieldRule(c.rule)
		if selector != c.selector || attr != c.attr {
			t.Errorf("SplitFieldRule(%q)=(%q,%q) want (%q,%q)",
				c.rule, selector, attr, c.selector, c.attr)
		}
	}
}

func TestFieldExtraction(t *testing.T) {
	doc := mustDoc(t)

	// 文档级："选择器链@属性"。
	if got := doc.Field("class.toc@href", "href"); got != "toc.html" {
		t.Errorf("class.toc@href=%q", got)
	}
	if got := doc.Field("a.next@text", "text"); got != "下一页" {
		t.Errorf("a.next@text=%q", got)
	}
	// 裸选择器单段（宽容回退：旧 Go 行为，真 Legado getString 也吃这种）——
	// 语料普查里没有这种真源规则，只是别把 `.title` 选崩。
	if got := doc.Field(".toc", ""); got != "目录" {
		t.Errorf(".toc=%q", got)
	}
	// ownText：只取直接文本子节点（不递归 <a> 里那份）。
	if got := doc.Select("dt")[0].Field("ownText", ""); got != "直连文本" {
		t.Errorf("ownText=%q", got)
	}
	// text. 前缀 + @href（真源 nextTocUrl="text.下一页@href" 的形状）。
	if got := doc.Field("text.下一页@href", "href"); got != "page2.html" {
		t.Errorf("text.下一页@href=%q", got)
	}

	// 节点级：单段裸名在当前节点上读（chapterUrl="href" 的语义靠这条）。
	node := doc.Select(".catalog .list li")[0]
	if got := node.Field("a@text", "text"); got != "第一章" {
		t.Errorf("节点级 a@text=%q", got)
	}
	if got := node.Field("a@href", "href"); got != "/book/1/1.html" {
		t.Errorf("节点级 a@href=%q", got)
	}
	link := doc.Select(".catalog .list li a")[1]
	if got := link.Field("href", "href"); got != "/book/1/2.html" {
		t.Errorf("裸 href 应直读当前节点：%q", got)
	}
	if got := link.Field("text", "text"); got != "第二章" {
		t.Errorf("裸 text 应直读当前节点：%q", got)
	}
	// 空规则不猜。
	if got := link.Field("", ""); got != "" {
		t.Errorf("空规则应回空：%q", got)
	}
}

// TestApplyRegexSuffixOracle 逐条对齐 Python _apply_regex_suffix 的当场实测
// （2026-10-16 在 legodo_booksource_check 环境直跑 Python 引擎读数）：
// 4 段 replace-first 是在**命中文本本身**上 sub count=1，不是全文替换。
func TestApplyRegexSuffixOracle(t *testing.T) {
	cases := []struct {
		value, rule, want string
	}{
		{"下一页下一页", "##下一页##第N页##", "第N页"},
		{"page2.html", `##(?<=pag)e##E##`, "e"},
		{"aaXaa", "##aa##b##", "b"},
		{"page2.html", "##zzz##X##", ""},
		{"page2.html", "##e##E", "pagE2.html"},
		{"page2.html", "##2", "page.html"},
		{"page2.html", "##2##", "page.html"},
		{"a(bc", "##(##X##", "X"},
		{"a(bc", "##(##X", "aXbc"},
	}
	for _, c := range cases {
		pieces := strings.Split("x"+c.rule, "##")
		if got := ApplyRegexSuffix(c.value, pieces); got != c.want {
			t.Errorf("ApplyRegexSuffix(%q, %q)=%q want %q", c.value, c.rule, got, c.want)
		}
	}
}

// TestFieldRegexSuffixThroughField `##` 后缀经 Field 全管线（选择器→取值→正则）。
func TestFieldRegexSuffixThroughField(t *testing.T) {
	doc := mustDoc(t)
	if got := doc.Field("a.next@text##下一页##第N页##", ""); got != "第N页" {
		t.Errorf("4 段 replace-first：%q", got)
	}
	if got := doc.Field("a.next@href##e##E", ""); got != "pagE2.html" {
		t.Errorf("3 段全局替换：%q", got)
	}
	if got := doc.Field("a.next@href##2", ""); got != "page.html" {
		t.Errorf("2 段删除：%q", got)
	}
	// 正则里含 `##` 之外的 Java 语法（lookbehind）不炸、按 oracle 口径出值。
	if got := doc.Field(`a.next@href##(?<=pag)e##E##`, ""); got != "e" {
		t.Errorf("lookbehind 4 段：%q", got)
	}
}

// TestFieldCandidateFallback `||` 候选惰性：首组有值即返回，不跑后续。
func TestFieldCandidateFallback(t *testing.T) {
	doc := mustDoc(t)
	if got := doc.Field(".missing@text || class.toc@text", ""); got != "目录" {
		t.Errorf("|| 回退：%q", got)
	}
	// 首组命中选择器但值为空（ownText 无直接文本）→ 继续候选。
	if got := doc.Field("div.ps@ownText || class.toc@href", ""); got != "toc.html" {
		t.Errorf("|| 空值继续回退：%q", got)
	}
	// 方括号里的 `||` 不是候选分隔点。
	if got := doc.Field(`a[href*="||"]@href || class.toc@href`, ""); got != "toc.html" {
		t.Errorf("括号内 || 不应切分：%q", got)
	}
	parts := splitBalancedFields(`a[href*='||']@href || .b@text ||.c`, "||")
	if len(parts) != 3 || parts[1] != ".b@text" || parts[2] != ".c" {
		t.Errorf("splitBalancedFields=%q", parts)
	}
}

// TestFieldUnionOperator `&&` 联合经 Evaluate 管线拼接（Field 取首值）。
func TestFieldUnionOperator(t *testing.T) {
	doc := mustDoc(t)
	if got := doc.Field("class.toc@href && a.next@href", ""); got != "toc.html" {
		t.Errorf("&& 联合取首值：%q", got)
	}
}

// TestFieldHtmlOuterSerialization `@html`/`@all` 都是外序列化（含节点自身标签），
// `@text` 是块边界空格相连的 jsoup 文本——oracle 实测 `div.ps@html` 返回
// `'<div class="ps">…</div>'`（含 div 本身）而非 inner。
func TestFieldHtmlOuterSerialization(t *testing.T) {
	doc := mustDoc(t)
	if got := doc.Field("div.ps@text", ""); got != "第一段 第二段" {
		t.Errorf("div.ps@text=%q", got)
	}
	html := doc.Field("div.ps@html", "html")
	if !strings.HasPrefix(html, `<div class="ps"`) || !strings.HasSuffix(html, "</div>") {
		t.Errorf("div.ps@html 应外序列化含自身标签：%q", html)
	}
	all := doc.Field("div.ps@all", "html")
	if !strings.HasPrefix(all, `<div class="ps"`) || !strings.HasSuffix(all, "</div>") {
		t.Errorf("div.ps@all 应外序列化含自身标签：%q", all)
	}
	// @html 摘 script/style（内联脚本不该以纯文本混进正文）。
	doc2, err := ParseHTML(`<html><body><div class="c"><script>var a=1;</script><p>正文</p></div></body></html>`)
	if err != nil {
		t.Fatalf("ParseHTML: %v", err)
	}
	clean := doc2.Field("div.c@html", "html")
	if strings.Contains(clean, "var a=1") || !strings.Contains(clean, "<p>正文</p>") {
		t.Errorf("div.c@html 应摘脚本保留正文：%q", clean)
	}
}

// TestFieldAttrRegexTail `@attr:regex(p)` 尾缀（对齐 jsoup_values：fullmatch
// 优先、回落 search、组 1 优先）——期望值 2026-10-16 Python oracle 实测。
func TestFieldAttrRegexTail(t *testing.T) {
	doc := mustDoc(t)
	if got := doc.Field(`a.next@href:regex('.+?(\d)')`, ""); got != "2" {
		t.Errorf("@href:regex fullmatch 组 1：%q", got)
	}
	if got := doc.Field(`a.next@href:regex('[a-z]+')`, ""); got != "page" {
		t.Errorf("@href:regex 回落 search 无组取整段：%q", got)
	}
	// fullmatch 优先的可观察差：`.*?(\d+)` 对 a1b2 fullmatch 取 "2"，纯 search 取 "1"。
	if got := doc.Field(`span@data-x:regex(".*?(\d+)")`, ""); got != "2" {
		t.Errorf("@data-x:regex fullmatch 优先：%q", got)
	}
}
