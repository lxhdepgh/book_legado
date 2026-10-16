package jsruntime

import "testing"

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
