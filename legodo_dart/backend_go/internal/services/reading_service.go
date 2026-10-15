package services

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// ReadingSession represents an active book reading session.
type ReadingSession struct {
	BookURL     string         `json:"bookUrl"`
	Name        string         `json:"name"`
	Author      string         `json:"author"`
	TocURL      string         `json:"tocUrl"`
	LastChapter int            `json:"lastChapter"`
	ReadTime    time.Time      `json:"readTime"`
	CachedChaps []*models.Chapter `json:"-"`
}

// ReadingService manages reading sessions, chapter prefetching, and progress persistence.
type ReadingService struct {
	base   *core.BaseService
	hub    *ServiceHub
	mu     sync.RWMutex
	session *ReadingSession
	cache  map[string]*models.ChapterContent // chapter URL -> content
	prefetchWg sync.WaitGroup
}

// NewReadingService creates a new reading service.
func NewReadingService(hub *ServiceHub) *ReadingService {
	s := &ReadingService{
		base: core.NewBaseService("ReadingService", "SourceService", "RuleEngineService"),
		hub:  hub,
		cache: make(map[string]*models.ChapterContent),
	}
	return s
}

// Start initializes the service.
func (s *ReadingService) Start() error { return s.base.Start() }
func (s *ReadingService) Stop() error  { return s.base.Stop() }

// OpenBook starts a new reading session for a book.
func (s *ReadingService) OpenBook(ctx context.Context, bookURL string) (*ReadingSession, error) {
	sourceSvc := s.hub.SourceService()
	src, ok := sourceSvc.Get(bookURL)
	if !ok {
		return nil, fmt.Errorf("source not found: %s", bookURL)
	}

	bookInfoSvc := s.hub.RuleEngine()
	bookItem, err := bookInfoSvc.GetBookInfo(ctx, src, bookURL)
	if err != nil {
		return nil, fmt.Errorf("get book info: %w", err)
	}

	session := &ReadingSession{
		BookURL:  bookURL,
		Name:     bookItem.Name,
		Author:   bookItem.Author,
		TocURL:   bookItem.TocURL,
		ReadTime: time.Now(),
	}

	chapters, err := bookInfoSvc.GetChapters(ctx, src, bookItem.TocURL)
	if err != nil {
		return nil, fmt.Errorf("get chapters: %w", err)
	}

	session.CachedChaps = chapters
	s.session = session

	// Publish event
	bus := s.hub.EventBus()
	if bus != nil {
		bus.Publish(&core.ReadingSessionChangedEvent{
			BookURL:      session.BookURL,
			ChapterIndex: session.LastChapter,
		})
	}

	return session, nil
}

// Close releases the current reading session.
func (s *ReadingService) Close() {
	s.mu.Lock()
	s.cache = make(map[string]*models.ChapterContent)
	s.session = nil
	s.prefetchWg.Wait()
	s.mu.Unlock()
}

// GetToc returns the table of contents for the current session.
func (s *ReadingService) GetToc() []*models.Chapter {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.session == nil {
		return nil
	}
	cp := make([]*models.Chapter, len(s.session.CachedChaps))
	copy(cp, s.session.CachedChaps)
	return cp
}

// GetChapter retrieves a single chapter's content, loading from cache or fetching from source.
func (s *ReadingService) GetChapter(ctx context.Context, chapter *models.Chapter) (*models.ChapterContent, error) {
	s.mu.RLock()
	if c, ok := s.cache[chapter.URL]; ok {
		s.mu.RUnlock()
		cp := *c
		return &cp, nil
	}
	s.mu.RUnlock()

	sourceSvc := s.hub.SourceService()
	source, _ := sourceSvc.Get(s.session.BookURL)
	content, err := s.hub.RuleEngine().GetContent(ctx, source, chapter)
	if err != nil {
		return nil, err
	}

	s.mu.Lock()
	s.cache[chapter.URL] = content
	s.mu.Unlock()

	return content, nil
}

// SavePosition persists the current reading position to settings.
func (s *ReadingService) SavePosition(chapterIndex int, offset float64) error {
	pos := map[string]interface{}{
		"chapterIndex": chapterIndex,
		"offset":       offset,
		"timestamp":    time.Now().UnixMilli(),
	}
	data, err := json.Marshal(pos)
	if err != nil {
		return err
	}
	settingsSvc := s.hub.SettingsService()
	return settingsSvc.Set(fmt.Sprintf("reading.%s.position", s.session.BookURL), string(data))
}

// GetPosition retrieves the last saved reading position.
func (s *ReadingService) GetPosition() (int, float64) {
	settingsSvc := s.hub.SettingsService()
	val := settingsSvc.Get(fmt.Sprintf("reading.%s.position", s.session.BookURL), "")
	if val == "" {
		return 0, 0
	}
	var pos map[string]interface{}
	if err := json.Unmarshal([]byte(val), &pos); err != nil {
		return 0, 0
	}
	chapIdx := int(pos["chapterIndex"].(float64))
	offset := pos["offset"].(float64)
	return chapIdx, offset
}

// OpenSession starts a new reading session directly (used by FFI engine).
func (s *ReadingService) OpenSession(sess *ReadingSession) {
	s.mu.Lock()
	s.session = sess
	s.mu.Unlock()
}

// GetSession returns the current reading session (nil if none open).
func (s *ReadingService) GetSession() *ReadingSession {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.session
}

// PrefetchChapters loads upcoming chapters into the content cache.
func (s *ReadingService) PrefetchChapters(ctx context.Context, fromChapter int, count int) {
	s.mu.RLock()
	chapList := s.session.CachedChaps
	s.mu.RUnlock()

	end := fromChapter + count
	if end > len(chapList) {
		end = len(chapList)
	}

	for i := fromChapter; i < end; i++ {
		chapter := chapList[i]
		_, err := s.GetChapter(ctx, chapter)
		if err != nil {
			continue // skip errors during prefetch
		}
	}
}
