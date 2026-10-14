package models

// Chapter represents a single chapter in a book's table of contents.
type Chapter struct {
	Index   int    `json:"index"`
	Title   string `json:"title"`
	URL     string `json:"url"`
	BookURL string `json:"bookUrl"` // parent book URL
	IsVip   bool   `json:"isVip"`
	IsPay   bool   `json:"isPay"`
}

// ChapterContent represents the rendered content of a single chapter.
type ChapterContent struct {
	ChapterURL  string `json:"chapterUrl"`
	Title       string `json:"title"`
	Content     string `json:"content"`
	SubTitle    string `json:"subTitle,omitempty"`
	SubContent  string `json:"subContent,omitempty"`
	ParsedAt    int64  `json:"parsedAt"`
	NextURL     string `json:"nextUrl,omitempty"` // pagination
}
