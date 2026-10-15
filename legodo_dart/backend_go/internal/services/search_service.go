package services

import (
	"strings"

	"github.com/legodo-booksource/legodo_go/core"
)

// SearchResult is a single search match from FTS5.
type SearchResult struct {
	BookURL      string `json:"bookUrl"`
	ChapterTitle string `json:"chapterTitle"`
	Snippet      string `json:"snippet"`
}

// ContentSearchService provides full-text search over book content using FTS5.
type ContentSearchService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
}

// NewContentSearchService creates a new content search service.
func NewContentSearchService(storage *core.StorageService, bus *core.EventBus) *ContentSearchService {
	s := &ContentSearchService{
		base:    core.NewBaseService("ContentSearchService"),
		storage: storage,
		bus:     bus,
	}
	return s
}

// Start initializes the service.
func (s *ContentSearchService) Start() error {
	s.base.SetOnStart(func() error {
		return s.ensureFTSTable()
	})
	return s.base.Start()
}
func (s *ContentSearchService) Stop() error { return s.base.Stop() }

// ensureFTSTable creates the FTS5 virtual table for content search.
func (s *ContentSearchService) ensureFTSTable() error {
	_, err := s.storage.Execute(`
	CREATE VIRTUAL TABLE IF NOT EXISTS content_search USING fts5(
		book_url, chapter_title, content
	);
	`)
	return err
}

// IndexContent adds or updates a chapter's content in the FTS index.
func (s *ContentSearchService) IndexContent(bookURL, chapterTitle, content string) error {
	s.storage.Execute("DELETE FROM content_search WHERE book_url = ?", bookURL)
	s.storage.Execute("INSERT INTO content_search VALUES (?, ?, ?)", bookURL, chapterTitle, content)
	return nil
}

// RemoveBookFromIndex removes all content for a book from the FTS index.
func (s *ContentSearchService) RemoveBookFromIndex(bookURL string) error {
	_, err := s.storage.Execute("DELETE FROM content_search WHERE book_url = ?", bookURL)
	return err
}

// Search searches across all indexed book content.
func (s *ContentSearchService) Search(query string, bookURL string, limit int) ([]SearchResult, error) {
	if query == "" {
		return nil, nil
	}

	query = strings.TrimSpace(query)
	if limit <= 0 || limit > 100 {
		limit = 20
	}

	var stmt string
	var args []interface{}
	if bookURL != "" {
		stmt = `SELECT book_url, chapter_title, snippet(content_search, 0, '«', '»', '...', '', -1, 300, false, false) as snippet
		        FROM content_search
		        WHERE content_search MATCH ? AND book_url = ? ORDER BY rank LIMIT ?`
		args = []interface{}{query, bookURL, limit}
	} else {
		stmt = `SELECT book_url, chapter_title, snippet(content_search, 0, '«', '»', '...', '', -1, 300, false, false) as snippet
		        FROM content_search
		        WHERE content_search MATCH ? ORDER BY rank LIMIT ?`
		args = []interface{}{query, limit}
	}

	rows, err := s.storage.Query(stmt, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	results := make([]SearchResult, 0)
	for rows.Next() {
		var r SearchResult
		if scanErr := rows.Scan(&r.BookURL, &r.ChapterTitle, &r.Snippet); scanErr != nil {
			continue
		}
		results = append(results, r)
	}
	return results, rows.Err()
}

// IndexedChapters returns the number of chapters indexed for a book.
func (s *ContentSearchService) IndexedChapters(bookURL string) int {
	row := s.storage.QueryRow("SELECT COUNT(DISTINCT chapter_title) FROM content_search WHERE book_url = ?", bookURL)
	var count int
	if err := row.Scan(&count); err != nil {
		return 0
	}
	return count
}

// FTSAvailable returns whether the FTS index table exists.
func (s *ContentSearchService) FTSAvailable() bool {
	row := s.storage.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='content_search'")
	var c int
	if err := row.Scan(&c); err != nil {
		return false
	}
	return c > 0
}
