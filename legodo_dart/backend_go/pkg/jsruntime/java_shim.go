package jsruntime

import (
	"encoding/base64"
	"fmt"
	"reflect"
	"regexp"
	"strings"

	"github.com/dop251/goja"
	"github.com/legodo-booksource/legodo_go/pkg/ruleparser"
)

// installJavaShim 注入 Legado 的 Java 模拟面（对齐 Python legado_js 的书源规则子集）。
//
// 覆盖面按真库实测选（docs/找书分类浏览修复方案.md §3）：java.ajax/toast/log/
// refreshExplore/t/longToast/base64Encode/get/put + Packages.org.jsoup.Jsoup.parse
// 的 DOM 子集。§7 明确不做的调用点（searchBook/getElement/ajaxAll/host.call/
// cookie.*）定义为显式抛错——0 号原则：未覆盖形状不许静默回空。
func installJavaShim(vm *goja.Runtime, ctx *Context, variables map[string]interface{}) {
	unsupported := func(name string) error {
		return fmt.Errorf("%s 暂不支持（Go 引擎未实现）", name)
	}

	java := map[string]interface{}{
		"ajax": func(call goja.FunctionCall) goja.Value {
			target := call.Argument(0).String()
			var headers map[string]string
			if arg := call.Argument(1); !goja.IsUndefined(arg) && !goja.IsNull(arg) {
				headers = jsObjectToStringMap(vm, arg)
			}
			if ctx.Ajax == nil {
				return vm.ToValue("java.ajax 不可用：未配置 HTTP 回调")
			}
			body, err := ctx.Ajax(target, headers)
			if err != nil {
				// 对齐 Python _ajax：失败以错误文本返回（脚本常直接喂给 Jsoup/JSON）。
				return vm.ToValue(fmt.Sprintf("java.ajax 失败: %v", err))
			}
			return vm.ToValue(body)
		},
		"toast":          func(goja.FunctionCall) goja.Value { return goja.Undefined() },
		"longToast":      func(goja.FunctionCall) goja.Value { return goja.Undefined() },
		"log":            func(goja.FunctionCall) goja.Value { return goja.Undefined() },
		"refreshExplore": func(goja.FunctionCall) goja.Value { return goja.Undefined() },
		"t": func(call goja.FunctionCall) goja.Value {
			return vm.ToValue(call.Argument(0).String())
		},
		// 已知降级（设计稿 D4）：不做繁转简，原文直出。
		"t2s": func(call goja.FunctionCall) goja.Value {
			return vm.ToValue(call.Argument(0).String())
		},
		"base64Encode": func(call goja.FunctionCall) goja.Value {
			return vm.ToValue(base64.StdEncoding.EncodeToString([]byte(call.Argument(0).String())))
		},
		"get": func(call goja.FunctionCall) goja.Value {
			if value, ok := variables[call.Argument(0).String()]; ok {
				return vm.ToValue(value)
			}
			return goja.Null()
		},
		"put": func(call goja.FunctionCall) goja.Value {
			variables[call.Argument(0).String()] = call.Argument(1).Export()
			return goja.Undefined()
		},
	}
	for _, name := range []string{"searchBook", "getElement", "getElements", "ajaxAll"} {
		label := "java." + name
		java[name] = func(goja.FunctionCall) goja.Value {
			panic(vm.NewGoError(unsupported(label)))
		}
	}

	cookie := map[string]interface{}{}
	for _, name := range []string{"getKey", "setKey", "get", "put", "removeCookie", "getCookie"} {
		label := "cookie." + name
		cookie[name] = func(goja.FunctionCall) goja.Value {
			panic(vm.NewGoError(unsupported(label)))
		}
	}

	host := map[string]interface{}{
		"call": func(goja.FunctionCall) goja.Value {
			panic(vm.NewGoError(unsupported("host.call")))
		},
	}

	jsoup := map[string]interface{}{
		"parse": func(call goja.FunctionCall) goja.Value {
			doc, err := parseJsoupDocument(call.Argument(0).String())
			if err != nil {
				panic(vm.NewGoError(fmt.Errorf("Jsoup.parse 失败: %w", err)))
			}
			return vm.ToValue(doc)
		},
	}
	org := map[string]interface{}{
		"jsoup": map[string]interface{}{"Jsoup": jsoup},
	}
	packages := map[string]interface{}{"org": org}

	vm.Set("java", java)
	vm.Set("cookie", cookie)
	vm.Set("host", host)
	vm.Set("Packages", packages)
	vm.Set("org", org)
}

// jsObjectToStringMap 把 JS 对象收成 map[string]string（java.ajax 的第二参）。
func jsObjectToStringMap(vm *goja.Runtime, value goja.Value) map[string]string {
	object := value.ToObject(vm)
	if object == nil {
		return nil
	}
	result := make(map[string]string)
	for _, key := range object.Keys() {
		result[key] = object.Get(key).String()
	}
	return result
}

// ---------------------------------------------------------------------------
// Jsoup DOM 子集（对齐 org.jsoup.nodes.Document/Element/Elements 常用面）
// ---------------------------------------------------------------------------

// unquotedAttrValueRE 匹配无引号的属性选择器值：jsoup 接受 `[href^=/book/]`，
// 而 goquery/cascadia 只认带引号的值——不改写的话真源（twkan）的选择器直接不命中。
var unquotedAttrValueRE = regexp.MustCompile(`\[([A-Za-z_:][-A-Za-z0-9_:.]*)\s*([~|^$*]?=)\s*([^"'\]]+?)\s*\]`)

// quoteUnquotedAttrValues 只给无引号属性值补引号。JS 脚本里的选择器是标准 CSS，
// 不做 Legado 的 class./tag./@ 归一（那是规则文本的语法，不是 Jsoup 的）。
func quoteUnquotedAttrValues(selector string) string {
	return unquotedAttrValueRE.ReplaceAllStringFunc(selector, func(match string) string {
		parts := unquotedAttrValueRE.FindStringSubmatch(match)
		return fmt.Sprintf(`[%s%s"%s"]`, parts[1], parts[2], parts[3])
	})
}

type javaDocument struct {
	doc *ruleparser.Document
}

func parseJsoupDocument(html string) (*javaDocument, error) {
	doc, err := ruleparser.ParseHTML(html)
	if err != nil {
		return nil, err
	}
	return &javaDocument{doc: doc}, nil
}

func (d *javaDocument) Select(selector string) *javaElements {
	return &javaElements{nodes: d.doc.Find(quoteUnquotedAttrValues(selector))}
}

func (d *javaDocument) Text() string {
	return jsoupNodesText(d.doc.Find("html"))
}

func (d *javaDocument) Html() string {
	nodes := d.doc.Find("html")
	if len(nodes) == 0 {
		return ""
	}
	html, _ := nodes[0].Html()
	return html
}

func (d *javaDocument) OuterHtml() string {
	nodes := d.doc.Find("html")
	if len(nodes) == 0 {
		return ""
	}
	return nodes[0].OuterHtml()
}

func (d *javaDocument) Location() string { return "" }

type javaElements struct {
	nodes []*ruleparser.Node
}

func (e *javaElements) Size() int { return len(e.nodes) }

func (e *javaElements) Get(index int) *javaNode {
	if index < 0 || index >= len(e.nodes) {
		return &javaNode{}
	}
	return &javaNode{node: e.nodes[index]}
}

func (e *javaElements) First() *javaNode { return e.Get(0) }

func (e *javaElements) Last() *javaNode { return e.Get(len(e.nodes) - 1) }

func (e *javaElements) IsEmpty() bool { return len(e.nodes) == 0 }

// Text 对齐 jsoup Elements.text()：各元素文本以空格相连。
func (e *javaElements) Text() string { return jsoupNodesText(e.nodes) }

func (e *javaElements) OwnText() string {
	parts := make([]string, 0, len(e.nodes))
	for _, node := range e.nodes {
		if text := node.OwnText(); text != "" {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, " ")
}

func (e *javaElements) Attr(name string) string {
	if len(e.nodes) == 0 {
		return ""
	}
	return e.nodes[0].Attr(name)
}

func (e *javaElements) HasAttr(name string) bool {
	return len(e.nodes) > 0 && e.nodes[0].Attr(name) != ""
}

func (e *javaElements) Html() string {
	parts := make([]string, 0, len(e.nodes))
	for _, node := range e.nodes {
		html, _ := node.Html()
		parts = append(parts, html)
	}
	return strings.Join(parts, " ")
}

func (e *javaElements) OuterHtml() string {
	parts := make([]string, 0, len(e.nodes))
	for _, node := range e.nodes {
		parts = append(parts, node.OuterHtml())
	}
	return strings.Join(parts, " ")
}

// Select 对齐 jsoup Elements.select：在本集合每个元素内跑选择器并合并结果。
func (e *javaElements) Select(selector string) *javaElements {
	normalized := quoteUnquotedAttrValues(selector)
	merged := make([]*ruleparser.Node, 0, len(e.nodes))
	for _, node := range e.nodes {
		merged = append(merged, node.Find(normalized)...)
	}
	return &javaElements{nodes: merged}
}

type javaNode struct {
	node *ruleparser.Node
}

func (n *javaNode) Select(selector string) *javaElements {
	if n.node == nil {
		return &javaElements{}
	}
	return &javaElements{nodes: n.node.Find(quoteUnquotedAttrValues(selector))}
}

// Text 对齐 jsoup Element.text()：ruleparser.Node.Text 已是 jsoup 语义。
func (n *javaNode) Text() string {
	if n.node == nil {
		return ""
	}
	return n.node.Text()
}

func (n *javaNode) OwnText() string {
	if n.node == nil {
		return ""
	}
	return n.node.OwnText()
}

func (n *javaNode) Attr(name string) string {
	if n.node == nil {
		return ""
	}
	return n.node.Attr(name)
}

func (n *javaNode) HasAttr(name string) bool { return n.Attr(name) != "" }

func (n *javaNode) Html() string {
	if n.node == nil {
		return ""
	}
	html, _ := n.node.Html()
	return html
}

func (n *javaNode) OuterHtml() string {
	if n.node == nil {
		return ""
	}
	return n.node.OuterHtml()
}

func (n *javaNode) TagName() string {
	if n.node == nil {
		return ""
	}
	return n.node.TagName()
}

// jsoupNodesText 各元素归一文本以空格相连（jsoup Elements.text 的形状；
// ruleparser.Node.Text 已是块边界 + 空白折叠后的 jsoup 口径）。
func jsoupNodesText(nodes []*ruleparser.Node) string {
	parts := make([]string, 0, len(nodes))
	for _, node := range nodes {
		if text := node.Text(); text != "" {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, " ")
}

// elementListValue 把一段段 HTML 串绑定为元素列表对象（对齐 Python 引导脚本的
// `__elementList(markup)`：脚本对 result 直接链式 select/get/first）。
func elementListValue(markups []string) *javaElements {
	nodes := make([]*ruleparser.Node, 0, len(markups))
	for _, markup := range markups {
		if node := ruleparser.FirstElementNode(markup); node != nil {
			nodes = append(nodes, node)
		}
	}
	return &javaElements{nodes: nodes}
}

// NormalizeExport 把 JS 出口值规整成管线能消费的形状：DOM 包装对象转 HTML 串
// （对齐 Python `__markup` 物化分支的 str() 口径）、数组/字典递归、Go 方法
// （goja 把对象属性里的函数导出为 func 值）丢弃。
func NormalizeExport(value interface{}) interface{} {
	switch typed := value.(type) {
	case *javaElements:
		out := make([]interface{}, 0, len(typed.nodes))
		for _, node := range typed.nodes {
			out = append(out, node.OuterHtml())
		}
		return out
	case *javaNode:
		return typed.OuterHtml()
	case *javaDocument:
		return typed.OuterHtml()
	case []interface{}:
		out := make([]interface{}, 0, len(typed))
		for _, item := range typed {
			out = append(out, NormalizeExport(item))
		}
		return out
	case map[string]interface{}:
		out := make(map[string]interface{}, len(typed))
		for key, item := range typed {
			if item != nil && reflect.TypeOf(item).Kind() == reflect.Func {
				continue
			}
			out[key] = NormalizeExport(item)
		}
		return out
	default:
		if value != nil && reflect.TypeOf(value).Kind() == reflect.Func {
			return nil
		}
		return value
	}
}
