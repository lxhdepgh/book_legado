package models

// BookItem represents a book found during search or explore.
// Aligns with Python app/services/engine/reader_book.py find.BookInfo / find.FindBookResult.
type BookItem struct {
	Name        string `json:"name"`
	Author      string `json:"author"`
	BookURL     string `json:"bookUrl"`
	CoverURL    string `json:"coverUrl"`
	Intro       string `json:"intro"`
	LastChapter string `json:"lastChapter"`
	TocURL      string `json:"tocUrl"`
	WordCount   string `json:"wordCount,omitempty"`
	Kind        string `json:"kind,omitempty"`
	Origin      string `json:"origin"`        // source URL
	OriginName  string `json:"originName"`    // source name
	SourceGroup string `json:"sourceGroup"`
	TotalChapters int  `json:"totalChapters"`
	IsFresh     bool   `json:"isFresh"` // whether this is a new discovery (not in cache)
}
