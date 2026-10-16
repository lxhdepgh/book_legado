package services

import (
	"time"

	"github.com/legodo-booksource/legodo_go/core"
)

// Bookmark represents a reader bookmark row. Keys align with the Python
// gateway contract (app/services/reading/bookmark_service.py list_bookmarks)
// and Dart's BookmarkRow.fromJson: snake_case, created_at in epoch seconds.
type Bookmark struct {
	ID         int64   `json:"bookmark_id"`
	BookURL    string  `json:"book_url"`
	ChapterIdx int     `json:"chapter_index"`
	Offset     float64 `json:"offset"`
	Summary    string  `json:"summary"`
	CreatedAt  float64 `json:"created_at"`
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

// Start brings the reader service up. The canonical bookmarks table is
// created by StorageService's schema; no service-local table here.
func (s *ReaderService) Start() error { return s.base.Start() }
func (s *ReaderService) Stop() error  { return s.base.Stop() }

// AddBookmark creates a bookmark for a book at current position.
func (s *ReaderService) AddBookmark(bookURL string, chapterIdx int, offset float64, summary string) (*Bookmark, error) {
	bm := &Bookmark{
		BookURL:    bookURL,
		ChapterIdx: chapterIdx,
		Offset:     offset,
		Summary:    summary,
		CreatedAt:  float64(time.Now().UnixNano()) / 1e9,
	}
	result, err := s.storage.Execute(
		`INSERT INTO bookmarks (book_url, chapter_index, offset, summary, created_at) VALUES (?, ?, ?, ?, ?)`,
		bm.BookURL, bm.ChapterIdx, bm.Offset, bm.Summary, bm.CreatedAt,
	)
	if err != nil {
		return nil, err
	}
	id, _ := result.LastInsertId()
	bm.ID = id
	return bm, nil
}

// RemoveBookmark deletes a bookmark by id, reporting whether a row was hit
// (the row may already be gone, removed from another view).
func (s *ReaderService) RemoveBookmark(bookURL string, bookmarkID int64) (bool, error) {
	result, err := s.storage.Execute("DELETE FROM bookmarks WHERE book_url = ? AND id = ?", bookURL, bookmarkID)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}

// ListBookmarks returns all bookmarks for a book, oldest first.
func (s *ReaderService) ListBookmarks(bookURL string) ([]Bookmark, error) {
	rows, err := s.storage.Query("SELECT id, book_url, chapter_index, offset, summary, created_at FROM bookmarks WHERE book_url = ? ORDER BY created_at ASC, id ASC", bookURL)
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
	_, err := s.storage.Execute("DELETE FROM bookmarks WHERE book_url = ?", bookURL)
	return err
}
