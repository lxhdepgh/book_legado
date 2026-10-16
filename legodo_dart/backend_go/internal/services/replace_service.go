package services

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// replaceGlobalEnabledKey 是全局替换开关的 settings 键（对齐 Python
// app/services/tools/replace_rule_service.py 的 GLOBAL_ENABLED_KEY）。
// 真库 replace_rules 表没有 global_enabled 列——全局开关只在 settings 里。
const replaceGlobalEnabledKey = "replace.global.enabled"

// ReplaceRule represents a text replacement rule.
// 列形状对齐 Python app/storage/schema.py v5：
// id/name/pattern/replacement/scope/is_regex/enabled/created_at/updated_at。
// scope: content（正文）| title（标题）；is_regex=false 时按字面量替换。
type ReplaceRule struct {
	ID          int64  `json:"id"`
	Name        string `json:"name"`
	Pattern     string `json:"pattern"`
	Replacement string `json:"replacement"`
	Scope       string `json:"scope"`
	IsRegex     bool   `json:"isRegex"`
	Enabled     bool   `json:"enabled"`
}

// ReplaceService manages text replacement rules.
type ReplaceService struct {
	base     *core.BaseService
	storage  *core.StorageService
	settings *core.SettingsService
	bus      *core.EventBus
	mu       sync.RWMutex
	rules    []ReplaceRule
}

// NewReplaceService creates a new replace service.
func NewReplaceService(storage *core.StorageService, settings *core.SettingsService, bus *core.EventBus) *ReplaceService {
	return &ReplaceService{
		base:     core.NewBaseService("ReplaceService", "StorageService"),
		storage:  storage,
		settings: settings,
		bus:      bus,
		rules:    make([]ReplaceRule, 0),
	}
}

// Start loads all rules from DB.
func (s *ReplaceService) Start() error {
	s.base.SetOnStart(func() error { return s.loadAllRules() })
	return s.base.Start()
}
func (s *ReplaceService) Stop() error { return s.base.Stop() }

func (s *ReplaceService) loadAllRules() error {
	rows, err := s.storage.Query(
		`SELECT id, name, pattern, replacement, scope, is_regex, enabled
		 FROM replace_rules ORDER BY scope ASC, id ASC`)
	if err != nil || rows == nil {
		return err
	}
	defer rows.Close()

	rules := make([]ReplaceRule, 0)
	for rows.Next() {
		var r ReplaceRule
		var isRegex, enabled int
		if scanErr := rows.Scan(&r.ID, &r.Name, &r.Pattern, &r.Replacement,
			&r.Scope, &isRegex, &enabled); scanErr != nil {
			continue
		}
		r.IsRegex = isRegex == 1
		r.Enabled = enabled == 1
		rules = append(rules, r)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	s.rules = rules
	s.mu.Unlock()
	return nil
}

// List returns all replace rules.
func (s *ReplaceService) List() []ReplaceRule {
	s.mu.RLock()
	defer s.mu.RUnlock()
	cp := make([]ReplaceRule, len(s.rules))
	copy(cp, s.rules)
	return cp
}

// AddRule adds a new replacement rule (scope=content, isRegex=true 对齐 Python 默认).
func (s *ReplaceService) AddRule(name, pattern, replacement string, enabled bool) (int64, error) {
	en := 0
	if enabled {
		en = 1
	}
	now := core.NowSeconds()
	result, err := s.storage.Execute(
		`INSERT INTO replace_rules (name, pattern, replacement, scope, is_regex, enabled, created_at, updated_at)
		 VALUES (?, ?, ?, 'content', 1, ?, ?, ?)`,
		name, pattern, replacement, en, now, now,
	)
	if err != nil {
		return 0, err
	}
	id, _ := result.LastInsertId()
	s.mu.Lock()
	s.rules = append(s.rules, ReplaceRule{
		ID: id, Name: name, Pattern: pattern, Replacement: replacement,
		Scope: "content", IsRegex: true, Enabled: enabled,
	})
	s.mu.Unlock()
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{int(id)}})
	return id, nil
}

// DeleteRule removes a rule by ID.
func (s *ReplaceService) DeleteRule(id int64) error {
	_, err := s.storage.Execute("DELETE FROM replace_rules WHERE id = ?", id)
	if err != nil {
		return err
	}
	s.mu.Lock()
	for i, r := range s.rules {
		if r.ID == id {
			s.rules = append(s.rules[:i], s.rules[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{}})
	return nil
}

// UpdateRule modifies an existing rule.
func (s *ReplaceService) UpdateRule(id int64, name, pattern, replacement string, enabled bool) error {
	en := 0
	if enabled {
		en = 1
	}
	_, err := s.storage.Execute(
		`UPDATE replace_rules SET name=?, pattern=?, replacement=?, enabled=?, updated_at=? WHERE id=?`,
		name, pattern, replacement, en, core.NowSeconds(), id)
	if err != nil {
		return err
	}
	s.mu.Lock()
	for i, r := range s.rules {
		if r.ID == id {
			s.rules[i].Name = name
			s.rules[i].Pattern = pattern
			s.rules[i].Replacement = replacement
			s.rules[i].Enabled = enabled
			break
		}
	}
	s.mu.Unlock()
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{int(id)}})
	return nil
}

// ToggleEnabled toggles a single rule on/off.
func (s *ReplaceService) ToggleEnabled(id int64, enabled bool) error {
	val := 0
	if enabled {
		val = 1
	}
	_, err := s.storage.Execute(
		"UPDATE replace_rules SET enabled=?, updated_at=? WHERE id=?",
		val, core.NowSeconds(), id)
	if err != nil {
		return err
	}
	s.mu.Lock()
	for i, r := range s.rules {
		if r.ID == id {
			s.rules[i].Enabled = enabled
			break
		}
	}
	s.mu.Unlock()
	return nil
}

// SetGlobalEnabled 写全局替换开关（settings 键，非表列）。
func (s *ReplaceService) SetGlobalEnabled(enabled bool) error {
	if err := s.settings.Set(replaceGlobalEnabledKey, enabled); err != nil {
		return err
	}
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{}})
	return nil
}

// GlobalEnabled 读全局替换开关（缺省 true，对齐 Python 默认开启）。
func (s *ReplaceService) GlobalEnabled() bool {
	v := s.settings.Get(replaceGlobalEnabledKey, "true")
	b, err := strconv.ParseBool(strings.TrimSpace(v))
	if err != nil {
		return true
	}
	return b
}

// ApplyRules applies all active replace rules to content text.
func (s *ReplaceService) ApplyRules(text string) (string, error) {
	if !s.GlobalEnabled() {
		return text, nil
	}
	s.mu.RLock()
	rules := make([]ReplaceRule, len(s.rules))
	copy(rules, s.rules)
	s.mu.RUnlock()

	for _, rule := range rules {
		if !rule.Enabled || rule.Scope != "content" {
			continue
		}
		if !rule.IsRegex {
			text = strings.ReplaceAll(text, rule.Pattern, rule.Replacement)
			continue
		}
		replaced, err := applyRegex(text, rule.Pattern, rule.Replacement)
		if err != nil {
			// 单条坏规则不拖垮整段正文替换（Python 侧同样逐条 try 吞掉）。
			continue
		}
		text = replaced
	}
	return text, nil
}

// applyRegex applies a full regex replacement to text.
func applyRegex(text, pattern, replacement string) (string, error) {
	re, err := regexp.Compile(pattern)
	if err != nil {
		return "", fmt.Errorf("compile regex: %w", err)
	}
	return re.ReplaceAllString(text, replacement), nil
}
