package services

import (
	"encoding/json"
	"fmt"
	"log"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// SourceService manages book source CRUD operations.
type SourceService struct {
	base     *core.BaseService
	storage  *core.StorageService
	settings *core.SettingsService
	engine   *engine.RuleEngine
	bus      *core.EventBus
	cache    map[string]*models.BookSource // url -> source
}

// NewSourceService creates a new SourceService.
func NewSourceService(storage *core.StorageService, settings *core.SettingsService, ruleEngine *engine.RuleEngine, bus *core.EventBus) *SourceService {
	return &SourceService{
		base:    core.NewBaseService("SourceService", "StorageService", "RuleEngineService"),
		storage: storage,
		settings: settings,
		engine:  ruleEngine,
		bus:     bus,
		cache:   make(map[string]*models.BookSource),
	}
}

// Start loads all sources from DB into cache.
func (s *SourceService) Start() error {
	s.base.SetOnStart(func() error {
		return s.loadAllSources()
	})
	s.base.SetOnStop(func() error {
		return nil
	})
	return s.base.Start()
}

// Stop releases resources.
func (s *SourceService) Stop() error {
	return s.base.Stop()
}

// loadAllSources reads all sources from DB into the in-memory cache.
func (s *SourceService) loadAllSources() error {
	rows, err := s.storage.Query("SELECT json FROM book_sources")
	if err != nil {
		return fmt.Errorf("load sources: %w", err)
	}
	defer rows.Close()

	count := 0
	for rows.Next() {
		var jsonStr string
		if err := rows.Scan(&jsonStr); err != nil {
			continue
		}
		var src models.BookSource
		if err := json.Unmarshal([]byte(jsonStr), &src); err != nil {
			log.Printf("[SourceService] failed to parse source JSON: %v", err)
			continue
		}
		s.cache[src.BookSourceURL] = &src
		count++
	}
	log.Printf("[SourceService] loaded %d sources into cache", count)
	return rows.Err()
}

// List returns all book sources.
func (s *SourceService) List() []models.BookSource {
	result := make([]models.BookSource, 0, len(s.cache))
	for _, src := range s.cache {
		result = append(result, *src)
	}
	return result
}

// Get retrieves a single source by URL. 查无此源回 (nil, false)——缓存里的
// nil 值与缺席同义，不能拿去解引用（换源写回按 url 找源，找不到是常态）。
func (s *SourceService) Get(url string) (*models.BookSource, bool) {
	src, ok := s.cache[url]
	if !ok || src == nil {
		return nil, false
	}
	copy := *src
	return &copy, true
}

// Save persists a book source to DB and updates the cache.
func (s *SourceService) Save(src *models.BookSource) error {
	srcJSON, err := json.Marshal(src)
	if err != nil {
		return fmt.Errorf("marshal source: %w", err)
	}

	_, err = s.storage.Execute(
		`INSERT INTO book_sources (name, url, "group", enabled, custom_order, check_state, respond_time, json)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(url) DO UPDATE SET
		   name=excluded.name, "group"=excluded."group", enabled=excluded.enabled,
		   json=excluded.json, custom_order=excluded.custom_order, respond_time=excluded.respond_time`,
		src.BookSourceName, src.BookSourceURL, src.BookSourceGroup,
		boolToInt(src.Enabled), src.CustomOrder, 0, src.RespondTime,
		string(srcJSON),
	)
	if err != nil {
		return fmt.Errorf("save source: %w", err)
	}

	s.cache[src.BookSourceURL] = src
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "save", SourceID: src.BookSourceURL, Timestamp: core.NowSeconds()})
	return nil
}

// Delete removes a book source by URL.
func (s *SourceService) Delete(url string) error {
	_, err := s.storage.Execute("DELETE FROM book_sources WHERE url = ?", url)
	if err != nil {
		return fmt.Errorf("delete source: %w", err)
	}
	delete(s.cache, url)
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "delete", SourceID: url, Timestamp: core.NowSeconds()})
	return nil
}

// Enable sets the enabled flag on a source.
func (s *SourceService) Enable(url string, enabled bool) error {
	_, err := s.storage.Execute("UPDATE book_sources SET enabled = ? WHERE url = ?", boolToInt(enabled), url)
	if err != nil {
		return err
	}
	if src, ok := s.cache[url]; ok {
		src.Enabled = enabled
	}
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "enable", SourceID: url, Timestamp: core.NowSeconds()})
	return nil
}

// ExportAll exports all sources as a JSON array string.
func (s *SourceService) ExportAll() (string, error) {
	items := make([]models.BookSource, 0, len(s.cache))
	for _, src := range s.cache {
		items = append(items, *src)
	}
	data, err := json.MarshalIndent(items, "", "  ")
	return string(data), err
}

// ImportMultiple imports multiple sources from a JSON array.
func (s *SourceService) ImportMultiple(data string, mode string) (int, []error) {
	var sources []models.BookSource
	if err := json.Unmarshal([]byte(data), &sources); err != nil {
		return 0, []error{fmt.Errorf("parse import data: %w", err)}
	}

	saved := 0
	var errs []error
	for _, src := range sources {
		if src.BookSourceURL == "" {
			errs = append(errs, fmt.Errorf("source missing URL: %s", src.BookSourceName))
			continue
		}
		if err := s.Save(&src); err != nil {
			errs = append(errs, fmt.Errorf("save %s: %w", src.BookSourceURL, err))
		} else {
			saved++
		}
	}

	if saved > 0 {
		s.bus.Publish(&core.ImportProgressEvent{Kind: "book_source", Phase: "done", Done: saved, Total: len(sources)})
	}

	return saved, errs
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
