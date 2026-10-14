package services

import (
	"fmt"
	"regexp"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// ReplaceRule represents a text replacement rule.
type ReplaceRule struct {
	ID           int64  `json:"id"`
	Name         string `json:"name"`
	Pattern      string `json:"pattern"`
	Replacement  string `json:"replacement"`
	Enabled      bool   `json:"enabled"`
	Global       bool   `json:"global"`
}

// ReplaceService manages regex-based text replacement rules.
type ReplaceService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
	mu      sync.RWMutex
	rules   []ReplaceRule
}

// NewReplaceService creates a new replace service.
func NewReplaceService(storage *core.StorageService, bus *core.EventBus) *ReplaceService {
	return &ReplaceService{
		base:    core.NewBaseService("ReplaceService", "StorageService"),
		storage: storage,
		bus:     bus,
		rules:   make([]ReplaceRule, 0),
	}
}

// Start loads all rules from DB.
func (s *ReplaceService) Start() error {
	s.base.SetOnStart(func() error { return s.loadAllRules() })
	return s.base.Start()
}
func (s *ReplaceService) Stop() error { return s.base.Stop() }

func (s *ReplaceService) loadAllRules() error {
	rows, err := s.storage.Query("SELECT id, name, pattern, replacement, enabled, global_enabled FROM replace_rules")
	if err != nil || rows == nil {
		return err
	}
	defer rows.Close()

	for rows.Next() {
		var r ReplaceRule
		var geo int
		if scanErr := rows.Scan(&r.ID, &r.Name, &r.Pattern, &r.Replacement, &r.Enabled, &geo); scanErr != nil {
			continue
		}
		r.Global = geo == 1
		s.rules = append(s.rules, r)
	}
	return rows.Err()
}

// List returns all replace rules.
func (s *ReplaceService) List() []ReplaceRule {
	cp := make([]ReplaceRule, len(s.rules))
	copy(cp, s.rules)
	return cp
}

// AddRule adds a new replacement rule.
func (s *ReplaceService) AddRule(name, pattern, replacement string) (int64, error) {
	result, err := s.storage.Execute(
		`INSERT INTO replace_rules (name, pattern, replacement, enabled, global_enabled) VALUES (?, ?, ?, 1, 0)`,
		name, pattern, replacement,
	)
	if err != nil {
		return 0, err
	}
	id, _ := result.LastInsertId()
	rule := ReplaceRule{ID: id, Name: name, Pattern: pattern, Replacement: replacement, Enabled: true}
	s.rules = append(s.rules, rule)
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{int(id)}})
	return id, nil
}

// DeleteRule removes a rule by ID.
func (s *ReplaceService) DeleteRule(id int64) error {
	_, err := s.storage.Execute("DELETE FROM replace_rules WHERE id = ?", id)
	if err != nil {
		return err
	}
	for i, r := range s.rules {
		if r.ID == id {
			s.rules = append(s.rules[:i], s.rules[i+1:]...)
			break
		}
	}
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{}})
	return nil
}

// ToggleGlobal enables/disables global apply for a rule.
func (s *ReplaceService) ToggleGlobal(id int64, global bool) error {
	val := 0
	if global {
		val = 1
	}
	_, err := s.storage.Execute("UPDATE replace_rules SET global_enabled = ? WHERE id = ?", val, id)
	if err != nil {
		return err
	}
	for i, r := range s.rules {
		if r.ID == id {
			s.rules[i].Global = global
			break
		}
	}
	s.bus.Publish(&core.ReplaceRulesChangedEvent{IDs: []int{}})
	return nil
}

// UpdateRule modifies an existing rule.
func (s *ReplaceService) UpdateRule(id int64, name, pattern, replacement string) error {
	_, err := s.storage.Execute("UPDATE replace_rules SET name=?, pattern=?, replacement=? WHERE id=?", name, pattern, replacement, id)
	if err != nil {
		return err
	}
	for i, r := range s.rules {
		if r.ID == id {
			s.rules[i] = ReplaceRule{ID: id, Name: name, Pattern: pattern, Replacement: replacement, Enabled: true}
			break
		}
	}
	return nil
}

// ToggleEnabled toggles a single rule on/off.
func (s *ReplaceService) ToggleEnabled(id int64, enabled bool) error {
	val := 0
	if enabled {
		val = 1
	}
	_, err := s.storage.Execute("UPDATE replace_rules SET enabled=? WHERE id=?", val, id)
	if err != nil {
		return err
	}
	for i, r := range s.rules {
		if r.ID == id {
			s.rules[i].Enabled = enabled
			break
		}
	}
	return nil
}

// ApplyRules applies all active replace rules to text.
func (s *ReplaceService) ApplyRules(text string) (string, error) {
	s.mu.RLock()
	rules := make([]ReplaceRule, len(s.rules))
	copy(rules, s.rules)
	s.mu.RUnlock()

	for _, rule := range rules {
		if !rule.Enabled {
			continue
		}
		replaced, err := applyRegex(text, rule.Pattern, rule.Replacement, rule.Global)
		if err != nil {
			return "", fmt.Errorf("apply rule %d (%s): %w", rule.ID, rule.Pattern, err)
		}
		text = replaced
	}
	return text, nil
}

// applyRegex applies a regex replacement to text.
func applyRegex(text, pattern, replacement string, global bool) (string, error) {
	re, err := regexp.Compile(pattern)
	if err != nil {
		return "", fmt.Errorf("compile regex: %w", err)
	}
	if global {
		return re.ReplaceAllString(text, replacement), nil
	}
	return re.ReplaceAllString(text, replacement), nil
}
