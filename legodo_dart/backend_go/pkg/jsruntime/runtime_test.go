package jsruntime

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// lvsexs 的 ruleBookInfo.tocUrl = `@js:baseUrl.replace(...)` 用的是**裸全局**
// baseUrl（对齐 Python bootstrap 的 `var baseUrl = ...`）。只注入 java.baseUrl
// 时这条脚本炸在 "baseUrl is not defined"，题目抽空回落到详情页。
func TestBareBaseUrlGlobal(t *testing.T) {
	rt := NewRuntime()
	defer rt.Close()

	value, err := rt.Execute("baseUrl", &Context{BaseURL: "https://m.example.com/xs/1.html"})
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if value != "https://m.example.com/xs/1.html" {
		t.Errorf("裸 baseUrl 没拿到 BaseURL：got %v", value)
	}

	// 书源地址回落（对齐 Python `context.base_url or bookSourceUrl`）。
	value, err = rt.Execute("baseUrl", &Context{
		Source: map[string]interface{}{"bookSourceUrl": "https://src.example.com"},
	})
	if err != nil {
		t.Fatalf("Execute(回落): %v", err)
	}
	if value != "https://src.example.com" {
		t.Errorf("BaseURL 为空时裸 baseUrl 应回落 bookSourceUrl：got %v", value)
	}

	// 出池归还后不能把上一次的地址漏给下一次没有 BaseURL 的执行。
	value, err = rt.Execute("typeof baseUrl", &Context{})
	if err != nil {
		t.Fatalf("Execute(空上下文): %v", err)
	}
	if value != "string" {
		t.Errorf("空上下文里 baseUrl 应为空串而非陈值：got %v", value)
	}
	if value, _ = rt.Execute("baseUrl", &Context{}); value != "" {
		t.Errorf("空上下文里裸 baseUrl 应为空串：got %v", value)
	}
}

// TestJavaShimSurface 对齐设计稿 §L6 的注入面清单。
func TestJavaShimSurface(t *testing.T) {
	rt := NewRuntime()
	defer rt.Close()

	// 无操作桩与文本工具。
	for _, script := range []string{"java.toast('x')", "java.longToast('x')", "java.log('x')", "java.refreshExplore()"} {
		if _, err := rt.Execute(script, &Context{}); err != nil {
			t.Errorf("%s 应可调用：%v", script, err)
		}
	}
	if value, _ := rt.Execute("java.t('第1章')", &Context{}); value != "第1章" {
		t.Errorf("java.t=%v", value)
	}
	// D4 已知降级：t2s 原文直出（不引繁转简词典）。
	if value, _ := rt.Execute("java.t2s('繁體中文')", &Context{}); value != "繁體中文" {
		t.Errorf("t2s 应原文直出：%v", value)
	}
	if value, _ := rt.Execute("java.base64Encode('ab')", &Context{}); value != "YWI=" {
		t.Errorf("base64Encode=%v", value)
	}

	// java.get/put 变量袋跨调用存续（同一次 Execute 内）。
	ctx := &Context{}
	if value, _ := rt.Execute("java.put('k','v'); java.get('k')", ctx); value != "v" {
		t.Errorf("java.get/put 往返=%v", value)
	}
	if ctx.Variables["k"] != "v" {
		t.Errorf("变量袋应挂到 Context.Variables：%v", ctx.Variables)
	}

	// java.ajax：成功返回正文；失败返回错误文本（不抛异常，脚本常直接喂 Jsoup）。
	var seenTarget, seenUA string
	ajaxCtx := &Context{Ajax: func(target string, headers map[string]string) (string, error) {
		seenTarget, seenUA = target, headers["User-Agent"]
		if target == "https://ok.example" {
			return "<html>ok</html>", nil
		}
		return "", fmt.Errorf("boom")
	}}
	value, err := rt.Execute(`java.ajax('https://ok.example', {'User-Agent':'ua-1'})`, ajaxCtx)
	if err != nil || value != "<html>ok</html>" {
		t.Errorf("java.ajax 成功应回正文：%v/%v", value, err)
	}
	if seenTarget != "https://ok.example" || seenUA != "ua-1" {
		t.Errorf("java.ajax 参数未透传：target=%q ua=%q", seenTarget, seenUA)
	}
	value, err = rt.Execute(`java.ajax('https://bad.example')`, ajaxCtx)
	if err != nil || !strings.Contains(fmt.Sprint(value), "java.ajax 失败") {
		t.Errorf("java.ajax 失败应回错误文本：%v/%v", value, err)
	}

	// 0 号原则：未覆盖形状在调用点显式报错，不静默回空。
	for _, script := range []string{
		"java.searchBook('x')", "java.getElement('x')", "java.getElements('x')",
		"java.ajaxAll('x')", "host.call('x')", "cookie.getKey('x')",
	} {
		if _, err := rt.Execute(script, &Context{}); err == nil {
			t.Errorf("%s 应显式报错", script)
		}
	}
}

// TestJsoupShimDom jsoup DOM 子集（twkan/77shuku 书单脚本形状）。
func TestJsoupShimDom(t *testing.T) {
	rt := NewRuntime()
	defer rt.Close()

	const html = `<div class="book"><a href="/b/1">甲</a><a href="/b/2">乙</a></div>`
	ctx := &Context{Result: html, ResultHTML: true}

	cases := []struct {
		script, want string
	}{
		// result 直接是 Jsoup 文档（ResultHTML=true 绑定）。
		{`String(result.select('a').size())`, "2"},
		{`result.select('a').get(0).attr('href')`, "/b/1"},
		{`result.select('a').text()`, "甲 乙"},
		{`result.select('a').first().text()`, "甲"},
		{`result.select('a').last().text()`, "乙"},
		{`String(result.select('a').isEmpty())`, "false"},
		// 无引号属性值（jsoup 接受，cascadia 原生不认）也要命中。
		{`String(result.select('[href^=/b/2]').size())`, "1"},
		// Elements 链式 select 合并。
		{`String(result.select('div').select('a').size())`, "2"},
		{`result.select('a').first().outerHtml()`, `<a href="/b/1">甲</a>`},
		// Packages 面手动解析。
		{`Packages.org.jsoup.Jsoup.parse('<p>a<b>b</b>c</p>').select('p').text()`, "abc"},
		{`org.jsoup.Jsoup.parse('<p>a</p><p>b</p>').select('p').text()`, "a b"},
	}
	for _, c := range cases {
		value, err := rt.Execute(c.script, ctx)
		if err != nil {
			t.Errorf("%s: %v", c.script, err)
			continue
		}
		if fmt.Sprint(value) != c.want {
			// outerHtml 用前后缀断言（序列化细节含角括号，整串核验更稳）。
			if c.script == `result.select('a').outerHtml()` &&
				strings.Contains(fmt.Sprint(value), `<a href="/b/1">甲</a>`) {
				continue
			}
			t.Errorf("%s=%v want %v", c.script, value, c.want)
		}
	}
}

// TestResultBindingString result 非 HTML 时是普通字符串（多阶段管线的文本阶段）。
func TestResultBindingString(t *testing.T) {
	rt := NewRuntime()
	defer rt.Close()
	if value, _ := rt.Execute(`result + '!'`, &Context{Result: "hello"}); value != "hello!" {
		t.Errorf("result 字符串绑定=%v", value)
	}
}

// TestWatchdogInterruptsInfiniteLoop 死循环脚本不能挂死调用线程；被中断的 VM
// 不回收、下次执行照常。
func TestWatchdogInterruptsInfiniteLoop(t *testing.T) {
	rt := NewRuntime()
	defer rt.Close()
	rt.timeout = 300 * time.Millisecond

	started := time.Now()
	if _, err := rt.Execute("while(true){}", &Context{}); err == nil ||
		!strings.Contains(err.Error(), "超时") {
		t.Fatalf("死循环应被看门狗中断：%v", err)
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Errorf("中断耗时过长：%v", elapsed)
	}
	if value, err := rt.Execute("'ok'", &Context{}); err != nil || value != "ok" {
		t.Errorf("中断后执行应恢复：%v/%v", value, err)
	}
}
