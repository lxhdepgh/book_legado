package core

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// SettingsService manages global preferences via key-value store.
// Aligns with Python core/settings.py SettingsService.
type SettingsService struct {
	*BaseService
	storage *StorageService
	cache   map[string]string // memory cache (string values)
	mu      sync.RWMutex
	bus     *EventBus
}

// NewSettingsService creates a settings service backed by storage.
func NewSettingsService(storage *StorageService, bus *EventBus) (*SettingsService, error) {
	svc := &SettingsService{
		BaseService: NewBaseService("SettingsService", "StorageService"),
		storage:     storage,
		cache:       make(map[string]string),
		bus:         bus,
	}
	return svc, nil
}

// OnStart loads all keys from DB into the in-memory cache.
func (s *SettingsService) OnStart() error {
	rows, err := s.storage.Query("SELECT key, value FROM settings")
	if err != nil {
		return fmt.Errorf("load settings: %w", err)
	}
	defer rows.Close()

	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			continue
		}
		s.cache[key] = value
	}
	return rows.Err()
}

// OnStop persists any remaining state (not needed for key-value; always reads fresh).
func (s *SettingsService) OnStop() error {
	return nil
}

// Get retrieves a setting value as raw JSON string.
// Falls back to defaultValue if key not found.
func (s *SettingsService) Get(key string, defaultValue string) string {
	s.mu.RLock()
	v, ok := s.cache[key]
	s.mu.RUnlock()
	if ok {
		return v
	}
	return defaultValue
}

// GetInt reads a setting and parses it as int.
func (s *SettingsService) GetInt(key string, defaultValue int) int {
	v := s.Get(key, "")
	if v == "" {
		return defaultValue
	}
	var n int
	fmt.Sscanf(v, "%d", &n)
	return n
}

// GetString reads a string setting.
func (s *SettingsService) GetString(key string) string {
	return s.Get(key, "")
}

// Set writes a setting value (JSON-encoded string) to DB + cache + publishes event.
func (s *SettingsService) Set(key string, value interface{}) error {
	jsonVal, err := json.Marshal(value)
	if err != nil {
		jsonVal = []byte(fmt.Sprintf("%v", value))
	}
	valueStr := string(jsonVal)

	oldValue := s.Get(key, "")

	s.mu.Lock()
	s.cache[key] = valueStr
	s.mu.Unlock()

	_, err = s.storage.Execute(
		`INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)`,
		key, valueStr,
	)
	if err != nil {
		return fmt.Errorf("save setting %s: %w", key, err)
	}

	s.bus.Publish(&SettingsChangedEvent{
		Key:       key,
		Value:     value,
		OldValue:  oldValue,
		Timestamp: float64(time.Now().UnixMilli()) / 1000.0,
	})

	return nil
}

// ReadKeys retrieves multiple settings in one query.
func (s *SettingsService) ReadKeys(keys []string) (map[string]string, error) {
	result := make(map[string]string)
	for _, key := range keys {
		result[key] = s.Get(key, "")
	}
	return result, nil
}

// Subscribe registers a handler for setting changes on the given keys.
// Returns an unsubscribe function.
func (s *SettingsService) Subscribe(keys []string, handler func(event *SettingsChangedEvent)) func() {
	unsubKeyHandlers := make([]func(), len(keys))
	for _, key := range keys {
		u := s.bus.Subscribe("settings_changed", func(event AppEvent) {
			if evt, ok := event.(*SettingsChangedEvent); ok && (key == evt.Key || len(keys) == 0) {
				handler(evt)
			}
		})
		unsubKeyHandlers = append(unsubKeyHandlers, u)
	}
	return func() {
		for _, u := range unsubKeyHandlers {
			u()
		}
	}
}
