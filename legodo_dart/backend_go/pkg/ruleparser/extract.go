package ruleparser

import (
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"golang.org/x/net/html"
)

// 本文件是 Python `app/services/engine/rule_engine.py` 字段取值族的逐行移植：
// extract_values / extract_value / _extract_single_value / _capture_value /
// _rule_uses_abs_attr / _replace_jsonpath_embeds / _legado_value_string /
// _json_smart_array_string / _java_map_child_string。列表管线（explore.go）与
// 字段链都用这里的口径；JS 分支在这里一律抛 UnsupportedError，由调用方按
// Python 的 tolerate 名单决定吞还是传。

// IsJSRule 对齐 Python `_is_js`：`@js:`/`@webjs:`（忽略大小写）前缀、`<js`
// 前缀（**大小写敏感**，Python 如此）或文本含 `java.` 都算需要 JS 引擎。
func IsJSRule(value string) bool {
	value = strings.TrimSpace(value)
	lowered := strings.ToLower(value)
	return strings.HasPrefix(lowered, "@js:") || strings.HasPrefix(lowered, "@webjs:") ||
		strings.HasPrefix(value, "<js") || strings.Contains(value, "java.")
}

// URLJoin 对齐 Python urljoin：解析失败回原值，绝不抛。
func URLJoin(baseURL, reference string) string {
	if reference == "" || baseURL == "" {
		return reference
	}
	base, err := url.Parse(baseURL)
	if err != nil {
		return reference
	}
	ref, err := url.Parse(reference)
	if err != nil {
		return reference
	}
	return base.ResolveReference(ref).String()
}

// hasRelativeURLPrefix 对齐 Python 的五个相对前缀：`/ ./ ../ // #`。
func hasRelativeURLPrefix(value string) bool {
	return strings.HasPrefix(value, "/") || strings.HasPrefix(value, "./") ||
		strings.HasPrefix(value, "../") || strings.HasPrefix(value, "//") ||
		strings.HasPrefix(value, "#")
}

// jsonQuote 对齐 Python json.dumps(str, ensure_ascii=False)（不转义 <>&）。
func jsonQuote(value string) string {
	var b strings.Builder
	encoder := json.NewEncoder(&b)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
	return strings.TrimSuffix(b.String(), "\n")
}

// ValueString 对齐 Python `_legado_value_string`（JSONPath 值的 Java toString 口径）：
// None→""、bool→true/false、dict→`{k=v, ...}`（键排序，Python 是插入序）、
// list→JSON 引号数组、Decimal/json.Number→原文、float 整值带 ".0"（Python str）。
func ValueString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case *Node:
		return typed.OuterHtml()
	case *Document:
		if roots := typed.RootNodes(); len(roots) > 0 {
			return roots[0].OuterHtml()
		}
		return ""
	case map[string]interface{}:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, key+"="+javaMapChildString(typed[key]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []interface{}:
		return jsonSmartArrayString(typed)
	case json.Number:
		return typed.String()
	case float64:
		if typed == float64(int64(typed)) {
			return strconv.FormatFloat(typed, 'f', 1, 64)
		}
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case int:
		return strconv.Itoa(typed)
	case int64:
		return strconv.FormatInt(typed, 10)
	case string:
		return typed
	default:
		return fmt.Sprintf("%v", typed)
	}
}

// jsonSmartArrayString 对齐 Python `_json_smart_array_string`：`[a,b]`，串值带
// JSON 引号、None→null、嵌套 dict 键值对无空格。
func jsonSmartArrayString(value []interface{}) string {
	parts := make([]string, 0, len(value))
	for _, item := range value {
		parts = append(parts, renderSmartItem(item))
	}
	return "[" + strings.Join(parts, ",") + "]"
}

func renderSmartItem(item interface{}) string {
	switch typed := item.(type) {
	case nil:
		return "null"
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case string:
		return jsonQuote(typed)
	case map[string]interface{}:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, jsonQuote(key)+":"+renderSmartItem(typed[key]))
		}
		return "{" + strings.Join(parts, ",") + "}"
	case []interface{}:
		return jsonSmartArrayString(typed)
	default:
		return ValueString(item)
	}
}

// javaMapChildString 对齐 Python `_java_map_child_string`：None→"null"（与
// _legado_value_string 的 None→"" 不同）、bool→true/false、dict/list 递归。
func javaMapChildString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return "null"
	case bool:
		if typed {
			return "true"
		}
		return "false"
	case map[string]interface{}:
		return ValueString(typed)
	case []interface{}:
		return jsonSmartArrayString(typed)
	case json.Number:
		return typed.String()
	case string:
		return typed
	default:
		return ValueString(value)
	}
}

// RegexSubKeep 对齐 Python java_regex.sub：count=0 表示全局替换；Java 风格
// `$1` 替换串由 regexp2 原生吃。pattern 非法返回 error（吞与否由调用方定）。
func RegexSubKeep(pattern, replacement, value string, count int) (string, error) {
	compiled, err := compileJavaRegex(pattern)
	if err != nil {
		return "", err
	}
	if count == 0 {
		count = -1
	}
	return compiled.Replace(value, replacement, 0, count)
}

// CaptureValue 对齐 Python `_capture_value`：规则含 `$n` 捕获令牌时把列表
// 元素按位填进模板（`$0` 保持字面、越界填空），非列表节点规则原样返回
// （SourceRule.makeUpRule 只对 List<String?> 做消费）；没命中捕获返回 false。
func CaptureValue(node interface{}, rule string) (string, bool) {
	capturePart, separator, suffix := rule, "", ""
	if idx := strings.Index(rule, "##"); idx >= 0 {
		capturePart, separator, suffix = rule[:idx], "##", rule[idx+2:]
	}
	if !captureProgramRE.MatchString(capturePart) {
		return "", false
	}
	var values []string
	switch typed := node.(type) {
	case []interface{}:
		values = make([]string, 0, len(typed))
		for _, item := range typed {
			values = append(values, captureItemString(item))
		}
	case []*Node:
		values = make([]string, 0, len(typed))
		for _, item := range typed {
			values = append(values, captureItemString(item))
		}
	default:
		// 非列表结果类型上捕获令牌保持字面，不会被消费。
		return rule, true
	}
	rendered := captureProgramRE.ReplaceAllStringFunc(capturePart, func(match string) string {
		index, err := strconv.Atoi(match[1:])
		if err != nil {
			return match
		}
		if index == 0 {
			return match
		}
		if index < len(values) {
			return values[index]
		}
		return ""
	})
	if separator != "" {
		return rendered + separator + suffix, true
	}
	return rendered, true
}

// captureItemString 近似 Python str()（None→""，bool 大写、节点给 HTML）。
func captureItemString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case bool:
		if typed {
			return "True"
		}
		return "False"
	case *Node:
		return typed.OuterHtml()
	default:
		return ValueString(value)
	}
}

// ExtractValues 对齐 Python extract_values：单容器批取（网站把多条记录包进
// 一个容器时逐字段抽全量）。capture 优先；随后逐 `||` 候选求值（**不传
// list_context**）、`##` 正则替换吞错保原值、相对地址补 base、空值丢弃；
// 一个候选有值即返回。
func ExtractValues(node interface{}, rule, kind, baseURL string) ([]string, error) {
	if rule == "" || IsJSRule(rule) {
		return nil, nil
	}
	if captured, ok := CaptureValue(node, rule); ok {
		value := strings.TrimSpace(captured)
		if baseURL != "" && hasRelativeURLPrefix(value) {
			value = URLJoin(baseURL, value)
		}
		if value == "" {
			return nil, nil
		}
		return []string{value}, nil
	}
	for _, candidate := range splitBalancedFields(rule, "||") {
		pieces := strings.Split(candidate, "##")
		targets, err := Evaluate(node, pieces[0], kind, false)
		if err != nil {
			return nil, err
		}
		values := make([]string, 0, len(targets))
		for _, target := range targets {
			value := renderExtractTarget(target)
			if len(pieces) >= 2 && pieces[1] != "" {
				replacement := ""
				if len(pieces) > 2 {
					replacement = pieces[2]
				}
				if rendered, err := RegexSubKeep(pieces[1], replacement, value, 0); err == nil {
					value = rendered
				}
			}
			value = strings.TrimSpace(value)
			if baseURL != "" && value != "" && hasRelativeURLPrefix(value) {
				value = URLJoin(baseURL, value)
			}
			if value != "" {
				values = append(values, value)
			}
		}
		if len(values) > 0 {
			return values, nil
		}
	}
	return nil, nil
}

// renderExtractTarget 对齐 Python `get_text(" ", strip=True) if hasattr` 分支。
func renderExtractTarget(target interface{}) string {
	if node, ok := target.(*Node); ok {
		return node.Text()
	}
	return ValueString(target)
}

// ExtractValue 对齐 Python extract_value：字段级单值取值全链。
func ExtractValue(node interface{}, rule, kind, baseURL string, unescapeHTML bool) (string, error) {
	decode := func(value string) string { return value }
	if unescapeHTML {
		decode = html.UnescapeString
	}
	if rule == "" {
		return "", nil
	}
	if strings.Contains(rule, "{baseUrl}") {
		return strings.TrimSpace(decode(strings.ReplaceAll(rule, "{baseUrl}", baseURL))), nil
	}
	if captured, ok := CaptureValue(node, rule); ok {
		captured = strings.TrimSpace(decode(captured))
		if baseURL != "" && hasRelativeURLPrefix(captured) {
			captured = URLJoin(baseURL, captured)
		}
		return captured, nil
	}
	if IsJSRule(rule) {
		return "", &UnsupportedError{Reason: "字段规则需要 JavaScript 引擎"}
	}
	if strings.Contains(rule, "{{@@") {
		value, err := replaceAllSubmatchE(rule, nestedExtractEmbedRE, func(expression string) (string, error) {
			values, err := ExtractValues(node, strings.TrimSpace(expression), kind, baseURL)
			if err != nil {
				return "", err
			}
			if len(values) == 0 {
				return "", nil
			}
			return values[0], nil
		})
		if err != nil {
			return "", err
		}
		value = strings.TrimSpace(decode(value))
		if baseURL != "" {
			value = URLJoin(baseURL, value)
		}
		return value, nil
	}
	if kind == "json" && (strings.Contains(rule, "{{") || strings.Contains(rule, "{$")) {
		value, err := ReplaceJSONPathEmbeds(rule, node)
		if err != nil {
			return "", err
		}
		value, err = replaceAllSubmatchE(value, jsonFieldEmbedRE, func(expression string) (string, error) {
			values, err := jsonValuesForEmbed(node, strings.TrimSpace(expression))
			if err != nil {
				return "", err
			}
			if len(values) == 0 {
				return "", nil
			}
			return ValueString(values[0]), nil
		})
		if err != nil {
			return "", err
		}
		value = decode(value)
		if baseURL != "" {
			value = URLJoin(baseURL, value)
		}
		return value, nil
	}
	candidates := splitBalancedFields(rule, "||")
	if len(candidates) == 0 {
		candidates = []string{rule}
	}
	value := ""
	for _, candidate := range candidates {
		single, err := extractSingleValue(node, candidate, kind, baseURL != "")
		if err != nil {
			return "", err
		}
		value = single
		if value != "" {
			break
		}
	}
	value = strings.TrimSpace(decode(value))
	lowered := strings.ToLower(rule)
	urlHint := strings.Contains(lowered, "url") || strings.Contains(lowered, "href") ||
		strings.Contains(lowered, "src")
	relativeHint := hasRelativeURLPrefix(value)
	absHint := ruleUsesAbsAttr(rule)
	if baseURL != "" && value != "" && (urlHint || relativeHint || absHint) &&
		!strings.HasPrefix(value, "data:") && !strings.HasPrefix(value, "javascript:") {
		value = URLJoin(baseURL, value)
	}
	return value, nil
}

// extractSingleValue 对齐 Python _extract_single_value。
func extractSingleValue(node interface{}, rule, kind string, firstOnly bool) (string, error) {
	selectorRule := rule
	if idx := strings.Index(rule, "##"); idx >= 0 {
		selectorRule = rule[:idx]
	}
	value := ""
	trimmed := strings.TrimSpace(selectorRule)
	// 旧书源常用裸属性名（onclick/href/style）直接读当前元素属性。
	if kind == "html" && trimmed != "" && bareAttrCandidateRE.MatchString(trimmed) {
		if target := attrReadTarget(node); target != nil {
			value = target.Attr(trimmed)
		}
	}
	if value == "" {
		values, err := Evaluate(node, selectorRule, kind, false)
		if err != nil {
			return "", err
		}
		if len(values) > 0 {
			rendered := make([]string, 0, len(values))
			for _, item := range values {
				rendered = append(rendered, renderExtractTarget(item))
			}
			if firstOnly {
				value = rendered[0]
			} else {
				value = strings.Join(rendered, "\n")
			}
		}
	}
	if strings.Contains(rule, "##") {
		pieces := strings.Split(rule, "##")
		if len(pieces) >= 2 && pieces[1] != "" {
			value = ApplyRegexSuffix(value, pieces)
		}
	}
	return strings.TrimSpace(value), nil
}

// attrReadTarget 裸属性名的读取目标（Python 里 bs4 节点的 `node.get`）。
func attrReadTarget(node interface{}) *Node {
	switch typed := node.(type) {
	case *Node:
		return typed
	case *Document:
		if roots := typed.RootNodes(); len(roots) > 0 {
			return roots[0]
		}
	}
	return nil
}

// ruleUsesAbsAttr 对齐 Python `_rule_uses_abs_attr`：最后的 `@` 属性段剥掉
// 可选 `attr.` 前缀后是否以 `abs:` 开头（jsoup abs:<attr> 的绝对化语义）。
func ruleUsesAbsAttr(rule string) bool {
	body := rule
	if idx := strings.Index(body, "##"); idx >= 0 {
		body = body[:idx]
	}
	if idx := strings.LastIndex(body, "@"); idx >= 0 {
		body = body[idx+1:]
	}
	body = strings.ToLower(strings.TrimSpace(body))
	body = strings.TrimPrefix(body, "attr.")
	return strings.HasPrefix(body, "abs:")
}

// jsonValuesForEmbed 对齐 Python `_json_values`。
func jsonValuesForEmbed(data interface{}, rule string) ([]interface{}, error) {
	selector := strings.TrimSpace(rule)
	if strings.HasPrefix(strings.ToLower(selector), "@json:") {
		selector = strings.TrimSpace(selector[strings.Index(selector, ":")+1:])
	}
	if selector == "" {
		if list, ok := data.([]interface{}); ok {
			return list, nil
		}
		return []interface{}{data}, nil
	}
	values, err := Evaluate(data, selector, "json", false)
	if err != nil {
		return nil, err
	}
	if len(values) == 1 {
		if list, ok := values[0].([]interface{}); ok {
			return list, nil
		}
	}
	return values, nil
}

// ReplaceJSONPathEmbeds 对齐 Python `_replace_jsonpath_embeds`：按 Legado
// RuleAnalyzer 的平衡扫描替换模板里的 `{$...}` 表达式（`{{$.` 双花括号跳过
// 内层；引号/转义/括号深度都参与配对；未闭合时其余原样保留）。
func ReplaceJSONPathEmbeds(template string, node interface{}) (string, error) {
	var b strings.Builder
	cursor := 0
	for {
		start := -1
		if idx := strings.Index(template[cursor:], "{$."); idx >= 0 {
			start = cursor + idx
		}
		for start > 0 && template[start-1] == '{' {
			next := -1
			if idx := strings.Index(template[start+2:], "{$."); idx >= 0 {
				next = start + 2 + idx
			}
			start = next
		}
		if start < 0 {
			b.WriteString(template[cursor:])
			break
		}
		b.WriteString(template[cursor:start])
		quote := byte(0)
		escaped := false
		depth := 1
		end := -1
		for index := start + 1; index < len(template); index++ {
			char := template[index]
			switch {
			case quote != 0:
				if escaped {
					escaped = false
				} else if char == '\\' {
					escaped = true
				} else if char == quote {
					quote = 0
				}
			case char == '\'' || char == '"':
				quote = char
			case char == '[' || char == '(' || char == '{':
				depth++
			case char == ']' || char == ')' || char == '}':
				depth--
				if char == '}' && depth == 0 {
					end = index
				}
			}
			if end >= 0 {
				break
			}
		}
		if end < 0 {
			b.WriteString(template[start:])
			break
		}
		values, err := jsonValuesForEmbed(node, strings.TrimSpace(template[start+1:end]))
		if err != nil {
			return "", err
		}
		if len(values) > 0 {
			b.WriteString(ValueString(values[0]))
		}
		cursor = end + 1
	}
	return b.String(), nil
}

// replaceAllSubmatchE 单捕获组替换：对齐 Python re.sub(pattern, fn, value) 的
// 逐匹配拼接（非贪心、从左到右、不重叠），fn 的错误可传播。
func replaceAllSubmatchE(value string, re *regexp.Regexp, fn func(match string) (string, error)) (string, error) {
	matches := re.FindAllStringSubmatchIndex(value, -1)
	if len(matches) == 0 {
		return value, nil
	}
	var b strings.Builder
	cursor := 0
	for _, match := range matches {
		rendered, err := fn(value[match[2]:match[3]])
		if err != nil {
			return "", err
		}
		b.WriteString(value[cursor:match[0]])
		b.WriteString(rendered)
		cursor = match[1]
	}
	b.WriteString(value[cursor:])
	return b.String(), nil
}

var (
	// `{{@@selector}}` 嵌套提取嵌入（非贪心、不跨行——Python re.sub 的 `.` 默认口径）。
	nestedExtractEmbedRE = regexp.MustCompile(`\{\{@@(.*?)\}\}`)
	// `{{$.path}}` JSONPath 嵌入（`[^{}]+` 可跨行）。
	jsonFieldEmbedRE = regexp.MustCompile(`\{\{([^{}]+)\}\}`)
	// 裸属性名（可作属性读的整词）。
	bareAttrCandidateRE = regexp.MustCompile(`^[A-Za-z_:][-A-Za-z0-9_:.]*$`)
)
