package services

import (
	"encoding/json"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/models"
)

// BookshelfService manages the bookshelf (in-shelf books).
type BookshelfService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
}

// NewBookshelfService creates a new bookshelf service.
func NewBookshelfService(storage *core.StorageService, bus *core.EventBus) *BookshelfService {
	return &BookshelfService{
		base:    core.NewBaseService("BookshelfService", "StorageService"),
		storage: storage,
		bus:     bus,
	}
}

// Start initializes.
func (s *BookshelfService) Start() error { return s.base.Start() }
func (s *BookshelfService) Stop() error  { return s.base.Stop() }

// List returns all in-shelf books.
func (s *BookshelfService) List() ([]map[string]interface{}, error) {
	rows, err := s.storage.Query("SELECT book_url, name, author, kind, cover_url, source_url, reading, vars, payload FROM bookshelf WHERE in_bookshelf = 1")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := make([]map[string]interface{}, 0)
	for rows.Next() {
		var reading, vars, payload string
		var b models.BookItem
		if scanErr := rows.Scan(&b.BookURL, &b.Name, &b.Author, &b.Kind, &b.CoverURL, &b.Origin, &reading, &vars, &payload); scanErr != nil {
			continue
		}
		item := map[string]interface{}{
			"bookUrl":  b.BookURL,
			"name":     b.Name,
			"author":   b.Author,
			"kind":     b.Kind,
			"coverUrl": b.CoverURL,
			"origin":   b.Origin,
			"reading":  reading,
			"vars":     vars,
			"payload":  payload,
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

// Add adds a book to the shelf.
func (s *BookshelfService) Add(book *models.BookItem) error {
	readJSON, _ := json.Marshal(map[string]interface{}{})
	varsJSON, _ := json.Marshal(map[string]interface{}{})
	payloadJSON, _ := json.Marshal(map[string]interface{}{})

	_, err := s.storage.Execute(
		`INSERT OR REPLACE INTO bookshelf (book_url, name, author, kind, cover_url, source_url, in_bookshelf, reading, vars, payload, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)`,
		book.BookURL, book.Name, book.Author, book.Kind, book.CoverURL, book.Origin,
		string(readJSON), string(varsJSON), string(payloadJSON), time.Now().UnixMilli(),
	)
	if err != nil {
		return err
	}
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "add_to_shelf", SourceURL: book.BookURL})
	return nil
}

// Remove removes a book from the shelf.
func (s *BookshelfService) Remove(bookURL string) error {
	_, err := s.storage.Execute("UPDATE bookshelf SET in_bookshelf = 0 WHERE book_url = ?", bookURL)
	return err
}

// GetBook retrieves a single book's data from shelf.
func (s *BookshelfService) GetBook(bookURL string) (map[string]interface{}, error) {
	row := s.storage.QueryRow("SELECT book_url, name, author, kind, cover_url, source_url, reading, vars, payload FROM bookshelf WHERE book_url = ?", bookURL)
	var b models.BookItem
	var reading, vars, payload string
	if err := row.Scan(&b.BookURL, &b.Name, &b.Author, &b.Kind, &b.CoverURL, &b.Origin, &reading, &vars, &payload); err != nil {
		return nil, err
	}
	return map[string]interface{}{
		"url":      b.BookURL,
		"name":     b.Name,
		"author":   b.Author,
		"kind":     b.Kind,
		"coverUrl": b.CoverURL,
		"origin":   b.Origin,
		"reading":  reading,
	}, nil
}

// SaveReading saves reading position and vars for a book on shelf.
func (s *BookshelfService) SaveReading(bookURL string, reading interface{}, vars interface{}) error {
	rj, _ := json.Marshal(reading)
	vj, _ := json.Marshal(vars)
	_, err := s.storage.Execute(
		`UPDATE bookshelf SET reading = ?, vars = ?, updated_at = ? WHERE book_url = ?`,
		string(rj), string(vj), time.Now().UnixMilli(), bookURL,
	)
	return err
}

// ToMap converts a BookItem to a map representation.
func ToMap(b models.BookItem) map[string]interface{} {
	return map[string]interface{}{"url": b.BookURL, "name": b.Name, "author": b.Author}
}
