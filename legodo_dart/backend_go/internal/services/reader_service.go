package services

import (
	"encoding/json"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
)

// Bookmark represents a reader bookmark.
type Bookmark struct {
	ID         int64     `json:"id"`
	BookURL    string    `json:"bookUrl"`
	ChapterIdx int       `json:"chapterIndex"`
	Offset     float64   `json:"offset"`
	Summary    string    `json:"summary"`
	CreatedAt  int64     `json:"createdAt"`
}

// ReaderService manages bookmarks for books being read.
type ReaderService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
}

// NewReaderService creates a new reader service.
func NewReaderService(storage *core.StorageService, bus *core.EventBus) *ReaderService {
	return &ReaderService{
		base:    core.NewBaseService("ReaderService", "StorageService"),
		storage: storage,
		bus:     bus,
	}
}

// Start initializes bookmarks table.
func (s *ReaderService) Start() error {
	s.base.SetOnStart(func() error {
		_, err := s.storage.Execute(`
		CREATE TABLE IF NOT EXISTS reading_bookmarks (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			book_url TEXT NOT NULL,
			chapter_index INTEGER NOT NULL,
			offset REAL DEFAULT 0,
			summary TEXT,
			created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
			UNIQUE(book_url, chapter_index, offset)
		);
		CREATE INDEX IF NOT EXISTS idx_reading_bm_book ON reading_bookmarks(book_url);
		`)
		return err
	})
	return s.base.Start()
}
func (s *ReaderService) Stop() error { return s.base.Stop() }

// AddBookmark creates a bookmark for a book at current position.
func (s *ReaderService) AddBookmark(bookURL string, chapterIdx int, offset float64, summary string) (*Bookmark, error) {
	bm := &Bookmark{
		BookURL:    bookURL,
		ChapterIdx: chapterIdx,
		Offset:     offset,
		Summary:    summary,
		CreatedAt:  time.Now().UnixMilli(),
	}
	result, err := s.storage.Execute(
		`INSERT INTO reading_bookmarks (book_url, chapter_index, offset, summary, created_at) VALUES (?, ?, ?, ?, ?)`,
		bm.BookURL, bm.ChapterIdx, bm.Offset, bm.Summary, bm.CreatedAt,
	)
	if err != nil {
		return nil, err
	}
	id, _ := result.LastInsertId()
	bm.ID = id
	return bm, nil
}

// RemoveBookmark deletes a specific bookmark.
func (s *ReaderService) RemoveBookmark(bookURL string, chapterIdx int, offset float64) error {
	_, err := s.storage.Execute("DELETE FROM reading_bookmarks WHERE book_url = ? AND chapter_index = ? AND offset = ?", bookURL, chapterIdx, offset)
	return err
}

// ListBookmarks returns all bookmarks for a book.
func (s *ReaderService) ListBookmarks(bookURL string) ([]Bookmark, error) {
	rows, err := s.storage.Query("SELECT id, book_url, chapter_index, offset, summary, created_at FROM reading_bookmarks WHERE book_url = ? ORDER BY chapter_index, offset", bookURL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var bms []Bookmark
	for rows.Next() {
		var bm Bookmark
		if scanErr := rows.Scan(&bm.ID, &bm.BookURL, &bm.ChapterIdx, &bm.Offset, &bm.Summary, &bm.CreatedAt); scanErr != nil {
			continue
		}
		bms = append(bms, bm)
	}
	return bms, rows.Err()
}

// DeleteAllBookmarks removes all bookmarks for a book.
func (s *ReaderService) DeleteAllBookmarks(bookURL string) error {
	_, err := s.storage.Execute("DELETE FROM reading_bookmarks WHERE book_url = ?", bookURL)
	return err
}

// SavePosition marshals position data and stores via SettingsService pattern.
func (s *ReaderService) SavePosition(key string, position interface{}) error {
	data, err := json.Marshal(position)
	if err != nil {
		return err
	}
	_, err = s.storage.Execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)", key, string(data))
	return err
}

// ReadPosition retrieves stored position JSON from settings.
func (s *ReaderService) ReadPosition(key string) (string, bool) {
	row := s.storage.QueryRow("SELECT value FROM settings WHERE key = ?", key)
	var val string
	if err := row.Scan(&val); err != nil {
		return "", false
	}
	return val, true
}
