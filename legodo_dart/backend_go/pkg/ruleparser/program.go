package ruleparser

import (
	"regexp"
	"sort"
	"strings"
)

// 本文件是 Python `app/services/source_rule.py`（compile_rule_program）的逐行移植：
// 把一条 Legado 规则文本编译为「plain 段 + js/webjs 段」的连续切片序列，
// 供字段层（selector_and_suffix）与列表管线（leading_js）按段消费。
// 语义细节（段区间、@put 剥离、模式判定顺序）都以该文件为准。

// RuleStage 编译后的一段规则。Mode：default/json/xpath/js/webjs/regex；
// Raw 是原文连续切片（js 段含 `<js>`/`@js:` 标记）；PutRule 是该段前原文里
// 剥出的 `@put:{...}` 正文（含花括号，无则为空）。
type RuleStage struct {
	Mode    string
	Rule    string
	Raw     string
	PutRule string
}

// RuleProgram 一条规则的编译产物。
type RuleProgram struct {
	Original string
	Stages   []RuleStage
}

var (
	// <js>…</js> 或 @js:…（到串尾）；alternation 顺序与 Python 一致。
	jsProgramRE = regexp.MustCompile(`(?is)<js>([\s\S]*?)</js>|@js:([\s\S]*)`)
	// Legado 3.x 只有 @webjs: 前缀，脚本体至少 5 字符。
	webjsProgramRE   = regexp.MustCompile(`(?i)@webjs:([\s\S]{5,})`)
	putRuleProgramRE = regexp.MustCompile(`(?i)@put:(\{[^}]+?\})`)
	evalPatternRE    = regexp.MustCompile(`(?i)@get:\{[^}]+?\}|\{\{[\s\S]*?\}\}`)
	captureProgramRE = regexp.MustCompile(`\$\d{1,2}`)
)

// SplitPutRules 剥出 `@put:{...}`：返回（去段后的规则, `{body1,body2}` 合并串）。
// 对齐 Python source_rule.strip_put_rules：多处 @put 的正文以逗号合并、重包花括号。
func SplitPutRules(value string) (string, string) {
	bodies := make([]string, 0, 2)
	cleaned := putRuleProgramRE.ReplaceAllStringFunc(value, func(match string) string {
		body := putRuleProgramRE.FindStringSubmatch(match)[1]
		bodies = append(bodies, body[1:len(body)-1])
		return ""
	})
	combined := ""
	if len(bodies) > 0 {
		combined = "{" + strings.Join(bodies, ",") + "}"
	}
	return strings.TrimSpace(cleaned), combined
}

// preparePlainRule 判定 plain 段的 Mode（对齐 Python _prepare_plain，
// all_in_one/is_regex 两条分支在本引擎的调用点恒为 false，不移植）。
func preparePlainRule(rule string, isJSON bool) (string, string) {
	value := strings.TrimSpace(rule)
	lowered := strings.ToLower(value)
	mode := ""
	switch {
	case strings.HasPrefix(lowered, "@xpath:"):
		mode, value = "xpath", value[7:]
	case strings.HasPrefix(lowered, "@json:"):
		mode, value = "json", value[6:]
	case strings.HasPrefix(value, "@@"):
		mode, value = "default", value[2:]
	case isJSON || strings.HasPrefix(value, "$.") || strings.HasPrefix(value, "$["):
		mode = "json"
	case strings.HasPrefix(value, "/"):
		mode = "xpath"
	default:
		mode = "default"
	}
	// SourceRule 把插值/捕获程序提升为 Regex 模式，除非首个插值跟在已分隔的
	// `##` 选择器之后。
	if loc := evalPatternRE.FindStringIndex(value); loc != nil && mode != "js" && mode != "regex" {
		prefix := value[:loc[0]]
		if loc[0] == 0 || !strings.Contains(prefix, "##") {
			mode = "regex"
		}
	}
	beforeSuffix := value
	if idx := strings.Index(value, "##"); idx >= 0 {
		beforeSuffix = value[:idx]
	}
	if captureProgramRE.MatchString(beforeSuffix) && mode != "js" && mode != "regex" {
		mode = "regex"
	}
	return mode, value
}

// CompileRuleProgram 编译规则文本（对齐 Python compile_rule_program）。
func CompileRuleProgram(value string, isJSON bool) *RuleProgram {
	original := value
	type programMatch struct {
		start, end int
		mode       string
		script     string
	}
	matches := make([]programMatch, 0, 4)
	for _, m := range jsProgramRE.FindAllStringSubmatchIndex(original, -1) {
		script := ""
		switch {
		case m[4] >= 0:
			script = original[m[4]:m[5]]
		case m[2] >= 0:
			script = original[m[2]:m[3]]
		}
		matches = append(matches, programMatch{m[0], m[1], "js", script})
	}
	for _, m := range webjsProgramRE.FindAllStringSubmatchIndex(original, -1) {
		matches = append(matches, programMatch{m[0], m[1], "webjs", original[m[2]:m[3]]})
	}
	sort.Slice(matches, func(i, j int) bool {
		if matches[i].start != matches[j].start {
			return matches[i].start < matches[j].start
		}
		if matches[i].end != matches[j].end {
			return matches[i].end < matches[j].end
		}
		return matches[i].mode < matches[j].mode
	})

	stages := make([]RuleStage, 0, 4)
	appendPlain := func(raw string) {
		rule, putRule := SplitPutRules(raw)
		if rule == "" && putRule == "" {
			return
		}
		mode, prepared := preparePlainRule(rule, isJSON)
		stages = append(stages, RuleStage{Mode: mode, Rule: prepared, Raw: raw, PutRule: putRule})
	}
	cursor := 0
	for _, m := range matches {
		if m.start < cursor {
			continue
		}
		if m.start > cursor {
			appendPlain(original[cursor:m.start])
		}
		stages = append(stages, RuleStage{Mode: m.mode, Rule: m.script, Raw: original[m.start:m.end]})
		cursor = m.end
	}
	if cursor < len(original) {
		appendPlain(original[cursor:])
	}
	return &RuleProgram{Original: original, Stages: stages}
}

func joinStagesRaw(stages []RuleStage) string {
	var b strings.Builder
	for _, stage := range stages {
		b.WriteString(stage.Raw)
	}
	return b.String()
}

// LeadingJS 列表管线的首段拆分（对齐 Python leading_js）：首段是 js/webjs 时
// 返回（该段原文, 其余段原文拼接去空白），否则（"", 原文去空白）。
func (p *RuleProgram) LeadingJS() (string, string) {
	if len(p.Stages) > 0 && (p.Stages[0].Mode == "js" || p.Stages[0].Mode == "webjs") {
		return p.Stages[0].Raw, strings.TrimSpace(joinStagesRaw(p.Stages[1:]))
	}
	return "", strings.TrimSpace(p.Original)
}

// SelectorAndSuffix 字段层拆分（对齐 Python selector_and_suffix）。
func (p *RuleProgram) SelectorAndSuffix() (string, string) {
	if len(p.Stages) == 0 {
		return p.Original, ""
	}
	if p.Stages[0].Mode == "js" || p.Stages[0].Mode == "webjs" {
		return "", strings.TrimSpace(p.Original)
	}
	if len(p.Stages) > 1 && (p.Stages[1].Mode == "js" || p.Stages[1].Mode == "webjs") {
		return strings.TrimSpace(p.Stages[0].Rule), strings.TrimSpace(joinStagesRaw(p.Stages[1:]))
	}
	return p.Original, ""
}
