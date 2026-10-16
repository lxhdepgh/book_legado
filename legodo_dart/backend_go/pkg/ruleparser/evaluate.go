package ruleparser

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dlclark/regexp2"
	"golang.org/x/net/html"
	"github.com/antchfx/htmlquery"
)

// UnsupportedError 未覆盖的规则形状。0 号原则（docs/找书分类浏览修复方案.md §4）：
// 列表级遇到它显式报错让界面看到原因，字段级记日志回落空值——不许静默吞空。
type UnsupportedError struct{ Reason string }

func (e *UnsupportedError) Error() string { return e.Reason }

// Evaluate 求值一条 Legado 规则（对齐 Python legado_rule_parser.evaluate 的覆盖面）：
//   - `||` 惰性回退（首组有值即返回）；`&&` 结果拼接；`%%` 交错
//   - `$./$[`/`@json:` → JSONPath；`/`、`./`、`@xpath:` → XPath
//   - kind=json 或 JSON 载荷 → JSONPath；其余按 jsoup 选择器链
//
// listContext 标记列表级规则（bookList/chapterList）：Legado 用 getElements 解析，
// 任何 `@` 段都只当选择器；非列表口径下最后一个 `@` 段是 text/html/ownText/all
// 或属性名——列表规则里 `class.read@li@a` 是三层选择器，同一串在字段位里
// `a` 会被当属性名读。
func Evaluate(root interface{}, rule, kind string, listContext bool) ([]interface{}, error) {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return nil, nil
	}
	rule = jsoupTextSuffixRE.ReplaceAllString(rule, "@text")
	if strings.HasPrefix(rule, ":") {
		return nil, &UnsupportedError{Reason: fmt.Sprintf("正则模式规则（: 前缀）暂未支持：%s", rule)}
	}
	jsonRule := kind == "json" || strings.HasPrefix(rule, "$.") ||
		strings.HasPrefix(rule, "$[") || hasPrefixFold(rule, "@json:")
	parts, operator := splitRuleOperators(rule, jsonRule)
	if operator != "" {
		switch operator {
		case "||":
			for _, part := range parts {
				group, err := Evaluate(root, part, kind, listContext)
				if err != nil {
					return nil, err
				}
				if len(group) > 0 {
					return group, nil
				}
			}
			return nil, nil
		case "&&":
			var out []interface{}
			for _, part := range parts {
				group, err := Evaluate(root, part, kind, listContext)
				if err != nil {
					return nil, err
				}
				out = append(out, group...)
			}
			return out, nil
		default: // %%
			groups := make([][]interface{}, 0, len(parts))
			maxLen := 0
			for _, part := range parts {
				group, err := Evaluate(root, part, kind, listContext)
				if err != nil {
					return nil, err
				}
				groups = append(groups, group)
				if len(group) > maxLen {
					maxLen = len(group)
				}
			}
			out := make([]interface{}, 0)
			for i := 0; i < maxLen; i++ {
				for _, group := range groups {
					if i < len(group) {
						out = append(out, group[i])
					}
				}
			}
			return out, nil
		}
	}
	if kind == "json" {
		return jsonValues(root, rule, listContext)
	}
	if strings.HasPrefix(rule, "$.") || strings.HasPrefix(rule, "$[") || hasPrefixFold(rule, "@json:") {
		return jsonValues(jsonRootForRule(root), rule, listContext)
	}
	if strings.HasPrefix(rule, "/") || strings.HasPrefix(rule, "./") ||
		hasPrefixFold(rule, "@xpath:") || hasPrefixFold(rule, "@XPath:") {
		return xpathValues(root, rule)
	}
	if isJSONPayload(root) {
		return jsonValues(root, rule, listContext)
	}
	return jsoupValues(root, rule, listContext)
}

// ---------------------------------------------------------------------------
// jsoup 选择器链
// ---------------------------------------------------------------------------

var (
	jsoupTextSuffixRE = regexp.MustCompile(`::text\s*$`)
	// `[!?indices]` 括号下标（可带 ! 排除、逗号/冒号多段）
	bracketIndexRE = regexp.MustCompile(`\[(!?)([-\d,:\s]+)\]$`)
	// 裸排除：`.!0` 或 `!0`
	bareExcludeRE = regexp.MustCompile(`\.?(!)(-?\d+(?::-?\d+)*)$`)
	// 裸下标：`.0`、`.0:1`（不是括号形态时冒号是独立下标分隔符）
	bareIndexBodyRE = regexp.MustCompile(`\.(-?\d+(?::-?\d+)*)$`)
	// `[^xxx]` 属性选择器简写：Legado 侧等价 [class^=xxx]
	attrCaretShortcutRE = regexp.MustCompile(`\[\^([^\]]+)\]`)
	// Python `\)(?=[A-Za-z]...)` → `") "`：RE2 无 lookahead，改捕获组尾随其后重放
	parenJoinRE = regexp.MustCompile(`\)([A-Za-z][\w-]*(?:[.#\[]|@|$))`)
	// Python `\](?=[A-Za-z_$])` → `"].#"`（原文替换成 `].` 后接原字符）
	jsonBracketGlueRE = regexp.MustCompile(`\]([A-Za-z_$])`)
	// `@attr:regex("pattern")` 尾缀（Python jsoup_values 入口处先处理）
	jsoupRegexSuffixRE = regexp.MustCompile(`@([\w.:-]+):regex\(\s*(?:'([^']*)'|"([^"]*)")\s*\)\s*$`)
)

// parseJsoupPart 归一单个选择器段（对齐 Python _jsoup_part 的判读顺序）：
// 尾部下标/排除先摘（`tag.a.0`→选择集[0]、`em.0:1`→[0,1]、`[!0]`→排除），
// 再补 `:nth-of-type` 与 class./tag./id./text. 前缀。
func parseJsoupPart(part string) (selector, selection string, exclude bool) {
	part = strings.TrimSpace(part)
	part = attrCaretShortcutRE.ReplaceAllString(part, "[class^=$1]")
	part = parenJoinRE.ReplaceAllString(part, ") $1")
	if match := bracketIndexRE.FindStringSubmatch(part); match != nil {
		exclude = match[1] == "!"
		selection = match[2]
		part = part[:len(part)-len(match[0])]
	} else if match := bareExcludeRE.FindStringSubmatch(part); match != nil {
		exclude = true
		selection = strings.ReplaceAll(match[2], ":", ",")
		part = part[:len(part)-len(match[0])]
	} else if match := bareIndexBodyRE.FindStringSubmatch(part); match != nil {
		selection = strings.ReplaceAll(match[1], ":", ",")
		part = part[:len(part)-len(match[0])]
	}
	part = nthIndexRE.ReplaceAllStringFunc(part, func(match string) string {
		sub := nthIndexRE.FindStringSubmatch(match)
		index, _ := strconv.Atoi(sub[1])
		return fmt.Sprintf(":nth-of-type(%d)%s", index+1, sub[2])
	})
	part = strings.TrimSuffix(part, "./")
	part = strings.TrimSuffix(part, ".")
	switch {
	case strings.HasPrefix(part, "class."):
		selector = "." + strings.ReplaceAll(strings.TrimSpace(part[len("class."):]), " ", ".")
	case strings.HasPrefix(part, "id."):
		selector = "#" + strings.TrimSpace(part[len("id."):])
	case strings.HasPrefix(part, "tag."):
		selector = strings.TrimSpace(part[len("tag."):])
	case strings.HasPrefix(part, "text."):
		escaped := strings.ReplaceAll(part[len("text."):], `\`, `\\`)
		escaped = strings.ReplaceAll(escaped, `"`, `\"`)
		selector = `*:contains("` + escaped + `")`
	default:
		selector = part
	}
	return selector, selection, exclude
}

// selectionIndices 展开 Legado 的下标表达式（逗号单点 / 起:止:步 区间，负数从尾数）。
func selectionIndices(expression string, length int) []int {
	if length <= 0 {
		return nil
	}
	normalized := func(value *int, fallback int) int {
		if value == nil {
			return fallback
		}
		result := *value
		if result < 0 {
			result += length
		}
		if result < 0 {
			result = 0
		}
		if result > length-1 {
			result = length - 1
		}
		return result
	}
	result := make([]int, 0, 4)
	appendUnique := func(index int) {
		for _, existing := range result {
			if existing == index {
				return
			}
		}
		result = append(result, index)
	}
	for _, item := range strings.Split(expression, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if !strings.Contains(item, ":") {
			raw, err := strconv.Atoi(item)
			if err != nil {
				continue
			}
			index := raw
			if raw < 0 {
				index = raw + length
			}
			if index >= 0 && index < length {
				appendUnique(index)
			}
			continue
		}
		pieces := strings.Split(item, ":")
		if len(pieces) != 2 && len(pieces) != 3 {
			continue
		}
		valid := true
		parsePiece := func(text string) *int {
			text = strings.TrimSpace(text)
			if text == "" {
				return nil
			}
			value, err := strconv.Atoi(text)
			if err != nil {
				valid = false
				return nil
			}
			return &value
		}
		startValue := parsePiece(pieces[0])
		endValue := parsePiece(pieces[1])
		var stepValue *int
		if len(pieces) == 3 {
			stepValue = parsePiece(pieces[2])
		}
		if !valid {
			continue
		}
		start := normalized(startValue, 0)
		end := normalized(endValue, length-1)
		step := 1
		if stepValue != nil {
			raw := *stepValue
			switch {
			case raw > 0:
				step = raw
			case -raw < length:
				step = raw + length
			default:
				step = 1
			}
		}
		if start == end || step >= length {
			appendUnique(start)
			continue
		}
		if end >= start {
			for index := start; index <= end; index += step {
				appendUnique(index)
			}
		} else {
			for index := start; index >= end; index -= step {
				appendUnique(index)
			}
		}
	}
	return result
}

// applySelection 按下标集取子集（exclude 时反向：丢掉命中下标）。
func applySelection(nodes []*Node, selection string, exclude bool) []*Node {
	if selection == "" {
		return nodes
	}
	indices := selectionIndices(selection, len(nodes))
	if exclude {
		blocked := make(map[int]bool, len(indices))
		for _, index := range indices {
			blocked[index] = true
		}
		out := make([]*Node, 0, len(nodes))
		for position, item := range nodes {
			if !blocked[position] {
				out = append(out, item)
			}
		}
		return out
	}
	out := make([]*Node, 0, len(indices))
	for _, position := range indices {
		if position >= 0 && position < len(nodes) {
			out = append(out, nodes[position])
		}
	}
	return out
}

// jsoupValues jsoup 选择器链求值（对齐 Python jsoup_values 的主体）。
func jsoupValues(root interface{}, rule string, listContext bool) ([]interface{}, error) {
	// `@attr:regex("pattern")` 尾缀：剥掉后求基础值，再按 Java 正则过滤
	// （Python：fullmatch 优先、回落 search；命中优先取组 1，否则整段命中）。
	// fullmatch 优先有可观察差：`.*?(\d+)` 对 `a1b2` fullmatch 取 "2"（贪到串尾），
	// 纯 search 会先命中 "a1" 取 "1"（oracle 实测钉住）。regexp2 无 fullmatch
	// API，用 `\A(?:…)\z` 锚定编译一次。
	if match := jsoupRegexSuffixRE.FindStringSubmatch(rule); match != nil {
		baseRule := strings.TrimSpace(rule[:len(rule)-len(match[0])]) + "@" + match[1]
		values, err := jsoupValues(root, baseRule, listContext)
		if err != nil {
			return nil, err
		}
		pattern := match[2]
		if pattern == "" {
			pattern = match[3]
		}
		compiled, err := compileJavaRegex(pattern)
		if err != nil {
			return nil, fmt.Errorf("无效的字段正则：%s", pattern)
		}
		anchored, err := compileJavaRegex(`\A(?:` + pattern + `)\z`)
		if err != nil {
			anchored = nil
		}
		extracted := make([]interface{}, 0, len(values))
		for _, value := range values {
			text := valueString(value)
			var match *regexp2.Match
			if anchored != nil {
				match, err = anchored.FindStringMatch(text)
				if err != nil {
					continue
				}
			}
			if match == nil {
				match, err = compiled.FindStringMatch(text)
				if err != nil || match == nil {
					continue
				}
			}
			groups := match.Groups()
			switch {
			case len(groups) > 1 && len(groups[1].Captures) > 0:
				extracted = append(extracted, groups[1].String())
			default:
				extracted = append(extracted, match.String())
			}
		}
		return extracted, nil
	}
	forceCSS := hasPrefixFold(rule, "@css:")
	parts := []string{}
	attr := ""
	if forceCSS {
		body := strings.TrimSpace(rule[len("@css:"):])
		if listContext {
			parts = []string{body}
		} else if cut := strings.LastIndex(body, "@"); cut >= 0 {
			parts = []string{body[:cut]}
			attr = strings.TrimSpace(body[cut+1:])
		} else {
			parts = []string{body}
		}
	} else {
		// Python split_rule_expression 会剥空段：`a@` 只有一段（`a` 成为尾段）
		rawParts := splitTopLevelString(rule, "@")
		parts = make([]string, 0, len(rawParts))
		for _, part := range rawParts {
			if trimmed := strings.TrimSpace(part); trimmed != "" {
				parts = append(parts, trimmed)
			}
		}
		if len(parts) > 0 && !listContext {
			attr = parts[len(parts)-1]
			parts = parts[:len(parts)-1]
		}
	}
	contexts := nodesFromRoot(root)
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		selector, selection, exclude := parseJsoupPart(part)
		found := make([]*Node, 0)
		for _, context := range contexts {
			if context == nil {
				continue
			}
			if selector == "children" {
				found = append(found, applySelection(context.Children(), selection, exclude)...)
				continue
			}
			if selector == "" {
				found = append(found, applySelection([]*Node{context}, selection, exclude)...)
				continue
			}
			matches, err := context.findRaw(selector)
			if err != nil {
				return nil, err
			}
			found = append(found, applySelection(matches, selection, exclude)...)
		}
		contexts = found
	}
	if attr == "" {
		out := make([]interface{}, 0, len(contexts))
		for _, node := range contexts {
			out = append(out, node)
		}
		return out, nil
	}
	return readAttrValues(contexts, attr), nil
}

// valueString 把规则求值出的一项转成文本（对齐 Python str()/value_string 常用分支）。
func valueString(value interface{}) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case *Node:
		return typed.OuterHtml()
	case float64:
		if typed == float64(int64(typed)) {
			return strconv.FormatInt(int64(typed), 10)
		}
		return strconv.FormatFloat(typed, 'f', -1, 64)
	case bool:
		if typed {
			return "true"
		}
		return "false"
	default:
		return fmt.Sprintf("%v", typed)
	}
}

// readAttrValues 按尾段读值（对齐 Python jsoup_values 的 getResultLast 分支）：
// text/ownText 取非空文本；html/all 序列化后以 "\n" 相拼为一个值；
// 其余当属性名逐个命中读、跳过缺失/空白。
func readAttrValues(nodes []*Node, attr string) []interface{} {
	lowered := strings.ToLower(attr)
	switch lowered {
	case "text":
		out := make([]interface{}, 0, len(nodes))
		for _, node := range nodes {
			if value := node.Text(); strings.TrimSpace(value) != "" {
				out = append(out, value)
			}
		}
		return out
	case "textnodes":
		out := make([]interface{}, 0, len(nodes))
		for _, node := range nodes {
			if value := node.TextNodes(); value != "" {
				out = append(out, value)
			}
		}
		return out
	case "owntext":
		out := make([]interface{}, 0, len(nodes))
		for _, node := range nodes {
			if value := node.OwnText(); strings.TrimSpace(value) != "" {
				out = append(out, value)
			}
		}
		return out
	case "html":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			if markup := node.CleanHTML(); markup != "" {
				rendered = append(rendered, markup)
			}
		}
		if len(rendered) == 0 {
			return nil
		}
		return []interface{}{strings.Join(rendered, "\n")}
	case "all":
		rendered := make([]string, 0, len(nodes))
		for _, node := range nodes {
			if markup := node.OuterHtml(); markup != "" {
				rendered = append(rendered, markup)
			}
		}
		if len(rendered) == 0 {
			return nil
		}
		return []interface{}{strings.Join(rendered, "\n")}
	default:
		name := attr
		if strings.HasPrefix(lowered, "attr.") {
			name = attr[len("attr."):]
		}
		if strings.HasPrefix(strings.ToLower(name), "abs:") {
			name = name[len("abs:"):]
		}
		out := make([]interface{}, 0, len(nodes))
		seen := make(map[string]bool, len(nodes))
		for _, node := range nodes {
			value := node.Attr(name)
			if strings.TrimSpace(value) == "" || seen[value] {
				continue
			}
			seen[value] = true
			out = append(out, value)
		}
		return out
	}
}

// nodesFromRoot 从求值根取选择上下文（文档 / 单节点 / 节点集合）。
func nodesFromRoot(root interface{}) []*Node {
	switch typed := root.(type) {
	case *Document:
		return typed.RootNodes()
	case *Node:
		return []*Node{typed}
	case []*Node:
		return typed
	case []interface{}:
		out := make([]*Node, 0, len(typed))
		for _, item := range typed {
			if node, ok := item.(*Node); ok {
				out = append(out, node)
			}
		}
		return out
	default:
		return nil
	}
}

// ---------------------------------------------------------------------------
// JSONPath
// ---------------------------------------------------------------------------

// jsonValueToList 把 json.Unmarshal 出来的值规整为 Python 形状的容器类型
// （map[string]interface{} / []interface{} 已一致，无需转换）。
func jsonRootForRule(root interface{}) interface{} {
	switch root.(type) {
	case map[string]interface{}, []interface{}:
		return root
	case string:
		var parsed interface{}
		if err := json.Unmarshal([]byte(root.(string)), &parsed); err == nil {
			return parsed
		}
	}
	return root
}

func isJSONPayload(root interface{}) bool {
	switch typed := root.(type) {
	case map[string]interface{}, []interface{}:
		return true
	case string:
		trimmed := strings.TrimLeft(typed, " \t\r\n")
		return strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")
	}
	return false
}

// jsonValues JSONPath 求值（对齐 Python json_values 的可观察语义）。
func jsonValues(root interface{}, rule string, listContext bool) ([]interface{}, error) {
	expression := strings.TrimSpace(rule)
	if hasPrefixFold(expression, "@json:") {
		expression = strings.TrimSpace(expression[len("@json:"):])
	}
	if text, ok := root.(string); ok {
		var parsed interface{}
		if err := json.Unmarshal([]byte(text), &parsed); err == nil {
			root = parsed
		}
	}
	expression = jsonBracketGlueRE.ReplaceAllString(expression, "].$1")
	if expression != "" && !strings.HasPrefix(expression, "$") && !strings.HasPrefix(expression, "@") {
		expression = "$." + expression
	}
	values, err := evalJSONPathExpr(root, expression)
	if err != nil {
		return nil, err
	}
	definite := jsonpathIsDefinite(expression)
	if listContext && len(values) > 0 && definite {
		if _, isList := values[0].([]interface{}); !isList {
			// 列表规则用 ArrayList 读定义路径——命中对象/标量算分支失败
			// （Python json_values 的 list_context 分支），让 || 链继续回落。
			return nil, nil
		}
	}
	if definite && len(values) == 1 {
		if items, isList := values[0].([]interface{}); isList {
			return items, nil
		}
	}
	return values, nil
}

// jsonpathIsDefinite 定义路径判据（对齐 Python _jsonpath_is_definite）：
// 含 `..`、`*`、`[?(...)]`、区间/联合下标的表达式一律视为非定义路径。
func jsonpathIsDefinite(expression string) bool {
	quote := byte(0)
	escaped := false
	square := 0
	bracketStart := -1
	for index := 0; index < len(expression); index++ {
		char := expression[index]
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
		case strings.HasPrefix(expression[index:], "..") || char == '*':
			return false
		case strings.HasPrefix(expression[index:], "[?("):
			return false
		case char == '[':
			square++
			if square == 1 {
				bracketStart = index + 1
			}
		case char == ']':
			if square == 1 && bracketStart >= 0 {
				body := expression[bracketStart:index]
				if strings.ContainsAny(body, ":,") {
					return false
				}
			}
			if square > 0 {
				square--
			}
		}
	}
	return true
}

type jsonPathStep struct {
	kind    string // name / deep / wild / index / slice / union
	name    string
	index   int
	start   *int
	end     *int
	indices []int
}

// parseJSONPathExpr 解析支持的 JSONPath 子集：名段、`..名` 深搜、`*` 通配、
// `[N]`、`[N:M]`、`[*]`、`[N,M]`、引号名。`[?(...)]` 过滤表达式整库实测为零，
// 遇到即显式报错（同 0 号原则），不猜。
func parseJSONPathExpr(expression string) ([]jsonPathStep, error) {
	text := strings.TrimSpace(expression)
	text = strings.TrimPrefix(text, "$")
	steps := make([]jsonPathStep, 0, 4)
	for len(text) > 0 {
		switch text[0] {
		case '.':
			deep := strings.HasPrefix(text, "..")
			text = strings.TrimPrefix(text, "..")
			if !deep {
				text = text[1:]
			}
			name, rest := readPathName(text)
			text = rest
			if name == "" || name == "*" {
				steps = append(steps, jsonPathStep{kind: "wild"})
				continue
			}
			if deep {
				steps = append(steps, jsonPathStep{kind: "deep", name: name})
			} else {
				steps = append(steps, jsonPathStep{kind: "name", name: name})
			}
		case '[':
			end := strings.IndexByte(text, ']')
			if end < 0 {
				return nil, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 括号未闭合：%s", expression)}
			}
			body := strings.TrimSpace(text[1:end])
			text = text[end+1:]
			step, err := parseBracketStep(body, expression)
			if err != nil {
				return nil, err
			}
			steps = append(steps, step)
		default:
			// 裸名（`a.b` 已被 `$.` 前缀化，这里只剩 `@` 开头等异常形状）
			name, rest := readPathName(text)
			if name == "" {
				return nil, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 形状暂未支持：%s", expression)}
			}
			steps = append(steps, jsonPathStep{kind: "name", name: name})
			text = rest
		}
	}
	return steps, nil
}

func readPathName(text string) (string, string) {
	end := len(text)
	for index := 0; index < len(text); index++ {
		if text[index] == '.' || text[index] == '[' {
			end = index
			break
		}
	}
	return text[:end], text[end:]
}

func parseBracketStep(body, expression string) (jsonPathStep, error) {
	switch {
	case body == "":
		return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 空下标：%s", expression)}
	case body == "*":
		return jsonPathStep{kind: "wild"}, nil
	case strings.HasPrefix(body, "?("):
		return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 过滤表达式暂未支持：%s", expression)}
	case strings.HasPrefix(body, "'") || strings.HasPrefix(body, `"`):
		name := strings.Trim(body, `'"`)
		return jsonPathStep{kind: "name", name: name}, nil
	case strings.Contains(body, ":"):
		pieces := strings.Split(body, ":")
		step := jsonPathStep{kind: "slice"}
		if len(pieces) > 3 {
			return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 步长区间暂未支持：%s", expression)}
		}
		if strings.TrimSpace(pieces[0]) != "" {
			value, err := strconv.Atoi(strings.TrimSpace(pieces[0]))
			if err != nil {
				return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 区间非法：%s", expression)}
			}
			step.start = &value
		}
		if len(pieces) > 1 && strings.TrimSpace(pieces[1]) != "" {
			value, err := strconv.Atoi(strings.TrimSpace(pieces[1]))
			if err != nil {
				return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 区间非法：%s", expression)}
			}
			step.end = &value
		}
		return step, nil
	case strings.Contains(body, ","):
		step := jsonPathStep{kind: "union"}
		for _, item := range strings.Split(body, ",") {
			value, err := strconv.Atoi(strings.TrimSpace(item))
			if err != nil {
				return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 联合下标非法：%s", expression)}
			}
			step.indices = append(step.indices, value)
		}
		return step, nil
	default:
		value, err := strconv.Atoi(strings.TrimSpace(body))
		if err != nil {
			return jsonPathStep{}, &UnsupportedError{Reason: fmt.Sprintf("JSONPath 下标非法：%s", expression)}
		}
		return jsonPathStep{kind: "index", index: value}, nil
	}
}

// evalJSONPathExpr 逐段求值（深搜按字典序保证确定性——Go map 无插入序）。
func evalJSONPathExpr(root interface{}, expression string) ([]interface{}, error) {
	steps, err := parseJSONPathExpr(expression)
	if err != nil {
		return nil, err
	}
	current := []interface{}{root}
	for _, step := range steps {
		next := make([]interface{}, 0, len(current))
		for _, item := range current {
			next = append(next, applyJSONPathStep(item, step)...)
		}
		current = next
	}
	return current, nil
}

func applyJSONPathStep(item interface{}, step jsonPathStep) []interface{} {
	switch step.kind {
	case "name":
		if object, ok := item.(map[string]interface{}); ok {
			if value, exists := object[step.name]; exists {
				return []interface{}{value}
			}
		}
	case "deep":
		out := make([]interface{}, 0)
		deepCollect(item, step.name, &out)
		return out
	case "wild":
		switch typed := item.(type) {
		case map[string]interface{}:
			keys := make([]string, 0, len(typed))
			for key := range typed {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			out := make([]interface{}, 0, len(keys))
			for _, key := range keys {
				out = append(out, typed[key])
			}
			return out
		case []interface{}:
			return typed
		}
	case "index":
		if list, ok := item.([]interface{}); ok {
			index := step.index
			if index < 0 {
				index += len(list)
			}
			if index >= 0 && index < len(list) {
				return []interface{}{list[index]}
			}
		}
	case "slice":
		if list, ok := item.([]interface{}); ok {
			length := len(list)
			start := 0
			end := length - 1
			if step.start != nil {
				start = *step.start
				if start < 0 {
					start += length
				}
			}
			if step.end != nil {
				end = *step.end
				if end < 0 {
					end += length
				}
			}
			if start < 0 {
				start = 0
			}
			if end > length-1 {
				end = length - 1
			}
			out := make([]interface{}, 0)
			for index := start; index <= end && index < length; index++ {
				if index >= 0 {
					out = append(out, list[index])
				}
			}
			return out
		}
	case "union":
		if list, ok := item.([]interface{}); ok {
			out := make([]interface{}, 0, len(step.indices))
			for _, index := range step.indices {
				if index < 0 {
					index += len(list)
				}
				if index >= 0 && index < len(list) {
					out = append(out, list[index])
				}
			}
			return out
		}
	}
	return nil
}

func deepCollect(item interface{}, name string, out *[]interface{}) {
	switch typed := item.(type) {
	case map[string]interface{}:
		if value, exists := typed[name]; exists {
			*out = append(*out, value)
		}
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			deepCollect(typed[key], name, out)
		}
	case []interface{}:
		for _, child := range typed {
			deepCollect(child, name, out)
		}
	}
}

// ---------------------------------------------------------------------------
// XPath（基础子集：选择节点取文本；@xpath: 前缀）
// ---------------------------------------------------------------------------

func xpathValues(root interface{}, rule string) ([]interface{}, error) {
	expression := strings.TrimSpace(rule)
	if hasPrefixFold(expression, "@xpath:") {
		expression = strings.TrimSpace(expression[len("@xpath:"):])
	}
	var doc *html.Node
	switch typed := root.(type) {
	case *Document:
		htmlText, _ := typed.doc.Find("html").First().Html()
		parsed, err := htmlquery.Parse(strings.NewReader(htmlText))
		if err != nil {
			return nil, err
		}
		doc = parsed
	case *Node:
		parsed, err := htmlquery.Parse(strings.NewReader(typed.OuterHtml()))
		if err != nil {
			return nil, err
		}
		doc = parsed
	default:
		return nil, nil
	}
	nodes := htmlquery.Find(doc, expression)
	out := make([]interface{}, 0, len(nodes))
	for _, node := range nodes {
		out = append(out, htmlquery.InnerText(node))
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// `##` 正则后缀（Java 正则语义，regexp2）
// ---------------------------------------------------------------------------

var javaRegexTimeout = time.Second

// ApplyRegexSuffix 应用 `selector##pattern##replacement` 后缀（对齐 Python
// _apply_regex_suffix，2026-10-16 逐条实测 oracle）：4 段形式是 replace-first
// （全文 search 一次，再在**命中文本本身**上 sub count=1——Python 就是这么写的，
// `aaXaa##aa##b##` → `b`；无命中回空串；非法正则回 replacement），3/2 段形式是
// 全局替换；正则编译失败退化为字面替换。
func ApplyRegexSuffix(value string, pieces []string) string {
	if len(pieces) < 2 || pieces[1] == "" {
		return value
	}
	pattern := pieces[1]
	replacement := ""
	if len(pieces) > 2 {
		replacement = pieces[2]
	}
	compiled, err := compileJavaRegex(pattern)
	if err != nil {
		if len(pieces) > 3 {
			return replacement
		}
		return strings.ReplaceAll(value, pattern, replacement)
	}
	if len(pieces) > 3 {
		match, err := compiled.FindStringMatch(value)
		if err != nil || match == nil {
			return ""
		}
		rendered, err := compiled.Replace(match.String(), replacement, 0, 1)
		if err != nil {
			return ""
		}
		return rendered
	}
	rendered, err := compiled.Replace(value, replacement, 0, -1)
	if err != nil {
		return value
	}
	return rendered
}

func compileJavaRegex(pattern string) (*regexp2.Regexp, error) {
	compiled, err := regexp2.Compile(pattern, 0)
	if err != nil {
		return nil, err
	}
	compiled.MatchTimeout = javaRegexTimeout
	return compiled, nil
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

func hasPrefixFold(value, prefix string) bool {
	return len(value) >= len(prefix) && strings.EqualFold(value[:len(prefix)], prefix)
}

// splitTopLevelString 在方括号/圆括号/引号之外按 sep 切分（对齐 Python
// split_rule_expression 的 code=False 口径：引号外反斜杠跳过下一字符，引号内
// 只认闭合引号；不过滤空段，由调用方决定）。
func splitTopLevelString(value, sep string) []string {
	parts := []string{}
	depth := 0
	quote := byte(0)
	start := 0
	for index := 0; index < len(value); index++ {
		char := value[index]
		if char == '\\' && quote == 0 {
			index++
			continue
		}
		if quote != 0 {
			if char == quote {
				quote = 0
			}
			continue
		}
		switch char {
		case '\'', '"':
			quote = char
		case '[', '(':
			depth++
		case ']', ')':
			if depth > 0 {
				depth--
			}
		default:
			if depth == 0 && strings.HasPrefix(value[index:], sep) {
				parts = append(parts, value[start:index])
				index += len(sep) - 1
				start = index + 1
			}
		}
	}
	parts = append(parts, value[start:])
	return parts
}

// splitBalancedFields 对齐 Python split_balanced（字段层 `||` 候选切分）：
// 方/圆/花括号与引号内不切；引号内反斜杠按"前一字符"判转义；每段去空白、
// 空段过滤。
func splitBalancedFields(value, separator string) []string {
	parts := []string{}
	start := 0
	quote := byte(0)
	square, round, curly := 0, 0, 0
	for index := 0; index < len(value); index++ {
		char := value[index]
		switch {
		case quote != 0:
			if char == quote && (index == 0 || value[index-1] != '\\') {
				quote = 0
			}
		case char == '\'' || char == '"':
			quote = char
		case char == '[':
			square++
		case char == ']':
			if square > 0 {
				square--
			}
		case char == '(':
			round++
		case char == ')':
			if round > 0 {
				round--
			}
		case char == '{':
			curly++
		case char == '}':
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

// ---------------------------------------------------------------------------
// 字段规则管线（对齐 Python extract_value / _extract_single_value）
// ---------------------------------------------------------------------------

// FieldErrorHook 字段级规则错误的记录口（引擎层注入日志；nil 时静默回落）。
// 0 号原则：列表级显式报错，字段级记日志回落空值。
var FieldErrorHook func(rule string, err error)

// fieldValue 字段规则统一取值口：`||` 候选惰性（首组有值即返回），每个候选按
// _extract_single_value 口径取值（`##` 正则后缀、裸属性名快捷、Evaluate 管线）。
func fieldValue(root interface{}, rule string, firstOnly bool) string {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return ""
	}
	candidates := splitBalancedFields(rule, "||")
	if len(candidates) == 0 {
		candidates = []string{rule}
	}
	for _, candidate := range candidates {
		if value := singleFieldValue(root, candidate, firstOnly); value != "" {
			return value
		}
	}
	return ""
}

// singleFieldValue 对齐 Python _extract_single_value：`##` 先切选择器段、
// 裸属性名快捷、Evaluate 取值、`##` 正则后缀收尾。
func singleFieldValue(root interface{}, rule string, firstOnly bool) string {
	selectorRule := rule
	if idx := strings.Index(rule, "##"); idx >= 0 {
		selectorRule = strings.TrimSpace(rule[:idx])
	}
	value := ""
	// 裸属性名快捷（旧书源兼容）：单段纯名在当前节点上直读属性
	// （Python：kind=html 且整段匹配 `[A-Za-z_:][-A-Za-z0-9_:.]*` 时 node.get）。
	if node, ok := root.(*Node); ok && plainNameRE.MatchString(selectorRule) {
		value = strings.TrimSpace(node.Attr(selectorRule))
	}
	if value == "" && selectorRule != "" {
		useLegacy := false
		if segments := splitTopLevel(selectorRule, '@'); len(segments) == 1 &&
			firstTopLevelOperator(selectorRule, false) == "" &&
			!hasFieldPathPrefix(selectorRule) {
			useLegacy = true
		}
		if useLegacy {
			value = legacyFieldValue(root, selectorRule)
		} else {
			values, err := Evaluate(root, selectorRule, "html", false)
			if err != nil {
				if FieldErrorHook != nil {
					FieldErrorHook(selectorRule, err)
				}
			} else {
				value = renderFieldValues(values, firstOnly)
			}
		}
	}
	if strings.Contains(rule, "##") {
		pieces := strings.Split(rule, "##")
		if len(pieces) >= 2 && pieces[1] != "" {
			value = ApplyRegexSuffix(value, pieces)
		}
	}
	return strings.TrimSpace(value)
}

// renderFieldValues 值列表渲染：每项取文本；firstOnly 只取首个，否则 "\n" 相连
// （Python rendered[0] / "\n".join(rendered) 的分支）。
func renderFieldValues(values []interface{}, firstOnly bool) string {
	if len(values) == 0 {
		return ""
	}
	rendered := make([]string, 0, len(values))
	for _, value := range values {
		if node, ok := value.(*Node); ok {
			rendered = append(rendered, node.Text())
			continue
		}
		rendered = append(rendered, valueString(value))
	}
	if firstOnly {
		return strings.TrimSpace(rendered[0])
	}
	return strings.TrimSpace(strings.Join(rendered, "\n"))
}

// hasFieldPathPrefix 字段候选是否以 JSONPath/XPath/CSS 前缀开头（走 Evaluate 管线）。
func hasFieldPathPrefix(rule string) bool {
	return strings.HasPrefix(rule, "$.") || strings.HasPrefix(rule, "$[") ||
		strings.HasPrefix(rule, "/") || strings.HasPrefix(rule, "./") ||
		hasPrefixFold(rule, "@json:") || hasPrefixFold(rule, "@xpath:") ||
		hasPrefixFold(rule, "@css:")
}

// splitRuleOperators 找外层第一个联合运算符并按它切分（对齐 Python
// split_rule_expression 的可观察分组：分组只取决于规则文本）。
func splitRuleOperators(rule string, code bool) ([]string, string) {
	operator := firstTopLevelOperator(rule, code)
	if operator == "" {
		return []string{rule}, ""
	}
	parts := topLevelSplitBy(rule, operator, code)
	trimmed := make([]string, 0, len(parts))
	for _, part := range parts {
		if part = strings.TrimSpace(part); part != "" {
			trimmed = append(trimmed, part)
		}
	}
	return trimmed, operator
}

func firstTopLevelOperator(value string, code bool) string {
	operators := []string{"&&", "||", "%%"}
	depth := 0
	quote := byte(0)
	for index := 0; index < len(value); index++ {
		char := value[index]
		// Python positions()：反斜杠在引号外一律转义下一字符；引号内仅 code
		// （JSON 口径）时转义。
		if char == '\\' && (code || quote == 0) {
			index++
			continue
		}
		if quote != 0 {
			if char == quote {
				quote = 0
			}
			continue
		}
		switch char {
		case '\'', '"':
			quote = char
			continue
		case '[', '(':
			depth++
			continue
		case ']', ')':
			if depth > 0 {
				depth--
			}
			continue
		}
		if depth != 0 {
			continue
		}
		for _, operator := range operators {
			if strings.HasPrefix(value[index:], operator) {
				return operator
			}
		}
	}
	return ""
}

func topLevelSplitBy(value, separator string, code bool) []string {
	parts := []string{}
	depth := 0
	quote := byte(0)
	start := 0
	for index := 0; index < len(value); index++ {
		char := value[index]
		if char == '\\' && (code || quote == 0) {
			index++
			continue
		}
		if quote != 0 {
			if char == quote {
				quote = 0
			}
			continue
		}
		switch char {
		case '\'', '"':
			quote = char
			continue
		case '[', '(':
			depth++
			continue
		case ']', ')':
			if depth > 0 {
				depth--
			}
			continue
		}
		if depth == 0 && strings.HasPrefix(value[index:], separator) {
			parts = append(parts, value[start:index])
			index += len(separator) - 1
			start = index + 1
		}
	}
	parts = append(parts, value[start:])
	return parts
}
