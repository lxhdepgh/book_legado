package engine

// explore.go —— 找书浏览链 L1-L4（分类解析 / URL 层 / 抓取 / 列表管线 + 字段取值）。
// 逐段对齐 Python app/services/engine/rule_engine.py 的 explore 族、legado_url.py、
// request_compat.py；设计稿 docs/找书分类浏览修复方案.md §L1-L4。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"math/big"
	"net/url"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/net/idna"
	"golang.org/x/text/encoding"
	"golang.org/x/text/encoding/htmlindex"
	"golang.org/x/text/transform"

	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
	"github.com/legodo-booksource/legodo_go/pkg/ruleparser"
)

// exploreTimeout 分类页抓取单发超时（设计稿 L3：对齐 Legado 15s 档）。
const exploreTimeout = 15 * time.Second

// ExploreCategory 一个可点击的发现分类（title + 地址模板）。
type ExploreCategory struct {
	Title string `json:"title"`
	URL   string `json:"url"`
}

// ---------------------------------------------------------------------------
// L1 分类解析（对齐 parse_explore_categories / resolve_explore_categories）
// ---------------------------------------------------------------------------

// ResolveCategories 解析书源的发现分类；exploreUrl 是 JS 规则时经 JS 运行时求值。
func (re *RuleEngine) ResolveCategories(ctx context.Context, source *models.BookSource) ([]ExploreCategory, error) {
	if source == nil {
		return nil, nil
	}
	raw := ""
	if source.ExploreURL != nil {
		raw = strings.TrimSpace(*source.ExploreURL)
	}
	if raw == "" {
		return nil, nil
	}
	if !ruleparser.IsJSRule(raw) {
		return parseCategoriesText(raw), nil
	}
	runner := re.newExploreRunner(source)
	value, err := runner.evalJS(raw, evalBinding{page: 1})
	if err != nil {
		return nil, err
	}
	return ParseCategoriesValue(source, value), nil
}

// ParseCategoriesValue 把静态文本或 JS 出口值收成分类列表（JS 出口可为
// 数组/单对象/字符串三形状）。
func ParseCategoriesValue(source *models.BookSource, value interface{}) []ExploreCategory {
	var raw interface{}
	if value == nil {
		if source != nil && source.ExploreURL != nil {
			raw = *source.ExploreURL
		} else {
			raw = ""
		}
	} else {
		raw = value
	}
	switch typed := raw.(type) {
	case []interface{}:
		return categoriesFromEntries(typed)
	case map[string]interface{}:
		return categoriesFromEntries([]interface{}{typed})
	}
	return parseCategoriesText(pythonOrString(raw))
}

func categoriesFromEntries(data []interface{}) []ExploreCategory {
	entries := make([]ExploreCategory, 0, len(data))
	for _, item := range data {
		dict, ok := item.(map[string]interface{})
		if !ok {
			continue
		}
		title := strings.TrimSpace(dictText(dict, "title"))
		urlText := strings.TrimSpace(dictText(dict, "url"))
		if title != "" && urlText != "" {
			entries = append(entries, ExploreCategory{Title: title, URL: urlText})
		}
	}
	return entries
}

// parseCategoriesText 静态分类文本：JSON 数组或逐行 `title::url`。
func parseCategoriesText(raw string) []ExploreCategory {
	text := strings.TrimSpace(raw)
	if text == "" {
		return nil
	}
	if strings.HasPrefix(text, "[") {
		var data []interface{}
		if err := json.Unmarshal([]byte(text), &data); err != nil {
			return nil
		}
		return categoriesFromEntries(data)
	}
	entries := make([]ExploreCategory, 0, 8)
	for _, line := range splitTextLines(text) {
		idx := strings.Index(line, "::")
		if idx < 0 {
			continue
		}
		title := strings.TrimSpace(line[:idx])
		urlText := strings.TrimSpace(line[idx+2:])
		if title != "" && urlText != "" {
			entries = append(entries, ExploreCategory{Title: title, URL: urlText})
		}
	}
	return entries
}

func splitTextLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	return strings.Split(text, "\n")
}

// ---------------------------------------------------------------------------
// L2 URL 层（对齐 explore()/`_resolve_rule_url`/`_render_templates`/
// legado_url.parse_search_url + resolve_search_url + request_compat）
// ---------------------------------------------------------------------------

// categoryRequest 分类页请求（parse_search_url 返回形状 + JS 返回 dict 形状）。
type categoryRequest struct {
	URL     string
	Method  string
	Body    string
	Headers map[string]string
	Charset string
}

type evalBinding struct {
	key         string
	page        int
	baseURL     string
	result      interface{}
	resultHTML  bool
	resultNodes []string
	nodesSet    bool
	ruleName    string
}

// exploreRunner 一次浏览调用内的求值上下文（源快照 + 变量袋快照）。
type exploreRunner struct {
	re        *RuleEngine
	source    *models.BookSource
	sourceMap map[string]interface{}
}

func (re *RuleEngine) newExploreRunner(source *models.BookSource) *exploreRunner {
	runner := &exploreRunner{re: re, source: source}
	runner.sourceMap = bookSourceToMap(source)
	return runner
}

func bookSourceToMap(source *models.BookSource) map[string]interface{} {
	mapped := map[string]interface{}{}
	if source == nil {
		return mapped
	}
	if raw, err := json.Marshal(source); err == nil {
		_ = json.Unmarshal(raw, &mapped)
	}
	if _, ok := mapped["bookSourceUrl"]; !ok {
		mapped["bookSourceUrl"] = source.BookSourceURL
	}
	return mapped
}

// jsBridgeError 标记来自 JS 运行时的错误（tolerate 名单里的 JsBridgeError）。
type jsBridgeError struct{ err error }

func (e *jsBridgeError) Error() string { return e.err.Error() }
func (e *jsBridgeError) Unwrap() error { return e.err }

// evalJS 执行一段带标记（@js:/<js>/@webjs:）或裸脚本的 JS；@webjs: 是
// WebView 执行路径，Go 引擎显式不支持（设计稿 §7，0 号原则）。
func (r *exploreRunner) evalJS(script string, binding evalBinding) (interface{}, error) {
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(script)), "@webjs:") {
		return nil, &ruleparser.UnsupportedError{Reason: "WebView 渲染暂不支持（Go 引擎未实现）"}
	}
	ctx := &jsruntime.Context{
		Source:     r.sourceMap,
		BaseURL:    binding.baseURL,
		Key:        binding.key,
		Page:       binding.page,
		Result:     binding.result,
		ResultHTML: binding.resultHTML,
		Ajax:       r.ajaxCallback(),
	}
	if binding.nodesSet {
		ctx.ResultNodes = binding.resultNodes
	}
	ctx.Variables = r.re.variablesSnapshot(r.source)
	value, err := r.re.jsRuntime.Execute(stripJSMarks(script), ctx)
	r.re.mergeVariables(r.source, ctx.Variables)
	if err != nil {
		return nil, &jsBridgeError{err}
	}
	return value, nil
}

// stripJSMarks 把 `@js:` / `@webjs:` / `<js>…</js>` 标记剥成纯脚本。
func stripJSMarks(value string) string {
	trimmed := strings.TrimSpace(value)
	lowered := strings.ToLower(trimmed)
	if strings.HasPrefix(lowered, "@js:") {
		return trimmed[4:]
	}
	if strings.HasPrefix(lowered, "@webjs:") {
		return trimmed[7:]
	}
	if strings.HasPrefix(lowered, "<js>") {
		body := trimmed[4:]
		if end := strings.Index(strings.ToLower(body), "</js>"); end >= 0 {
			return body[:end]
		}
		return body
	}
	return trimmed
}

// resolveCategoryRequest 对齐 explore() 前半：分类地址先过 `_resolve_rule_url`，
// 再按 JS 出口 / 条件分页 / 常规模板三条路组装请求。
func (r *exploreRunner) resolveCategoryRequest(categoryURL string, page int) (*categoryRequest, error) {
	base := ""
	if r.source != nil {
		base = r.source.BookSourceURL
	}
	resolved, err := r.resolveRuleURL(categoryURL, "", page)
	if err != nil {
		return nil, err
	}
	trimmed := strings.TrimSpace(resolved)
	if strings.HasPrefix(trimmed, "@js:") || strings.HasPrefix(trimmed, "<js>") {
		jsValue, err := r.evalJS(trimmed, evalBinding{page: page, baseURL: base})
		if err != nil {
			return nil, err
		}
		if dict, ok := jsValue.(map[string]interface{}); ok {
			return categoryRequestFromDict(dict, base), nil
		}
		request, err := parseSearchURLRequest(pythonOrString(jsValue), base, "", page)
		if err != nil {
			return nil, err
		}
		if request == nil {
			return nil, &ruleparser.UnsupportedError{Reason: "Explore JS did not return a valid request"}
		}
		return request, nil
	}
	if staticCategoryRE.MatchString(trimmed) {
		target, err := resolveStaticCategoryURL(trimmed, base, page)
		if err != nil {
			return nil, err
		}
		return &categoryRequest{URL: target, Method: "GET"}, nil
	}
	request, err := parseSearchURLRequest(trimmed, base, "", page)
	if err != nil {
		return nil, err
	}
	if request == nil {
		target, err := resolveStaticCategoryURL(trimmed, base, page)
		if err != nil {
			return nil, err
		}
		return &categoryRequest{URL: target, Method: "GET"}, nil
	}
	return request, nil
}

var staticCategoryRE = regexp.MustCompile(`<[^<>]*,[^<>]*>`)

// categoryRequestFromDict 对齐 explore() 的 JS dict 出口分支：url 按源地址补全，
// method 缺省 GET；body/headers/charset 直接透传。
func categoryRequestFromDict(dict map[string]interface{}, baseURL string) *categoryRequest {
	request := &categoryRequest{
		URL:    ruleparser.URLJoin(baseURL, dictText(dict, "url")),
		Method: "GET",
	}
	if method, ok := dict["method"]; ok && !pythonFalsy(method) {
		request.Method = normalizeMethod(pythonStr(method))
	}
	if body, ok := dict["body"]; ok && body != nil {
		request.Body = stringifyBodyValue(body)
	}
	if headers, ok := dict["headers"]; ok {
		request.Headers = stringMapFromValue(headers)
	}
	if charset, ok := dict["charset"]; ok && charset != nil {
		request.Charset = pythonStr(charset)
	}
	return request
}

// resolveRuleURL 对齐 `_resolve_rule_url`：无 JS 段时直接渲染模板；有 JS 段时
// 按 stage 顺序运行（非 JS 段以 `@result` 替换进结果，JS 段以上一结果作 result）。
func (r *exploreRunner) resolveRuleURL(value string, key string, page int) (string, error) {
	base := ""
	if r.source != nil {
		base = r.source.BookSourceURL
	}
	program := ruleparser.CompileRuleProgram(value, false)
	hasJS := false
	for _, stage := range program.Stages {
		if stage.Mode == "js" || stage.Mode == "webjs" {
			hasJS = true
			break
		}
	}
	if !hasJS {
		return r.renderTemplates(value, key, page)
	}
	result := value
	for _, stage := range program.Stages {
		if stage.Mode != "js" && stage.Mode != "webjs" {
			fragment := strings.TrimSpace(stage.Raw)
			if fragment != "" {
				result = strings.ReplaceAll(fragment, "@result", result)
			}
			continue
		}
		if stage.Mode == "webjs" {
			return "", &ruleparser.UnsupportedError{Reason: "WebView 渲染暂不支持（Go 引擎未实现）"}
		}
		evaluated, err := r.evalJS(stage.Raw, evalBinding{key: key, page: page, baseURL: base, result: result})
		if err != nil {
			return "", err
		}
		result = pythonStr(evaluated)
	}
	return r.renderTemplates(result, key, page)
}

var templateExpressionRE = regexp.MustCompile(`\{\{([\s\S]*?)\}\}`)

// renderTemplates 对齐 `_render_templates`：逐个求值 `{{ … }}` 表达式；
// 裸 `{{page}}`/`{{key}}` 走快路径不过 JS。
func (r *exploreRunner) renderTemplates(value string, key string, page int) (string, error) {
	base := ""
	if r.source != nil {
		base = r.source.BookSourceURL
	}
	rendered := value
	seen := map[string]bool{}
	for !seen[rendered] {
		seen[rendered] = true
		loc := templateExpressionRE.FindStringSubmatchIndex(rendered)
		if loc == nil {
			break
		}
		expression := strings.TrimSpace(rendered[loc[2]:loc[3]])
		if bare, ok := bareTemplateValue(expression, key, page); ok {
			rendered = rendered[:loc[0]] + bare + rendered[loc[1]:]
			continue
		}
		result, err := r.evalJS("@js:"+expression, evalBinding{key: key, page: page, baseURL: base})
		if err != nil {
			return "", err
		}
		rendered = rendered[:loc[0]] + templateResultText(result) + rendered[loc[1]:]
	}
	return rendered, nil
}

func bareTemplateValue(expression, key string, page int) (string, bool) {
	if expression == "page" {
		return strconv.Itoa(page), true
	}
	if expression == "key" {
		return key, true
	}
	return "", false
}

// templateResultText 对齐 `_template_result_text`：数字零保留（分页表达式常用），
// 其余 falsy（None/False/空串/空容器）渲染为空串。
func templateResultText(result interface{}) string {
	if isNumericZero(result) {
		return "0"
	}
	if pythonFalsy(result) {
		return ""
	}
	return pythonStr(result)
}

// parseSearchURLRequest 对齐 parse_search_url：先剪掉尾部可选 @js 段，再按
// POST 模板（`,{…}`）/ GET 模板两条路解析；无法静态解析时回 nil。
func parseSearchURLRequest(searchURL, baseURL, keyword string, page int) (*categoryRequest, error) {
	if searchURL == "" {
		return nil, nil
	}
	text := strings.TrimSpace(searchURL)
	if loc := jsSuffixRE.FindStringIndex(text); loc != nil && loc[0] > 0 {
		plainPrefix := strings.TrimSpace(text[:loc[0]])
		if strings.HasPrefix(plainPrefix, "http://") || strings.HasPrefix(plainPrefix, "https://") ||
			strings.HasPrefix(plainPrefix, "/") || strings.Contains(plainPrefix, ",{") {
			text = plainPrefix
		}
	}
	if strings.HasPrefix(text, "@js:") {
		return nil, nil
	}
	if strings.HasPrefix(text, "<js") && strings.Contains(text, "</js>") {
		return nil, nil
	}
	if strings.HasPrefix(text, "{{") && (strings.Contains(text, "source.") || strings.Contains(text, "java.")) {
		return nil, nil
	}
	if strings.Contains(text, ",") && postURLConfigRE.MatchString(text) {
		urlPart, configStr, ok := splitPostURLText(text)
		if ok {
			resolved, found := resolveSearchURLText(urlPart, baseURL, keyword, page)
			if !found {
				return nil, nil
			}
			config := normalizeURLOption(parseRequestConfigRelaxed(configStr))
			if value, ok := config["webView"]; ok && webViewOptionTruthy(value) {
				// 设计稿 §7：webView 请求选项需要 WebView 宿主，Go 引擎不承接，
				// 按 0 号原则在列表级显式报错而不是静默出一页空结果。
				return nil, &ruleparser.UnsupportedError{
					Reason: "分类需要 WebView 渲染（暂不支持）"}
			}
			method := "GET"
			if value, ok := config["method"].(string); ok && value != "" {
				method = value
			}
			body := ""
			if value, ok := config["body"]; ok && value != nil {
				body = pythonStr(value)
			}
			if body != "" {
				body = strings.ReplaceAll(body, "{{key}}", keyword)
				body = strings.ReplaceAll(body, "{{page}}", strconv.Itoa(page))
			}
			request := &categoryRequest{
				URL:     resolved,
				Method:  normalizeMethod(method),
				Body:    body,
				Headers: stringMapFromValue(config["headers"]),
			}
			if charset, ok := config["charset"]; ok && charset != nil {
				request.Charset = pythonStr(charset)
			}
			return request, nil
		}
	}
	resolved, found := resolveSearchURLText(text, baseURL, keyword, page)
	if !found {
		return nil, nil
	}
	return &categoryRequest{URL: resolved, Method: "GET"}, nil
}

var jsSuffixRE = regexp.MustCompile(`(?i)\n\s*@js:\s*`)
var postURLConfigRE = regexp.MustCompile(`,\s*\{`)

// resolveSearchURLText 对齐 resolve_search_url：模板替换 → 页码备选 → 复杂模板
// 拒判 → data:/相对/协议检查 → 编码。
func resolveSearchURLText(text, baseURL, keyword string, page int) (string, bool) {
	target := strings.TrimSpace(text)
	for _, variable := range []string{"{{key}}", "{{searchKey}}", "{{keyword}}", "{{keys}}"} {
		target = strings.ReplaceAll(target, variable, keyword)
	}
	target = strings.ReplaceAll(target, "{{page}}", strconv.Itoa(page))
	target = strings.ReplaceAll(target, "{{pg}}", strconv.Itoa(page))
	target = strings.ReplaceAll(target, "{{page-1}}", strconv.Itoa(maxInt(page-1, 0)))
	target = strings.ReplaceAll(target, "{{page+1}}", strconv.Itoa(page+1))
	target = strings.ReplaceAll(target, "{{baseUrl}}", strings.TrimRight(baseURL, "/")+"/")
	target = pageChoiceRE.ReplaceAllStringFunc(target, func(match string) string {
		inner := match[1 : len(match)-1]
		choices := strings.Split(inner, ",")
		for index := range choices {
			choices[index] = strings.TrimSpace(choices[index])
		}
		if len(choices) == 0 {
			return ""
		}
		index := maxInt(page, 1) - 1
		if index >= len(choices) {
			index = len(choices) - 1
		}
		if index < 0 {
			index = 0
		}
		return choices[index]
	})
	if strings.Contains(target, "{{") && strings.Contains(target, "}}") {
		if strings.Contains(target, "{{source.key}}") || strings.Contains(target, "{{sourceKey}}") {
			target = strings.ReplaceAll(target, "{{source.key}}", baseURL)
			target = strings.ReplaceAll(target, "{{sourceKey}}", baseURL)
			if !strings.Contains(target, "{{") || !strings.Contains(target, "}}") {
				return target, true
			}
		}
		return "", false
	}
	if strings.HasPrefix(target, "data:") {
		return target, true
	}
	if !strings.HasPrefix(target, "http://") && !strings.HasPrefix(target, "https://") {
		target = ruleparser.URLJoin(strings.TrimRight(baseURL, "/")+"/", target)
	}
	if !strings.HasPrefix(target, "http://") && !strings.HasPrefix(target, "https://") {
		return "", false
	}
	return encodeURLValue(target), true
}

var pageChoiceRE = regexp.MustCompile(`<([^<>]*)>`)

// splitPostURLText 对齐 split_post_url：定位第一个 `,{`，左半段去空白去尾逗号。
func splitPostURLText(text string) (string, string, bool) {
	loc := postURLConfigRE.FindStringSubmatchIndex(text)
	if loc == nil {
		return "", "", false
	}
	braceIdx := loc[2]
	urlPart := strings.TrimRight(strings.TrimSpace(text[:braceIdx]), ",")
	return urlPart, text[braceIdx:], true
}

// parseRequestConfigRelaxed 对齐 parse_request_config：严格 JSON 优先，
// 失败后按 Gson 宽松语法（裸键补引号、单引号串转换、尾逗号清理）重试。
func parseRequestConfigRelaxed(configStr string) map[string]interface{} {
	var value interface{}
	if err := json.Unmarshal([]byte(configStr), &value); err == nil {
		if dict, ok := value.(map[string]interface{}); ok {
			return dict
		}
		return nil
	}
	relaxed := bareKeyRE.ReplaceAllString(configStr, `$1"$2"$3`)
	var out strings.Builder
	for index := 0; index < len(relaxed); {
		char := relaxed[index]
		switch {
		case char == '"':
			start := index
			index++
			for index < len(relaxed) {
				if relaxed[index] == '\\' {
					index += 2
				} else if relaxed[index] == '"' {
					index++
					break
				} else {
					index++
				}
			}
			out.WriteString(relaxed[start:minInt(index, len(relaxed))])
		case char == '\'':
			index++
			var fragment []byte
			for index < len(relaxed) {
				if relaxed[index] == '\\' && index+1 < len(relaxed) {
					fragment = append(fragment, relaxed[index+1])
					index += 2
				} else if relaxed[index] == '\'' {
					index++
					break
				} else {
					fragment = append(fragment, relaxed[index])
					index++
				}
			}
			out.WriteString(jsonQuoteString(string(fragment)))
		default:
			out.WriteByte(char)
			index++
		}
	}
	cleaned := trailingCommaRE.ReplaceAllString(out.String(), "$1")
	if err := json.Unmarshal([]byte(cleaned), &value); err == nil {
		if dict, ok := value.(map[string]interface{}); ok {
			return dict
		}
	}
	return nil
}

var (
	bareKeyRE       = regexp.MustCompile(`([{,]\s*)([A-Za-z_][\w-]*)(\s*:)`)
	trailingCommaRE = regexp.MustCompile(`,\s*([}\]])`)
)

// normalizeURLOption 对齐 request_compat.normalize_url_option：method 归一，
// retry/serverID 的「全有全无」整数强转，headers/body 的 Java 字符串化。
func normalizeURLOption(config map[string]interface{}) map[string]interface{} {
	if config == nil {
		return map[string]interface{}{}
	}
	result := make(map[string]interface{}, len(config))
	for key, value := range config {
		result[key] = value
	}
	method := ""
	if _, ok := result["method"]; ok {
		method = javaValueString(result["method"])
	}
	result["method"] = normalizeMethod(method)
	for _, name := range []string{"retry", "serverID"} {
		value, ok := result[name]
		if !ok || value == nil {
			continue
		}
		if text, isText := value.(string); isText && text == "" {
			continue
		}
		integer, ok := coerceIntOption(value, name == "retry")
		if !ok {
			return map[string]interface{}{}
		}
		result[name] = integer
	}
	headers := result["headers"]
	if text, ok := headers.(string); ok {
		var parsed interface{}
		if err := json.Unmarshal([]byte(text), &parsed); err == nil {
			headers = parsed
		} else {
			headers = nil
		}
	}
	if dict, ok := headers.(map[string]interface{}); ok {
		converted := make(map[string]string, len(dict))
		for key, value := range dict {
			converted[javaValueString(key)] = javaValueString(value)
		}
		result["headers"] = converted
	} else {
		result["headers"] = nil
	}
	body := result["body"]
	switch body.(type) {
	case map[string]interface{}, []interface{}:
		result["body"] = compactJSONText(body)
	default:
		if body != nil {
			result["body"] = javaValueString(body)
		}
	}
	return result
}

func coerceIntOption(value interface{}, isRetry bool) (int, bool) {
	if _, isBool := value.(bool); isBool {
		return 0, false
	}
	var number float64
	switch typed := value.(type) {
	case float64:
		number = typed
	case int:
		number = float64(typed)
	case int64:
		number = float64(typed)
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		if err != nil {
			return 0, false
		}
		number = parsed
	default:
		return 0, false
	}
	if math.IsInf(number, 0) || math.IsNaN(number) || number != math.Trunc(number) {
		return 0, false
	}
	if number > math.MaxInt64 || number < math.MinInt64 {
		return 0, false
	}
	integer := int(number)
	if isRetry && (integer < -2147483648 || integer > 2147483647) {
		return 0, false
	}
	return integer, true
}

// javaValueString 对齐 request_compat.java_value_string（Java Any?.toString）。
func javaValueString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "null"
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case map[string]interface{}:
		keys := sortedKeys(typed)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, javaValueString(key)+"="+javaValueString(typed[key]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []interface{}:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			parts = append(parts, javaValueString(item))
		}
		return "[" + strings.Join(parts, ", ") + "]"
	default:
		return pythonStr(value)
	}
}

// resolveStaticCategoryURL 对齐 `_resolve_explore_url`：静态分类地址的
// 页码模板 + 条件分页展开（JS/Cookie/动态模板显式报错）。
func resolveStaticCategoryURL(raw, baseURL string, page int) (string, error) {
	if ruleparser.IsJSRule(raw) || strings.Contains(raw, "{{cookie.") || strings.Contains(raw, "{{java.") {
		return "", &ruleparser.UnsupportedError{Reason: "分类地址需要 JavaScript、Cookie 或动态签名"}
	}
	target := expandPageTemplateValue(raw, page)
	target = conditionalPageRE.ReplaceAllStringFunc(target, func(match string) string {
		inner := match[1 : len(match)-1]
		first, later, found := strings.Cut(inner, ",")
		if !found {
			return inner
		}
		if page == 1 {
			return first
		}
		return later
	})
	if strings.Contains(target, "{{") {
		return "", &ruleparser.UnsupportedError{Reason: "分类地址包含暂不支持的动态模板"}
	}
	return encodeURLValue(ruleparser.URLJoin(strings.TrimRight(baseURL, "/")+"/", target)), nil
}

var conditionalPageRE = regexp.MustCompile(`<([^<>]*,[^<>]*)>`)

func expandPageTemplateValue(value string, page int) string {
	value = strings.ReplaceAll(value, "{{page}}", strconv.Itoa(page))
	value = strings.ReplaceAll(value, "{{page-1}}", strconv.Itoa(maxInt(page-1, 0)))
	value = strings.ReplaceAll(value, "{{page+1}}", strconv.Itoa(page+1))
	return value
}

// ---------------------------------------------------------------------------
// URL 编码族（对齐 legado_url.encode_url 与 request_compat 的 escape/encode）
// ---------------------------------------------------------------------------

// encodeURLValue 对齐 encode_url：全 ASCII 直通；否则 netloc 逐段 IDNA、
// path/query 百分号编码（fragment 丢弃）。
func encodeURLValue(raw string) string {
	if isASCIIString(raw) {
		return raw
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return escapeNonASCII(raw, ":/?#[]@!$&'()*+,;=-._~%")
	}
	segments := strings.Split(parsed.Host, ".")
	for index, segment := range segments {
		if isASCIIString(segment) {
			continue
		}
		if encoded, err := idna.Lookup.ToASCII(segment); err == nil {
			segments[index] = encoded
		}
	}
	result := parsed.Scheme + "://" + strings.Join(segments, ".") +
		escapeNonASCII(parsed.EscapedPath(), "/%")
	if query := escapeNonASCII(parsed.RawQuery, "=&%"); query != "" {
		result += "?" + query
	}
	return result
}

func isASCIIString(value string) bool {
	for index := 0; index < len(value); index++ {
		if value[index] >= 0x80 {
			return false
		}
	}
	return true
}

var percentEscapeRE = regexp.MustCompile(`%[0-9a-fA-F]{2}`)

// escapeURLQuery 对齐 escape_url_query：query 逐字段 JS escape。
func escapeURLQuery(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.RawQuery == "" {
		return raw
	}
	parsed.RawQuery = escapeForm(parsed.RawQuery)
	return parsed.String()
}

// escapeForm 对齐 escape_form：字段名 quote_plus、字段值 JS escape。
func escapeForm(value string) string {
	fields := make([]string, 0, 4)
	for _, field := range strings.Split(value, "&") {
		if strings.TrimSpace(field) == "" {
			continue
		}
		name, item, found := strings.Cut(field, "=")
		if !found {
			item = ""
		}
		fields = append(fields, quotePlus(name, "%")+"="+legadoEscape(item))
	}
	return strings.Join(fields, "&")
}

const escapeSafeChars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"

// legadoEscape 对齐 JS/Legado escape：<256 走 %XX，其余 %uXXXX（含代理对）。
func legadoEscape(value string) string {
	var builder strings.Builder
	for _, char := range value {
		code := int(char)
		if code < 0x80 && strings.IndexByte(escapeSafeChars, byte(char)) >= 0 {
			builder.WriteByte(byte(char))
			continue
		}
		switch {
		case code < 256:
			fmt.Fprintf(&builder, "%%%02X", code)
		case code <= 0xFFFF:
			fmt.Fprintf(&builder, "%%u%04X", code)
		default:
			code -= 0x10000
			fmt.Fprintf(&builder, "%%u%04X", 0xD800+(code>>10))
			fmt.Fprintf(&builder, "%%u%04X", 0xDC00+(code&0x3FF))
		}
	}
	return builder.String()
}

// encodeURLQueryFields 对齐 encode_url_query_fields：query 逐字段表单编码。
func encodeURLQueryFields(raw string, charset string) string {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.RawQuery == "" {
		return raw
	}
	parsed.RawQuery = encodeFormFields(parsed.RawQuery, charset)
	return parsed.String()
}

// encodeFormFields 对齐 encode_form_fields：已含 %XX 的值原样保留。
func encodeFormFields(value, charset string) string {
	normalized := charset
	if normalized == "" {
		normalized = "utf-8"
	}
	fields := make([]string, 0, 4)
	for _, field := range strings.Split(value, "&") {
		if strings.TrimSpace(field) == "" {
			continue
		}
		name, item, found := strings.Cut(field, "=")
		if !found {
			item = ""
		}
		encoded := item
		if !percentEscapeRE.MatchString(item) {
			encoded = encodeValueWithCharset(item, normalized)
		}
		fields = append(fields, quotePlus(name, "%")+"="+encoded)
	}
	return strings.Join(fields, "&")
}

func encodeValueWithCharset(value, charset string) string {
	if strings.EqualFold(charset, "utf-8") || strings.EqualFold(charset, "utf8") {
		return quotePlus(value, "")
	}
	encoder, err := htmlindex.Get(charset)
	if err != nil {
		return quotePlus(value, "")
	}
	encoded, _, err := transform.String(encoding.ReplaceUnsupported(encoder.NewEncoder()), value)
	if err != nil {
		return quotePlus(value, "")
	}
	return percentEncodeBytes([]byte(encoded))
}

func percentEncodeBytes(data []byte) string {
	var builder strings.Builder
	for _, char := range data {
		if char < 0x80 && isURLSafeByte(char) {
			builder.WriteByte(char)
			continue
		}
		fmt.Fprintf(&builder, "%%%02X", char)
	}
	return builder.String()
}

// quotePlus 对齐 Python quote_plus（保留 safe 集合，空格转 `+`）。
func quotePlus(value, safe string) string {
	var builder strings.Builder
	for index := 0; index < len(value); index++ {
		char := value[index]
		switch {
		case char < 0x80 && (isURLSafeByte(char) || strings.IndexByte(safe, char) >= 0):
			builder.WriteByte(char)
		case char == ' ':
			builder.WriteByte('+')
		default:
			fmt.Fprintf(&builder, "%%%02X", char)
		}
	}
	return builder.String()
}

// ---------------------------------------------------------------------------
// L3 抓取（`_request` 的务实子集：源头合并 + POST 默认头 + 15s 超时）
// ---------------------------------------------------------------------------

func (r *exploreRunner) fetchCategory(ctx context.Context, request *categoryRequest) (string, string, string, error) {
	headers := map[string]string{}
	if r.source != nil && r.source.Header != nil {
		var sourceHeaders map[string]string
		if err := json.Unmarshal([]byte(*r.source.Header), &sourceHeaders); err == nil {
			for key, value := range sourceHeaders {
				headers[key] = value
			}
		}
	}
	for key, value := range request.Headers {
		headers[key] = value
	}
	method := "GET"
	if request.Method == "POST" {
		method = "POST"
	}
	if method == "POST" && !hasHeaderFold(headers, "content-type") {
		headers["Content-Type"] = defaultPostContentType(request.Body)
	}
	for key := range headers {
		if strings.EqualFold(key, "user-agent") && headers[key] == "null" {
			delete(headers, key)
		}
	}
	target := request.URL
	if strings.ToLower(request.Charset) == "escape" {
		target = escapeURLQuery(target)
	} else {
		target = encodeURLQueryFields(target, charsetOrUtf8(request.Charset))
	}
	requestCtx, cancel := context.WithTimeout(ctx, exploreTimeout)
	defer cancel()
	var response *httpclient.Response
	var err error
	if method == "POST" {
		payload := encodeRequestBody(request.Body, request.Charset, headerValueFold(headers, "content-type"))
		response, err = r.re.httpClient.Do(requestCtx, "POST", target, headers, bytes.NewReader(payload))
	} else {
		response, err = r.re.httpClient.Do(requestCtx, "GET", target, headers, nil)
	}
	if err != nil {
		return "", "", "", fmt.Errorf("分类请求失败: %w", err)
	}
	contentType := response.Header.Get("Content-Type")
	text := normalizeResponseText(response.BodyString(), contentType)
	return text, contentType, response.FinalURL, nil
}

func charsetOrUtf8(charset string) string {
	if charset == "" {
		return "utf-8"
	}
	return charset
}

func hasHeaderFold(headers map[string]string, name string) bool {
	for key := range headers {
		if strings.EqualFold(key, name) {
			return true
		}
	}
	return false
}

func headerValueFold(headers map[string]string, name string) string {
	for key, value := range headers {
		if strings.EqualFold(key, name) {
			return value
		}
	}
	return ""
}

// defaultPostContentType 对齐 `_default_post_content_type`：JSON 带 charset，
// form 头不带 charset。
func defaultPostContentType(body string) string {
	trimmed := strings.TrimLeft(body, " \t\n\r\v\f")
	if strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[") || strings.HasPrefix(trimmed, "<") {
		return "application/json; charset=UTF-8"
	}
	return "application/x-www-form-urlencoded"
}

// encodeRequestBody 对齐 `_encode_request_body`：非 JSON/XML 形状走表单编码。
func encodeRequestBody(body, charset, contentType string) []byte {
	trimmed := strings.TrimLeft(body, " \t\n\r\v\f")
	if !strings.HasPrefix(trimmed, "{") && !strings.HasPrefix(trimmed, "[") && !strings.HasPrefix(trimmed, "<") {
		if strings.ToLower(charset) == "escape" {
			return []byte(escapeForm(body))
		}
		return []byte(encodeFormFields(body, charsetOrUtf8(charset)))
	}
	bodyCharset := "utf-8"
	if match := bodyCharsetRE.FindStringSubmatch(contentType); match != nil {
		bodyCharset = match[1]
	}
	if strings.EqualFold(bodyCharset, "utf-8") || strings.EqualFold(bodyCharset, "utf8") {
		return []byte(body)
	}
	encoder, err := htmlindex.Get(bodyCharset)
	if err != nil {
		return []byte(body)
	}
	encoded, _, err := transform.String(encoding.ReplaceUnsupported(encoder.NewEncoder()), body)
	if err != nil {
		return []byte(body)
	}
	return []byte(encoded)
}

var bodyCharsetRE = regexp.MustCompile(`(?i)charset\s*=\s*["']?([^;\s"']+)`)

// normalizeResponseText 对齐 `_normalize_response_text`：XML 无声明时补版本声明。
func normalizeResponseText(text, contentType string) string {
	mediaType := strings.ToLower(strings.TrimSpace(strings.SplitN(contentType, ";", 2)[0]))
	switch mediaType {
	case "application/xml", "text/xml", "application/xhtml+xml":
		if !strings.HasPrefix(strings.ToLower(strings.TrimLeft(text, " \t\n\r\v\f")), "<?xml") {
			return `<?xml version="1.0"?>` + text
		}
	}
	return text
}

// ---------------------------------------------------------------------------
// Part 2 · Python 语义助手族
// ---------------------------------------------------------------------------

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// sortedKeys 是 Go 侧对 Python 插入序的降级（决策 (j)：键排序）。
func sortedKeys(value map[string]interface{}) []string {
	keys := make([]string, 0, len(value))
	for key := range value {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// dictText 对齐 `str(item.get(name, ""))`：缺键回空串，显式 null 回 "None"。
func dictText(dict map[string]interface{}, key string) string {
	value, ok := dict[key]
	if !ok {
		return ""
	}
	if value == nil {
		return "None"
	}
	return pythonStr(value)
}

// pythonStr 对齐 Python `str()`：None→None、bool→True/False、整值浮点带 .0、
// 容器走 repr。
func pythonStr(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "None"
	case bool:
		if typed {
			return "True"
		}
		return "False"
	case string:
		return typed
	case int:
		return strconv.Itoa(typed)
	case int64:
		return strconv.FormatInt(typed, 10)
	case float64:
		return pythonFloatText(typed)
	case json.Number:
		text := typed.String()
		if !strings.ContainsAny(text, ".eE") {
			return text
		}
		if parsed, err := typed.Float64(); err == nil {
			return pythonFloatText(parsed)
		}
		return text
	case map[string]interface{}, []interface{}:
		return pythonReprValue(value)
	case *ruleparser.Node:
		return typed.OuterHtml()
	case *ruleparser.Document:
		return nodeAsString(value)
	}
	return fmt.Sprint(value)
}

// pythonFloatText 对齐 Python `repr(float)`：最短往返十进制，指数域 <-4 或 >=16
// 走 e 形，整值补 ".0"。
func pythonFloatText(value float64) string {
	if math.IsNaN(value) {
		return "nan"
	}
	if math.IsInf(value, 1) {
		return "inf"
	}
	if math.IsInf(value, -1) {
		return "-inf"
	}
	exponentForm := strconv.FormatFloat(value, 'e', -1, 64)
	mantissa, exponentText, _ := strings.Cut(exponentForm, "e")
	exponent, err := strconv.Atoi(exponentText)
	if err == nil && exponent >= -4 && exponent < 16 {
		fixed := strconv.FormatFloat(value, 'f', -1, 64)
		if !strings.ContainsAny(fixed, ".e") {
			fixed += ".0"
		}
		return fixed
	}
	if !strings.Contains(mantissa, ".") {
		bits := strings.SplitN(exponentForm, "e", 2)
		exponentForm = bits[0] + ".0e" + bits[1]
	}
	return exponentForm
}

// pythonReprValue 对齐 Python `repr()` 的容器形状（list `[a, b]`、dict
// `{'k': v}`）。
func pythonReprValue(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "None"
	case bool:
		if typed {
			return "True"
		}
		return "False"
	case string:
		return pythonReprString(typed)
	case map[string]interface{}:
		keys := sortedKeys(typed)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, pythonReprString(key)+": "+pythonReprValue(typed[key]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []interface{}:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			parts = append(parts, pythonReprValue(item))
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case []*ruleparser.Node:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			parts = append(parts, item.OuterHtml())
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	return pythonStr(value)
}

// pythonReprString 对齐 Python `repr(str)`：优先单引号，含单引号不含双引号时
// 用双引号；控制字符转 \xNN。
func pythonReprString(value string) string {
	quote := byte('\'')
	if strings.Contains(value, "'") && !strings.Contains(value, "\"") {
		quote = '"'
	}
	var builder strings.Builder
	builder.WriteByte(quote)
	for _, character := range value {
		switch {
		case character == rune(quote), character == '\\':
			builder.WriteByte('\\')
			builder.WriteRune(character)
		case character == '\n':
			builder.WriteString("\\n")
		case character == '\r':
			builder.WriteString("\\r")
		case character == '\t':
			builder.WriteString("\\t")
		case character < 0x20 || character == 0x7f:
			builder.WriteString(fmt.Sprintf("\\x%02x", character))
		default:
			builder.WriteRune(character)
		}
	}
	builder.WriteByte(quote)
	return builder.String()
}

// pythonFalsy 对齐 Python 真值判定（None/False/0/""/空容器为假）。
func pythonFalsy(value interface{}) bool {
	switch typed := value.(type) {
	case nil:
		return true
	case bool:
		return !typed
	case string:
		return typed == ""
	case int:
		return typed == 0
	case int64:
		return typed == 0
	case float64:
		return typed == 0
	case json.Number:
		text := typed.String()
		if parsed, err := typed.Float64(); err == nil {
			return parsed == 0
		}
		return text == ""
	case []interface{}:
		return len(typed) == 0
	case map[string]interface{}:
		return len(typed) == 0
	case []*ruleparser.Node:
		return len(typed) == 0
	}
	return false
}

// pythonOrString 对齐 `str(value or "")`。
func pythonOrString(value interface{}) string {
	if pythonFalsy(value) {
		return ""
	}
	return pythonStr(value)
}

// isNumericZero 对齐 `value == 0 and not isinstance(value, bool)`。
func isNumericZero(value interface{}) bool {
	switch typed := value.(type) {
	case bool:
		return false
	case int:
		return typed == 0
	case int64:
		return typed == 0
	case float64:
		return typed == 0
	case json.Number:
		if parsed, err := typed.Float64(); err == nil {
			return parsed == 0
		}
	}
	return false
}

// jsonQuoteString 对齐 `json.dumps(text, ensure_ascii=False)`。
func jsonQuoteString(value string) string {
	var builder bytes.Buffer
	encoder := json.NewEncoder(&builder)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return "\"" + value + "\""
	}
	return strings.TrimRight(builder.String(), "\n")
}

// compactJSONText 对齐 `json.dumps(value, ensure_ascii=False,
// separators=(",", ":"))`。
func compactJSONText(value interface{}) string {
	var builder bytes.Buffer
	encoder := json.NewEncoder(&builder)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return ""
	}
	return strings.TrimRight(builder.String(), "\n")
}

// normalizeMethod 对齐 normalize_url_option 的两值收敛。
func normalizeMethod(value string) string {
	if strings.EqualFold(strings.TrimSpace(value), "post") {
		return "POST"
	}
	return "GET"
}

// stringMapFromValue 把请求头载荷规整成 map（None/非容器→nil，对齐 Python
// `config.get("headers")` 透传）。
func stringMapFromValue(value interface{}) map[string]string {
	switch typed := value.(type) {
	case map[string]string:
		out := make(map[string]string, len(typed))
		for key, item := range typed {
			out[key] = item
		}
		return out
	case map[string]interface{}:
		out := make(map[string]string, len(typed))
		for key, item := range typed {
			out[key] = javaValueString(item)
		}
		return out
	case string:
		var parsed interface{}
		if err := json.Unmarshal([]byte(typed), &parsed); err != nil {
			return nil
		}
		return stringMapFromValue(parsed)
	}
	return nil
}

// stringifyBodyValue 对齐 `json.dumps(dict/list) 否则 str(body)`。
func stringifyBodyValue(value interface{}) string {
	switch value.(type) {
	case map[string]interface{}, []interface{}:
		return compactJSONText(value)
	}
	return pythonStr(value)
}

// webViewOptionTruthy 对齐 legado_js 的 webView 开关判定：null/false/""/"false"
// 以外都算要求 WebView。
func webViewOptionTruthy(value interface{}) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case bool:
		return typed
	case string:
		lowered := strings.TrimSpace(strings.ToLower(typed))
		return lowered != "" && lowered != "false"
	}
	return !pythonFalsy(value)
}

// tolerateFieldError 对齐 `_book_from_node.field` 的 tolerate 名单
// （UnsupportedRule / RuleParseError / JsBridgeError）。
func tolerateFieldError(err error) bool {
	var unsupported *ruleparser.UnsupportedError
	if errors.As(err, &unsupported) {
		return true
	}
	var selector *ruleparser.SelectorError
	if errors.As(err, &selector) {
		return true
	}
	var bridge *jsBridgeError
	return errors.As(err, &bridge)
}

// ---------------------------------------------------------------------------
// JSON（对齐 parse_json_smart 的 json-smart 无损数值分裂）
// ---------------------------------------------------------------------------

// parseJSONSmartText 对齐 parse_json_smart：整数走 int64，浮点仅在 float64
// 往返无损时取 float，否则保留原文本（Python 落 Decimal，str 即原文）。
func parseJSONSmartText(text string) (interface{}, error) {
	if strings.TrimSpace(text) == "" {
		return nil, fmt.Errorf("期望 JSON 值，但获取到空值")
	}
	var strict interface{}
	if err := json.Unmarshal([]byte(text), &strict); err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	var value interface{}
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	return convertJSONNumbers(value), nil
}

func convertJSONNumbers(value interface{}) interface{} {
	switch typed := value.(type) {
	case map[string]interface{}:
		out := make(map[string]interface{}, len(typed))
		for key, item := range typed {
			out[key] = convertJSONNumbers(item)
		}
		return out
	case []interface{}:
		out := make([]interface{}, 0, len(typed))
		for _, item := range typed {
			out = append(out, convertJSONNumbers(item))
		}
		return out
	case json.Number:
		return smartNumber(typed)
	}
	return value
}

// smartNumber 是 parse_int=int / parse_float=_json_smart_float 的 Go 口径。
func smartNumber(token json.Number) interface{} {
	text := token.String()
	if !strings.ContainsAny(text, ".eE") {
		if parsed, err := token.Int64(); err == nil {
			return parsed
		}
		return token
	}
	parsed, err := token.Float64()
	if err != nil {
		return token
	}
	source, sourceOK := new(big.Rat).SetString(text)
	rendered, renderedOK := new(big.Rat).SetString(pythonFloatText(parsed))
	if sourceOK && renderedOK && source.Cmp(rendered) == 0 {
		return parsed
	}
	return token
}

// isJSONTextLike 对齐 String.isJson()（AnalyzeRule 按内容挑 JSON 分析器）。
func isJSONTextLike(value interface{}) bool {
	text := strings.TrimSpace(pythonStr(value))
	return (strings.HasPrefix(text, "{") && strings.HasSuffix(text, "}")) ||
		(strings.HasPrefix(text, "[") && strings.HasSuffix(text, "]"))
}

// ---------------------------------------------------------------------------
// 节点 / 载荷形状
// ---------------------------------------------------------------------------

// listifyRoot 对齐 `root if isinstance(root, list) else [root]`。
func listifyRoot(root interface{}) []interface{} {
	if typed, ok := root.([]interface{}); ok {
		return typed
	}
	return []interface{}{root}
}

// nodeAsString 对齐 bs4 节点的 `str(node)`（markup 全文）。
func nodeAsString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "None"
	case string:
		return typed
	case *ruleparser.Node:
		return typed.OuterHtml()
	case *ruleparser.Document:
		roots := typed.RootNodes()
		if len(roots) > 0 {
			return roots[0].OuterHtml()
		}
		return ""
	}
	return pythonStr(value)
}

// nodeHasSelect 对齐 `hasattr(node, "select")`：只有 DOM 节点有该属性。
func nodeHasSelect(value interface{}) bool {
	switch value.(type) {
	case *ruleparser.Node, *ruleparser.Document:
		return true
	}
	return false
}

// rhinoResultString 对齐 `_rhino_result_string`：JS 结果喂给分析器前的文本化。
func rhinoResultString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "null"
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case map[string]interface{}:
		return "[object Object]"
	case []interface{}:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			switch item.(type) {
			case map[string]interface{}, []interface{}:
				parts = append(parts, rhinoResultString(item))
			case nil:
				parts = append(parts, "")
			default:
				parts = append(parts, pythonStr(item))
			}
		}
		return strings.Join(parts, ",")
	}
	return pythonStr(value)
}

// payloadForCompiledStage 对齐 `_payload_for_compiled_stage`：JS 段结果按下一段
// 模式物化成 (root, kind, stage_result)。
func payloadForCompiledStage(transformed interface{}, remaining, analyzerKind string) (interface{}, string, interface{}, error) {
	program := ruleparser.CompileRuleProgram(remaining, analyzerKind == "json")
	mode := "default"
	if len(program.Stages) > 0 {
		mode = program.Stages[0].Mode
	}
	switch mode {
	case "js", "webjs":
		return transformed, analyzerKind, transformed, nil
	case "json":
		switch transformed.(type) {
		case map[string]interface{}, []interface{}:
			return transformed, "json", transformed, nil
		}
		root, err := parseJSONSmartText(rhinoResultString(transformed))
		if err != nil {
			return nil, "", nil, err
		}
		return root, "json", transformed, nil
	}
	text := rhinoResultString(transformed)
	document, err := ruleparser.ParseHTML(text)
	if err != nil {
		return nil, "", nil, err
	}
	return document, "html", text, nil
}

// splitBalancedText 对齐 split_balanced（`@put:{a:1,b:2}` 的顶层逗号切分）。
func splitBalancedText(value, separator string) []string {
	parts := []string{}
	start := 0
	quote := byte(0)
	square, round, curly := 0, 0, 0
	for index := 0; index < len(value); index++ {
		character := value[index]
		switch {
		case quote != 0:
			if character == quote && (index == 0 || value[index-1] != '\\') {
				quote = 0
			}
		case character == '\'' || character == '"':
			quote = character
		case character == '[':
			square++
		case character == ']':
			if square > 0 {
				square--
			}
		case character == '(':
			round++
		case character == ')':
			if round > 0 {
				round--
			}
		case character == '{':
			curly++
		case character == '}':
			if curly > 0 {
				curly--
			}
		default:
			if square == 0 && round == 0 && curly == 0 &&
				strings.HasPrefix(value[index:], separator) {
				parts = append(parts, strings.TrimSpace(value[start:index]))
				index += len(separator) - 1
				start = index + 1
			}
		}
	}
	parts = append(parts, strings.TrimSpace(value[start:]))
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

// fieldParts 对齐 `_field_parts`：选择器与其后的 JS/正则后缀。
func fieldParts(rule, kind string) (string, string) {
	return ruleparser.CompileRuleProgram(rule, kind == "json").SelectorAndSuffix()
}

// ---------------------------------------------------------------------------
// Part 2 · 变量袋（Python 引擎实例级 self.js_variables）
// ---------------------------------------------------------------------------

var pythonSpaceRunes = []rune{
	9, 10, 11, 12, 13, 32, 0x1c, 0x1d, 0x1e, 0x1f,
	0x85, 0xa0, 0x1680,
	0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007,
	0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
}

var pythonSpaces = string(pythonSpaceRunes)

func lstripPython(value string) string {
	return strings.TrimLeft(value, pythonSpaces)
}

func (re *RuleEngine) sourceBag(source *models.BookSource) map[string]interface{} {
	key := ""
	if source != nil {
		key = source.BookSourceURL
	}
	re.bagMu.Lock()
	defer re.bagMu.Unlock()
	if re.sourceBags == nil {
		re.sourceBags = map[string]map[string]interface{}{}
	}
	bag, ok := re.sourceBags[key]
	if !ok {
		bag = map[string]interface{}{}
		re.sourceBags[key] = bag
	}
	return bag
}

// variablesSnapshot 把变量袋拷成 JS 侧副本：java.put 写副本，脚本结束再合并回，
// 避免脚本执行中途与其他调用共享一张可变 map。
func (re *RuleEngine) variablesSnapshot(source *models.BookSource) map[string]interface{} {
	bag := re.sourceBag(source)
	re.bagMu.Lock()
	defer re.bagMu.Unlock()
	snapshot := make(map[string]interface{}, len(bag))
	for key, value := range bag {
		snapshot[key] = value
	}
	return snapshot
}

func (re *RuleEngine) mergeVariables(source *models.BookSource, snapshot map[string]interface{}) {
	if snapshot == nil {
		return
	}
	bag := re.sourceBag(source)
	re.bagMu.Lock()
	defer re.bagMu.Unlock()
	for key, value := range snapshot {
		bag[key] = value
	}
}

func (re *RuleEngine) putVariable(source *models.BookSource, name, value string) string {
	bag := re.sourceBag(source)
	re.bagMu.Lock()
	defer re.bagMu.Unlock()
	bag[name] = value
	return value
}

// getVariableValue 对齐 `_get_variable`（书源浏览链上 book/chapter 恒为 None，
// 故只走引擎变量袋那一层）。
func (re *RuleEngine) getVariableValue(source *models.BookSource, name string) string {
	bag := re.sourceBag(source)
	re.bagMu.Lock()
	defer re.bagMu.Unlock()
	for _, key := range []string{name, "source:data:" + name} {
		value, ok := bag[key]
		if !ok || value == nil {
			continue
		}
		text := pythonStr(value)
		if text != "" {
			return text
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// Part 2 · java.ajax 回调（对齐 legado_js._ajax / _request_json 的 body 出口）
// ---------------------------------------------------------------------------

func (r *exploreRunner) ajaxCallback() func(string, map[string]string) (string, error) {
	return func(requestValue string, extraHeaders map[string]string) (string, error) {
		base := ""
		if r.source != nil {
			base = r.source.BookSourceURL
		}
		target := strings.TrimSpace(requestValue)
		if target == "" {
			return "", nil
		}
		request, err := parseSearchURLRequest(target, base, "", 1)
		if err != nil {
			return errorText(err), nil
		}
		if request == nil {
			resolved, found := resolveSearchURLText(target, base, "", 1)
			if !found {
				return errorText(fmt.Errorf("ajax 无法解析请求地址: %s", target)), nil
			}
			request = &categoryRequest{URL: resolved, Method: "GET"}
		}
		if len(extraHeaders) > 0 {
			merged := map[string]string{}
			for key, value := range request.Headers {
				merged[key] = value
			}
			for key, value := range extraHeaders {
				merged[key] = value
			}
			request.Headers = merged
		}
		// JS 桥是同步口，拿不到调用方的 ctx；用同一档 15s 兜底。
		text, _, _, err := r.fetchCategory(context.Background(), request)
		if err != nil {
			return errorText(err), nil
		}
		return text, nil
	}
}

// errorText 对齐 Python `_ajax` 的 runCatching：把异常文本当返回值交回脚本，
// 脚本常直接喂给 Jsoup.parse/JSON.parse。
func errorText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ---------------------------------------------------------------------------
// Part 2 · L4 列表管线（对齐 _list_rule_pipeline 逐段语义）
// ---------------------------------------------------------------------------

// stageText 对齐 `text = transformed if isinstance(transformed,str) else json.dumps(...)`。
func stageText(transformed interface{}) string {
	if text, ok := transformed.(string); ok {
		return text
	}
	return compactJSONText(transformed)
}

// markMarkupNodes 对齐首分支：JS 返回带 __markup 的字典列表时换成 markup 串。
func markMarkupNodes(transformed interface{}) interface{} {
	items, ok := transformed.([]interface{})
	if !ok || len(items) == 0 {
		return transformed
	}
	for _, item := range items {
		dict, ok := item.(map[string]interface{})
		if !ok {
			return transformed
		}
		if _, ok := dict["__markup"]; !ok {
			return transformed
		}
	}
	out := make([]interface{}, 0, len(items))
	for _, item := range items {
		out = append(out, pythonStr(item.(map[string]interface{})["__markup"]))
	}
	return out
}

func allItemsString(transformed interface{}) ([]string, bool) {
	items, ok := transformed.([]interface{})
	if !ok {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok {
			return nil, false
		}
		out = append(out, text)
	}
	return out, true
}

func allItemsMap(transformed interface{}) bool {
	items, ok := transformed.([]interface{})
	if !ok || len(items) == 0 {
		return false
	}
	for _, item := range items {
		if _, ok := item.(map[string]interface{}); !ok {
			return false
		}
	}
	return true
}

// materializeLeadingJS 是 leading-JS 段后的 6 分支（顺序照抄 Python，含空列表
// 与 isJson 两条 suffix 段没有的分支）。handled=false 时走尾部解析。
func materializeLeadingJS(transformed interface{}, text, contentType string) ([]interface{}, string, bool, error) {
	transformed = markMarkupNodes(transformed)
	if items, ok := allItemsString(transformed); ok && len(items) > 0 {
		allMarkup := true
		for _, item := range items {
			if !strings.HasPrefix(lstripPython(item), "<") {
				allMarkup = false
				break
			}
		}
		if allMarkup {
			nodes := make([]interface{}, 0, len(items))
			for _, item := range items {
				if node := ruleparser.FirstElementNode(item); node != nil {
					nodes = append(nodes, node)
				}
			}
			return nodes, "html", true, nil
		}
	}
	if items, ok := transformed.([]interface{}); ok && len(items) == 0 {
		return nil, "html", true, nil
	}
	if _, ok := transformed.(map[string]interface{}); ok {
		return []interface{}{transformed}, "json", true, nil
	}
	if allItemsMap(transformed) {
		return transformed.([]interface{}), "json", true, nil
	}
	if items, ok := allItemsString(transformed); ok && len(items) > 0 {
		allJSON := true
		for _, item := range items {
			if !isJSONTextLike(item) {
				allJSON = false
				break
			}
		}
		if allJSON {
			nodes := make([]interface{}, 0, len(items))
			for _, item := range items {
				nodes = append(nodes, item)
			}
			return nodes, "json", true, nil
		}
	}
	nodes, kind, err := tailMaterialize(text, contentType)
	return nodes, kind, false, err
}

// materializeSuffixStage 是 suffix JS 段后的 4 分支：**没有**空列表与 isJson 两条
// （空列表在这里落到「全 dict」分支 → [],"json"）。
func materializeSuffixStage(transformed interface{}, text, contentType string) ([]interface{}, string, bool, error) {
	transformed = markMarkupNodes(transformed)
	if items, ok := allItemsString(transformed); ok && len(items) > 0 {
		allMarkup := true
		for _, item := range items {
			if !strings.HasPrefix(lstripPython(item), "<") {
				allMarkup = false
				break
			}
		}
		if allMarkup {
			nodes := make([]interface{}, 0, len(items))
			for _, item := range items {
				if node := ruleparser.FirstElementNode(item); node != nil {
					nodes = append(nodes, node)
				}
			}
			return nodes, "html", true, nil
		}
	}
	if _, ok := transformed.(map[string]interface{}); ok {
		return []interface{}{transformed}, "json", true, nil
	}
	if allItemsMap(transformed) {
		return transformed.([]interface{}), "json", true, nil
	}
	nodes, kind, err := tailMaterialize(text, contentType)
	return nodes, kind, false, err
}

// tailMaterialize 对齐两个分支块之后共同的收尾解析：`"json" in content_type`
// 用**原始** ctype（大小写敏感，与嗅探处的 .lower() 不同，见决策 (b)）。
func tailMaterialize(text, contentType string) ([]interface{}, string, error) {
	if strings.Contains(contentType, "json") {
		root, err := parseJSONSmartText(text)
		if err != nil {
			return nil, "", err
		}
		return listifyRoot(root), "json", nil
	}
	document, err := ruleparser.ParseHTML(text)
	if err != nil {
		return nil, "", err
	}
	return []interface{}{document}, "html", nil
}

// listRulePipeline 顺序跑完列表规则里的每个选择器/JS 段（对齐
// `_list_rule_pipeline`，含 `-`/`+` 前缀与收尾 reverse）。
func (r *exploreRunner) listRulePipeline(ctx context.Context, text, contentType, rule,
	baseURL, key string, page int, ruleName string) ([]interface{}, string, error) {
	value := strings.TrimSpace(rule)
	originalText := text
	analyzerKind := ""
	if strings.Contains(strings.ToLower(contentType), "json") && strings.TrimSpace(text) == "" {
		analyzerKind = "json"
	} else if _, err := parseJSONSmartText(originalText); err == nil {
		analyzerKind = "json"
	} else {
		analyzerKind = "html"
	}
	currentResult := interface{}(text)
	var pendingRoot interface{}
	pendingKind := ""
	hasPending := false

	reverse := strings.HasPrefix(value, "-")
	if strings.HasPrefix(value, "-") || strings.HasPrefix(value, "+") {
		value = strings.TrimSpace(value[1:])
	}
	var nodes []interface{}
	kind := "html"

	for {
		if strings.Contains(strings.ToLower(contentType), "json") &&
			strings.TrimSpace(pythonOrString(text)) == "" {
			nodes, kind = nil, "json"
			break
		}
		jsRule, remaining := ruleparser.CompileRuleProgram(strings.TrimSpace(value), false).LeadingJS()
		if jsRule != "" {
			transformed, err := r.evalJS(jsRule, evalBinding{
				key: key, page: page, baseURL: baseURL, result: currentResult, ruleName: ruleName})
			if err != nil {
				return nil, "", err
			}
			currentResult = transformed
			text = stageText(transformed)
			value = remaining
			if value != "" {
				program := ruleparser.CompileRuleProgram(value, analyzerKind == "json")
				if len(program.Stages) > 0 && program.Stages[0].Mode != "js" &&
					program.Stages[0].Mode != "webjs" {
					root, nextKind, _, err := payloadForCompiledStage(transformed, value, analyzerKind)
					if err != nil {
						return nil, "", err
					}
					pendingRoot, pendingKind, hasPending = root, nextKind, true
				}
				continue
			}
			out, outKind, _, err := materializeLeadingJS(transformed, text, contentType)
			if err != nil {
				return nil, "", err
			}
			nodes, kind = out, outKind
			break
		}

		var root interface{}
		if hasPending {
			root, kind, hasPending = pendingRoot, pendingKind, false
		} else if strings.Contains(strings.ToLower(contentType), "json") ||
			strings.HasPrefix(lstripPython(text), "{") ||
			strings.HasPrefix(lstripPython(text), "[") {
			if parsed, err := parseJSONSmartText(text); err == nil {
				root, kind = parsed, "json"
			} else {
				document, markupErr := ruleparser.ParseHTML(text)
				if markupErr != nil {
					return nil, "", markupErr
				}
				root, kind = document, "html"
			}
		} else {
			document, err := ruleparser.ParseHTML(text)
			if err != nil {
				return nil, "", err
			}
			root, kind = document, "html"
		}
		selector, suffix := fieldParts(value, kind)
		if selector != "" {
			items, err := ruleparser.Evaluate(root, selector, kind, true)
			if err != nil {
				return nil, "", err
			}
			nodes = items
		} else {
			nodes = listifyRoot(root)
		}
		if suffix == "" {
			break
		}
		var script string
		if strings.HasPrefix(suffix, "<js>") && strings.Contains(suffix, "</js>") {
			end := strings.Index(suffix, "</js>") + 5
			script, value = suffix[:end], strings.TrimSpace(suffix[end:])
		} else {
			script, value = suffix, ""
		}
		binding := evalBinding{key: key, page: page, baseURL: baseURL, ruleName: ruleName}
		if kind == "html" {
			rendered := make([]string, 0, len(nodes))
			interfaceItems := make([]interface{}, 0, len(nodes))
			for _, node := range nodes {
				rendered = append(rendered, nodeAsString(node))
				interfaceItems = append(interfaceItems, nodeAsString(node))
			}
			binding.result = interfaceItems
			binding.resultNodes = rendered
			binding.nodesSet = true
		} else {
			binding.result = nodes
		}
		transformed, err := r.evalJS(script, binding)
		if err != nil {
			return nil, "", err
		}
		currentResult = transformed
		text = stageText(transformed)
		if value != "" {
			program := ruleparser.CompileRuleProgram(value, analyzerKind == "json")
			if len(program.Stages) > 0 && program.Stages[0].Mode != "js" &&
				program.Stages[0].Mode != "webjs" {
				nextRoot, nextKind, _, stageErr := payloadForCompiledStage(transformed, value, analyzerKind)
				if stageErr != nil {
					return nil, "", stageErr
				}
				pendingRoot, pendingKind, hasPending = nextRoot, nextKind, true
			}
			continue
		}
		out, outKind, _, err := materializeSuffixStage(transformed, text, contentType)
		if err != nil {
			return nil, "", err
		}
		nodes, kind = out, outKind
		break
	}
	if reverse {
		for i, j := 0, len(nodes)-1; i < j; i, j = i+1, j-1 {
			nodes[i], nodes[j] = nodes[j], nodes[i]
		}
	}
	return nodes, kind, nil
}

// ---------------------------------------------------------------------------
// Part 2 · 字段链（对齐 _field_value 的 (a)-(e) 五段）
// ---------------------------------------------------------------------------

// fieldScope 一次字段取值的绑定上下文。hasRaw 对齐 Python 的
// `raw_result is not None`——空串是"已绑定"，不是"未设置"。
type fieldScope struct {
	kind      string
	baseURL   string
	key       string
	page      int
	ruleName  string
	rawResult interface{}
	hasRaw    bool
}

// initialResult 对齐 `raw_result if raw_result is not None else (node if
// kind=="json" else str(node))`。
func initialResult(node interface{}, scope fieldScope) interface{} {
	if scope.hasRaw {
		return scope.rawResult
	}
	if scope.kind == "json" {
		return node
	}
	return nodeAsString(node)
}

// resultHTMLNodes 对齐 `raw_result is None and kind=="html" and
// hasattr(node,"select")`。
func resultHTMLNodes(node interface{}, scope fieldScope) bool {
	return !scope.hasRaw && scope.kind == "html" && nodeHasSelect(node)
}

func hasAnyFoldPrefix(value string, prefixes ...string) bool {
	for _, prefix := range prefixes {
		if len(value) >= len(prefix) && strings.EqualFold(value[:len(prefix)], prefix) {
			return true
		}
	}
	return false
}

var getVariableRE = regexp.MustCompile(`@get:\{([^{}]+)\}`)

// fieldValue 是字段规则的取值口，返回文本（对齐 Python 恒返 str 的口径）。
func (r *exploreRunner) fieldValue(node interface{}, rule string, scope fieldScope) (string, error) {
	rule, putRule := ruleparser.SplitPutRules(rule)
	if putRule != "" {
		if err := r.putRuleValues(node, putRule, scope); err != nil {
			return "", err
		}
	}

	if strings.Contains(rule, "{{") && !strings.Contains(rule, "{{@@") {
		rendered, err := r.renderFieldExpressions(node, rule, scope)
		if err != nil {
			return "", err
		}
		strippedRule := strings.TrimSpace(rule)
		if strings.HasPrefix(strippedRule, "@js:") ||
			strings.HasPrefix(strippedRule, "<js>") ||
			strings.Index(rendered, "@js:") > 0 {
			rule = rendered
		} else {
			jsMarker := strings.Index(rendered, "<js>")
			if jsMarker >= 0 && strings.Contains(rendered[jsMarker:], "</js>") {
				nested := fieldScope{kind: scope.kind, baseURL: scope.baseURL,
					key: scope.key, page: scope.page,
					rawResult: strings.TrimSpace(rendered[:jsMarker]), hasRaw: true}
				value, nestedErr := r.fieldValue(node, rendered[jsMarker:], nested)
				if nestedErr != nil {
					return "", nestedErr
				}
				return strings.TrimSpace(value), nil
			}
			pieces := strings.Split(rendered, "##")
			value := pieces[0]
			if len(pieces) > 1 && pieces[1] != "" {
				count := 0
				if len(pieces) > 3 {
					count = 1
				}
				replacement := ""
				if len(pieces) > 2 {
					replacement = pieces[2]
				}
				if replaced, regexErr := ruleparser.RegexSubKeep(pieces[1], replacement, value, count); regexErr == nil {
					value = replaced
				}
			}
			return strings.TrimSpace(value), nil
		}
	}

	if stripped := strings.TrimSpace(rule); strings.HasPrefix(stripped, "<js>") &&
		strings.Contains(stripped, "</js>") {
		end := strings.Index(stripped, "</js>") + 5
		script := stripped[:end]
		remaining := strings.TrimSpace(stripped[end:])
		body := strings.TrimSpace(stripped[4 : end-5])
		initial := initialResult(node, scope)
		if strings.HasPrefix(body, "##") {
			transformed := ruleparser.ApplyRegexSuffix(pythonStr(initial), strings.Split(body, "##"))
			if remaining == "" {
				return strings.TrimSpace(transformed), nil
			}
			nested := scope
			nested.rawResult, nested.hasRaw = transformed, true
			if parsed, parseErr := parseJSONSmartText(transformed); parseErr == nil {
				nested.kind = "json"
				return r.fieldValue(parsed, remaining, nested)
			}
			document, markupErr := ruleparser.ParseHTML(transformed)
			if markupErr != nil {
				return "", markupErr
			}
			nested.kind = "html"
			return r.fieldValue(document, remaining, nested)
		}
		binding := evalBinding{key: scope.key, page: scope.page, baseURL: scope.baseURL,
			ruleName: scope.ruleName, result: initial, resultHTML: resultHTMLNodes(node, scope)}
		transformed, err := r.evalJS(script, binding)
		if err != nil {
			return "", err
		}
		if remaining == "" {
			if transformed == nil {
				return "", nil
			}
			return pythonStr(transformed), nil
		}
		if strings.HasPrefix(remaining, "##") {
			text := ""
			if transformed != nil {
				text = pythonStr(transformed)
			}
			return strings.TrimSpace(ruleparser.ApplyRegexSuffix(text, strings.Split(remaining, "##"))), nil
		}
		stageRoot, stageKind, stageResult, stageErr := payloadForCompiledStage(transformed, remaining, scope.kind)
		if stageErr != nil {
			return "", stageErr
		}
		nested := scope
		nested.kind = stageKind
		nested.rawResult, nested.hasRaw = stageResult, true
		return r.fieldValue(stageRoot, remaining, nested)
	}

	if matches := getVariableRE.FindAllStringSubmatchIndex(rule, -1); len(matches) > 0 {
		interpolated := getVariableRE.ReplaceAllStringFunc(rule, func(match string) string {
			groups := getVariableRE.FindStringSubmatch(match)
			return r.re.getVariableValue(r.source, groups[1])
		})
		value := strings.TrimSpace(interpolated)
		positions := []int{}
		if position := strings.Index(value, "@js:"); position >= 0 {
			positions = append(positions, position)
		}
		if position := strings.Index(value, "<js>"); position >= 0 {
			positions = append(positions, position)
		}
		jsPosition := -1
		for index, position := range positions {
			if index == 0 || position < jsPosition {
				jsPosition = position
			}
		}
		switch {
		case jsPosition == 0:
			// `@get` 只是 JS 段内的预处理，继续走正常 JS 管线。
			rule = value
		case jsPosition > 0:
			nested := scope
			nested.rawResult, nested.hasRaw = strings.TrimSpace(value[:jsPosition]), true
			return r.fieldValue(node, value[jsPosition:], nested)
		default:
			pieces := strings.Split(value, "##")
			out := pieces[0]
			if len(pieces) >= 2 && pieces[1] != "" {
				replacement := ""
				if len(pieces) > 2 {
					replacement = pieces[2]
				}
				if replaced, regexErr := ruleparser.RegexSubKeep(pieces[1], replacement, out, 0); regexErr == nil {
					out = replaced
				}
			}
			return strings.TrimSpace(out), nil
		}
	}

	selector, postJS := fieldParts(rule, scope.kind)
	var value interface{}
	if selector == "" && postJS != "" {
		if scope.hasRaw {
			value = scope.rawResult
		} else {
			value = node
		}
	} else {
		text, err := ruleparser.ExtractValue(node, selector, scope.kind, scope.baseURL, true)
		if err != nil {
			return "", err
		}
		value = text
	}
	remaining := strings.TrimSpace(postJS)
	for remaining != "" {
		if strings.HasPrefix(remaining, "<js>") && strings.Contains(remaining, "</js>") {
			end := strings.Index(remaining, "</js>") + 5
			script := remaining[:end]
			remaining = strings.TrimSpace(remaining[end:])
			body := strings.TrimSpace(script[4 : len(script)-6])
			if strings.HasPrefix(body, "##") {
				value = ruleparser.ApplyRegexSuffix(pythonStr(value), strings.Split(body, "##"))
				continue
			}
			binding := evalBinding{key: scope.key, page: scope.page, baseURL: scope.baseURL,
				ruleName: scope.ruleName, result: value,
				resultHTML: selector == "" && resultHTMLNodes(node, scope)}
			transformed, err := r.evalJS(script, binding)
			if err != nil {
				return "", err
			}
			if transformed == nil {
				value = ""
			} else {
				value = transformed
			}
			continue
		}
		if hasAnyFoldPrefix(remaining, "@webjs:") {
			// WebView 字段段在 Go 引擎里不可执行（设计稿 §7）：字段级显式报错，
			// tolerate 名单内的字段回空、name/author/bookUrl 升到列表级错误。
			return "", &ruleparser.UnsupportedError{Reason: "字段需要 WebView 渲染（暂不支持）"}
		}
		if strings.HasPrefix(remaining, "@js:") {
			script, regexSuffix := remaining, ""
			if index := strings.Index(remaining, "##"); index >= 0 {
				script, regexSuffix = remaining[:index], "##"+remaining[index+2:]
			}
			binding := evalBinding{key: scope.key, page: scope.page, baseURL: scope.baseURL,
				ruleName: scope.ruleName, result: value,
				resultHTML: selector == "" && resultHTMLNodes(node, scope)}
			transformed, err := r.evalJS(script, binding)
			if err != nil {
				return "", err
			}
			value = transformed
			if value == nil {
				value = ""
			}
			if regexSuffix != "" {
				text := ""
				if value != nil {
					text = pythonStr(value)
				}
				value = ruleparser.ApplyRegexSuffix(text, strings.Split(regexSuffix, "##"))
			}
			remaining = ""
			continue
		}
		// JS 之后的选择器分析的是变换后的载荷。
		stageRoot, stageKind, stageResult, stageErr := payloadForCompiledStage(value, remaining, scope.kind)
		if stageErr != nil {
			return "", stageErr
		}
		nested := scope
		nested.kind = stageKind
		nested.rawResult, nested.hasRaw = stageResult, true
		// Python 在这一层递归里没有传 rule_name（对齐 oracle：字段名不进日志）。
		nested.ruleName = ""
		text, err := r.fieldValue(stageRoot, remaining, nested)
		if err != nil {
			return "", err
		}
		value = text
		remaining = ""
	}
	if scope.baseURL != "" {
		if text, ok := value.(string); ok &&
			(strings.HasPrefix(text, "/") || strings.HasPrefix(text, "./") ||
				strings.HasPrefix(text, "../") || strings.HasPrefix(text, "//")) {
			value = ruleparser.URLJoin(scope.baseURL, text)
		}
	}
	if value == nil {
		return "", nil
	}
	if text, ok := value.(string); ok {
		return text, nil
	}
	return pythonStr(value), nil
}

// renderFieldExpressions 对齐 `re.sub(r"\{\{([\s\S]*?)\}\}", render_expression)`
// 的三分支（`@@`/`@` 走选择器，其余当 JS）。
func (r *exploreRunner) renderFieldExpressions(node interface{}, rule string, scope fieldScope) (string, error) {
	var builder strings.Builder
	cursor := 0
	for _, match := range templateExpressionRE.FindAllStringSubmatchIndex(rule, -1) {
		builder.WriteString(rule[cursor:match[0]])
		expression := strings.TrimSpace(rule[match[2]:match[3]])
		lowered := strings.ToLower(expression)
		text, err := func() (string, error) {
			switch {
			case strings.HasPrefix(expression, "@@"):
				return ruleparser.ExtractValue(node, expression[2:], scope.kind, scope.baseURL, true)
			case strings.HasPrefix(expression, "@") &&
				!hasAnyFoldPrefix(lowered, "@js:", "@json:", "@xpath:", "@css:"):
				return ruleparser.ExtractValue(node, expression[1:], scope.kind, scope.baseURL, true)
			case strings.HasPrefix(expression, "$") || strings.HasPrefix(expression, "//") ||
				hasAnyFoldPrefix(lowered, "@json:", "@xpath:", "@css:"):
				return ruleparser.ExtractValue(node, expression, scope.kind, scope.baseURL, true)
			}
			binding := evalBinding{key: scope.key, page: scope.page, baseURL: scope.baseURL,
				ruleName: scope.ruleName, result: initialResult(node, scope),
				resultHTML: resultHTMLNodes(node, scope)}
			value, evalErr := r.evalJS("@js:"+expression, binding)
			if evalErr != nil {
				return "", evalErr
			}
			if value == nil {
				return "", nil
			}
			return pythonStr(value), nil
		}()
		if err != nil {
			return "", err
		}
		builder.WriteString(text)
		cursor = match[1]
	}
	builder.WriteString(rule[cursor:])
	return builder.String(), nil
}

// putRuleValues 对齐 `_put_rule_values`：`@put:{a:rule,b:rule}` 逐键求值进变量袋。
// 浏览链上 book/chapter 恒为 None，故写入的是引擎变量袋（Python self.js_variables）。
func (r *exploreRunner) putRuleValues(node interface{}, putRule string, scope fieldScope) error {
	body := strings.TrimSpace(putRule)
	if len(body) >= 2 && strings.HasPrefix(body, "{") && strings.HasSuffix(body, "}") {
		body = body[1 : len(body)-1]
	}
	for _, item := range splitBalancedText(body, ",") {
		name, nestedRule, found := strings.Cut(item, ":")
		if !found {
			continue
		}
		name = strings.Trim(strings.TrimSpace(name), "'\"")
		nestedRule = strings.Trim(strings.TrimSpace(nestedRule), "'\"")
		if name == "" {
			continue
		}
		value, err := r.fieldValue(node, nestedRule, scope)
		if err != nil {
			return err
		}
		r.re.putVariable(r.source, name, value)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Part 3 · 书籍装配（对齐 _book_from_node / _books_from_nodes / explore_document）
// ---------------------------------------------------------------------------

// 空白集两套必须分开：Legado 规则里的 {space} 是显式 `[ \t\n\x0B\f\r]`（纯 ASCII），
// 而 Python re 在 str 上的 \s/\S 是 Unicode 感知（还含 \x1c-\x1f、\x85、\xa0、
// \u2000-\u200a、\u3000 等）。RE2 的 \s 只有 ASCII，所以取反那一处必须把整集显式
// 列出——否则「　作者…」（全角空格开头）这类书名剥不掉。
const (
	legadoSpaceClass = `[\x09-\x0d\x20]`
	pySpaceChars     = `\x09-\x0d\x1c-\x1f\x20\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}` +
		`\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`
)

var (
	bookNameCleanRE = regexp.MustCompile(legadoSpaceClass + `+作` + legadoSpaceClass +
		`*者.*|` + legadoSpaceClass + `+[^` + pySpaceChars + `]+` + legadoSpaceClass + `+著`)
	authorCleanRE = regexp.MustCompile(`^` + legadoSpaceClass + `*作` + legadoSpaceClass +
		`*者[:：\x09-\x0d\x20]+|` + legadoSpaceClass + `+著`)
	asciiTrimRE    = regexp.MustCompile(`^[\x00-\x20]+|[\x00-\x20]+$`)
	wordCountIntRE = regexp.MustCompile(`^-?[0-9]+$`)
)

// rawIntroPrefixes：带这些标记的简介存原文，显示时才解包。
var rawIntroPrefixes = []string{"<usehtml>", "<md>", "<useweb>"}

func ruleText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

// formatBookName / formatBookAuthorText 对齐 `_format_book_name`/`_format_book_author`。
func formatBookName(value string) string {
	return asciiTrimRE.ReplaceAllString(bookNameCleanRE.ReplaceAllString(value, ""), "")
}

func formatBookAuthorText(value string) string {
	return asciiTrimRE.ReplaceAllString(authorCleanRE.ReplaceAllString(value, ""), "")
}

// formatBookIntroText 对齐 `_format_book_intro`：带渲染标记的载荷整段原样留
// （lstrip 之后），其余走 `_strip_html_tags`——注意格式化吃的是**原文**不是 lstrip 后的。
func formatBookIntroText(value string) string {
	trimmed := lstripPython(value)
	for _, prefix := range rawIntroPrefixes {
		if strings.HasPrefix(trimmed, prefix) {
			return trimmed
		}
	}
	return ruleparser.StripHTML(value)
}

// formatWordCount 对齐 `_format_word_count`：非纯整数原样返回；超出 Java Int 报错，
// 由调用处按 Python 的 except (ValueError, OverflowError) 回落空串。「x.x 万字」按
// Legado 的历史口径先过一遍 float32 再定点一位（半值会落在小数形式之下）。
func formatWordCount(value string) (string, error) {
	if !wordCountIntRE.MatchString(value) {
		return value, nil
	}
	number, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return "", fmt.Errorf("字数超出 Java Int：%s", value)
	}
	if number < -2147483648 || number >= 2147483648 {
		return "", fmt.Errorf("字数超出 Java Int：%d", number)
	}
	if number <= 0 {
		return "", nil
	}
	if number <= 10000 {
		return strconv.FormatInt(number, 10) + "字", nil
	}
	quotient := float64(float32(float64(number) / 10000))
	rendered := strings.TrimRight(
		strings.TrimRight(strconv.FormatFloat(quotient, 'f', 1, 64), "0"), ".")
	return rendered + "万字", nil
}

// bookTypeFromSource 对齐 `_book_type`：legacy BookSourceType → 位标志 BookType。
func bookTypeFromSource(source *models.BookSource) int {
	if source == nil {
		return 8
	}
	switch int(source.BookSourceType) {
	case 1:
		return 32
	case 2:
		return 64
	case 3:
		return 8 | 128
	}
	return 8
}

// ruleStructToMap 把类型化规则段还原成 Python 侧的 dict 形状——合并语义
// （ruleSearch 打底、ruleExplore 非空覆盖）只能在键面上做，而两张表同构，
// json tag 就是那套键名。nil 指针段经 omitempty 直接缺键，与 Python 的 None
// 在同一个判据（falsy）下同形。
func ruleStructToMap(rule interface{}) map[string]string {
	out := map[string]string{}
	if rule == nil {
		return out
	}
	value := reflect.ValueOf(rule)
	if value.Kind() == reflect.Ptr && value.IsNil() {
		return out
	}
	raw, err := json.Marshal(rule)
	if err != nil {
		return out
	}
	var fields map[string]interface{}
	if err := json.Unmarshal(raw, &fields); err != nil {
		return out
	}
	for key, item := range fields {
		if text, ok := item.(string); ok {
			out[key] = text
		}
	}
	return out
}

// mergedExploreRules 对齐 `explore_document` 开头两行：ruleSearch 打底 +
// ruleExplore 非空覆盖，返回（规则面, 列表规则所属段名）。段名进 rule_name，
// 报错时界面才能说清是哪一格规则出的问题。
func mergedExploreRules(source *models.BookSource) (map[string]string, string) {
	if source == nil {
		return map[string]string{}, "ruleSearch"
	}
	rules := ruleStructToMap(source.RuleSearch)
	explore := ruleStructToMap(source.RuleExplore)
	for key, value := range explore {
		if value != "" {
			rules[key] = value
		}
	}
	scope := "ruleSearch"
	if explore["bookList"] != "" {
		scope = "ruleExplore"
	}
	return rules, scope
}

// bookFromNode 对齐 `_book_from_node`。字段顺序照抄——JS 的 java.put 会写变量袋，
// 求值顺序就是副作用顺序；name/author/bookUrl 的错误向上抛（0 号原则：列表面
// 要看得见原因），intro/coverUrl/kind/lastChapter/wordCount 按 tolerate 名单吞回空。
func (r *exploreRunner) bookFromNode(node interface{}, kind string, rules map[string]string,
	baseURL, key string, page int, rulePrefix string) (*models.BookItem, error) {

	field := func(name string, withBase, tolerate bool) (string, error) {
		primary := rules[name]
		if primary == "" {
			return "", nil
		}
		scope := fieldScope{kind: kind, key: key, page: page,
			ruleName: rulePrefix + "." + name}
		if withBase {
			scope.baseURL = baseURL
		}
		value, err := r.fieldValue(node, primary, scope)
		if err != nil {
			if tolerate && tolerateFieldError(err) {
				return "", nil
			}
			return "", err
		}
		return value, nil
	}

	name, err := field("name", false, false)
	if err != nil {
		return nil, err
	}
	author, err := field("author", false, false)
	if err != nil {
		return nil, err
	}
	intro, err := field("intro", false, true)
	if err != nil {
		return nil, err
	}
	bookURL, err := field("bookUrl", true, false)
	if err != nil {
		return nil, err
	}
	if bookURL == "" {
		bookURL = baseURL
	}
	coverURL, err := field("coverUrl", true, true)
	if err != nil {
		return nil, err
	}
	kindValue, err := field("kind", false, true)
	if err != nil {
		return nil, err
	}
	lastChapter, err := field("lastChapter", false, true)
	if err != nil {
		return nil, err
	}
	wordCountRaw, err := field("wordCount", false, true)
	if err != nil {
		return nil, err
	}
	wordCount, wordErr := formatWordCount(wordCountRaw)
	if wordErr != nil {
		wordCount = ""
	}

	book := &models.BookItem{
		Name:               formatBookName(name),
		Author:             formatBookAuthorText(author),
		Intro:              formatBookIntroText(intro),
		URL:                bookURL,
		CoverURL:           coverURL,
		Kind:               kindValue,
		LastChapter:        lastChapter,
		LatestChapterTitle: lastChapter,
		WordCount:          wordCount,
		Origin:             originOf(r.source),
		OriginName:         originNameOf(r.source),
		Type:               bookTypeFromSource(r.source),
		OriginOrder:        customOrderOf(r.source),
	}
	book.Variables = r.re.variablesSnapshot(r.source)
	return book, nil
}

func originOf(source *models.BookSource) string {
	if source == nil {
		return ""
	}
	return source.BookSourceURL
}

func originNameOf(source *models.BookSource) string {
	if source == nil {
		return ""
	}
	return source.BookSourceName
}

func customOrderOf(source *models.BookSource) int {
	if source == nil {
		return 0
	}
	return source.CustomOrder
}

// booksFromNodes 对齐 `_books_from_nodes`：严格规则只命中一个容器时按字段规则
// 批量提取、按位置组合（这条路上的书**不走** format、也不带 origin——与 Python
// 逐字一致，界面用当前源补 origin，见 find_books_notifier 的 currentSourceUrl 回落）。
func (r *exploreRunner) booksFromNodes(nodes []interface{}, kind string, rules map[string]string,
	baseURL, key string, page int, listPut, rulePrefix string) ([]*models.BookItem, error) {
	if kind == "html" && len(nodes) == 1 {
		names, err := ruleparser.ExtractValues(nodes[0], rules["name"], kind, "")
		if err != nil {
			return nil, err
		}
		urls, err := ruleparser.ExtractValues(nodes[0], rules["bookUrl"], kind, baseURL)
		if err != nil {
			return nil, err
		}
		if len(names) >= 2 && len(urls) >= 2 {
			authors, err := ruleparser.ExtractValues(nodes[0], rules["author"], kind, "")
			if err != nil {
				return nil, err
			}
			intros, err := ruleparser.ExtractValues(nodes[0], rules["intro"], kind, "")
			if err != nil {
				return nil, err
			}
			covers, err := ruleparser.ExtractValues(nodes[0], rules["coverUrl"], kind, baseURL)
			if err != nil {
				return nil, err
			}
			count := minInt(len(names), len(urls))
			books := make([]*models.BookItem, 0, count)
			for index := 0; index < count; index++ {
				book := &models.BookItem{Name: names[index], URL: urls[index]}
				if index < len(authors) {
					book.Author = authors[index]
				}
				if index < len(intros) {
					book.Intro = intros[index]
				}
				if index < len(covers) {
					book.CoverURL = covers[index]
				}
				books = append(books, book)
			}
			return books, nil
		}
	}
	books := make([]*models.BookItem, 0, len(nodes))
	for _, node := range nodes {
		if listPut != "" {
			// Python 这里传的 raw_result=str(node)、rule_name 留空。
			scope := fieldScope{kind: kind, baseURL: baseURL, key: key, page: page,
				rawResult: nodeAsString(node), hasRaw: true}
			if err := r.putRuleValues(node, listPut, scope); err != nil {
				return nil, err
			}
		}
		book, err := r.bookFromNode(node, kind, rules, baseURL, key, page, rulePrefix)
		if err != nil {
			return nil, err
		}
		books = append(books, book)
	}
	return books, nil
}

// dedupBooksByNameURL 对齐两条链收尾的同一句去重：有书名且 URL 没见过。
// 空 URL 只可能留一条（seen[""] 一旦落下，后面的空 URL 全被吞）。
func dedupBooksByNameURL(books []*models.BookItem) []*models.BookItem {
	out := make([]*models.BookItem, 0, len(books))
	seen := make(map[string]bool, len(books))
	for _, book := range books {
		if book.Name == "" || seen[book.URL] {
			continue
		}
		seen[book.URL] = true
		out = append(out, book)
	}
	return out
}

// detailBookFromText 对齐 `_detail_book_from_response` 中**没有** `_initialize_detail`
// 的那一支（ReaderEngine 形状）：整页物化 → ruleBookInfo 逐字段 → 覆盖 url/infoHtml。
// 已知偏差（记入本轮日志，不在设计稿验收面内）：Python 桌面/检测/服务三面跑的是
// LegadoReadingEngine，它会先执行 ruleBookInfo.init 再取值；Go 引擎没有 init 段，
// 靠 init 播变量（@put）的源在详情页回落这一条上会少那一步。
func (r *exploreRunner) detailBookFromText(text, contentType, finalURL, bookURL string) (*models.BookItem, error) {
	var root interface{}
	kind := "html"
	if strings.Contains(strings.ToLower(contentType), "json") {
		parsed, err := parseJSONSmartText(text)
		if err != nil {
			return nil, err
		}
		switch parsed.(type) {
		case map[string]interface{}, []interface{}:
			kind = "json"
		}
		root = parsed
	} else {
		document, err := ruleparser.ParseHTML(text)
		if err != nil {
			return nil, err
		}
		root = document
	}
	book, err := r.bookFromNode(root, kind, ruleStructToMap(bookInfoRules(r.source)),
		finalURL, "", 1, "ruleBookInfo")
	if err != nil {
		return nil, err
	}
	book.URL = bookURL
	book.InfoHTML = text
	return book, nil
}

func bookInfoRules(source *models.BookSource) interface{} {
	if source == nil {
		return nil
	}
	return source.RuleBookInfo
}

// exploreDocument 用完整规则面解析一页已下载的浏览结果。
func (r *exploreRunner) exploreDocument(ctx context.Context, text, contentType, finalURL string,
	page int) ([]*models.BookItem, error) {
	rules, listScope := mergedExploreRules(r.source)
	listRule, listPut := ruleparser.SplitPutRules(rules["bookList"])
	nodes, kind, err := r.listRulePipeline(ctx, text, contentType, listRule, finalURL, "", page,
		listScope+".bookList")
	if err != nil {
		return nil, err
	}
	if len(nodes) == 0 && ruleText(r.source.BookURLPattern) == "" {
		// Legado 把"列表规则一条没中"的页面猜成详情页；猜错时 Python 是一句
		// bare `except Exception: return []`。0 号原则不许静默，故落一行日志再回空。
		book, detailErr := r.detailBookFromText(text, contentType, finalURL, finalURL)
		if detailErr != nil {
			log.Printf("[explore] 按详情页解析失败，该分类回空（%s）：%v", finalURL, detailErr)
			return nil, nil
		}
		if book == nil || book.Name == "" {
			return nil, nil
		}
		return []*models.BookItem{book}, nil
	}
	books, err := r.booksFromNodes(nodes, kind, rules, finalURL, "", page, listPut, listScope)
	if err != nil {
		return nil, err
	}
	return dedupBooksByNameURL(books), nil
}

// Explore 是浏览链的入口：分类地址（原样，含 @js:/条件分页/<a,b>/POST 选项等
// 任意形状）→ 请求组装 → 抓取 → exploreDocument。预解析会让未覆盖的形状静默
// 抽空，所以调用方**不许**先把 {{page}} 之类替换掉再传进来。
func (re *RuleEngine) Explore(ctx context.Context, source *models.BookSource,
	categoryURL string, page int) ([]*models.BookItem, error) {
	if source == nil {
		return nil, fmt.Errorf("浏览失败：书源为空")
	}
	runner := re.newExploreRunner(source)
	request, err := runner.resolveCategoryRequest(categoryURL, page)
	if err != nil {
		return nil, err
	}
	text, contentType, finalURL, err := runner.fetchCategory(ctx, request)
	if err != nil {
		return nil, err
	}
	return runner.exploreDocument(ctx, text, contentType, finalURL, page)
}
