package ruleparser

// Legado 规则分段的语义测试（对齐 Python jsoup_values / _jsoup_part 的实跑结论）：
//   - LIST 上下文每段都是选择器（`class.read@li@a` → `.read li a`）；
//   - FIELD 上下文最后一段**永远**是 text/html/ownText/属性名，从不做选择器；
//   - 单段裸名（href/text/title）= 当前节点直接读（真源 chapterUrl="href" 的形状）；
//   - `@` 在方括号/引号里不是分段点；`a[href*="@qq.com"]` 整串是一条选择器。
// 真源语料（qbmfxs 的 ".catalog .list li a"、lvsexs 的 "class.read@li@a"）在
// 917 源普查里各只此一种拼法，回归先钉这两条 + 真语料会踩的分支。

import (
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
