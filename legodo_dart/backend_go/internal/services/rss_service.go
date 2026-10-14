package services

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/antchfx/xmlquery"
	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
)

// RSSSource represents an RSS feed subscription.
type RSSSource struct {
	ID          int64     `json:"id"`
	Name        string    `json:"name"`
	URL         string    `json:"url"`
	Group       string    `json:"group"`
	Enabled     bool      `json:"enabled"`
	RSSType     int       `json:"rssType"`
	LastUpdate  int64     `json:"lastUpdateTime"`
	CustomOrder int       `json:"customOrder"`
}

// RSSArticle represents a parsed article from an RSS feed.
type RSSArticle struct {
	ID        int64     `json:"id"`
	SourceID  int64     `json:"sourceId"`
	Title     string    `json:"title"`
	URL       string    `json:"url"`
	Author    string    `json:"author"`
	PubDate   string    `json:"pubDate"`
	Image     string    `json:"image"`
	Read      bool      `json:"read"`
	Starred   bool      `json:"starred"`
	Text      string    `json:"text"`
	CreatedAt int64     `json:"createdAt"`
}

// RSSService manages RSS source subscriptions and articles.
type RSSService struct {
	base  *core.BaseService
	storage *core.StorageService
	bus   *core.EventBus
	httpC *httpclient.ClientSession
	mu    sync.RWMutex
	sources map[int64]*RSSSource
}

// NewRSSService creates a new RSS service.
func NewRSSService(storage *core.StorageService, bus *core.EventBus, hc *httpclient.ClientSession) *RSSService {
	return &RSSService{
		base:    core.NewBaseService("RSSService", "StorageService"),
		storage: storage,
		bus:     bus,
		httpC:   hc,
		sources: make(map[int64]*RSSSource),
	}
}

// Start loads RSS sources from DB into cache.
func (s *RSSService) Start() error {
	s.base.SetOnStart(func() error { return s.loadAllSources() })
	return s.base.Start()
}
func (s *RSSService) Stop() error { return s.base.Stop() }

func (s *RSSService) loadAllSources() error {
	rows, err := s.storage.Query("SELECT id, name, url, \"group\", enabled, rss_type, last_update_time, custom_order FROM rss_sources WHERE enabled = 1")
	if err != nil || rows == nil {
		return err
	}
	defer rows.Close()

	for rows.Next() {
		var src RSSSource
		var group sql.NullString
		if err := rows.Scan(&src.ID, &src.Name, &src.URL, &group, &src.Enabled, &src.RSSType, &src.LastUpdate, &src.CustomOrder); err != nil {
			continue
		}
		src.Group = group.String
		s.sources[src.ID] = &src
	}
	return rows.Err()
}

// ListSources returns all enabled RSS sources.
func (s *RSSService) ListSources() []RSSSource {
	result := make([]RSSSource, 0, len(s.sources))
	for _, src := range s.sources {
		result = append(result, *src)
	}
	return result
}

// AddSource saves a new RSS source.
func (s *RSSService) AddSource(src *RSSSource) error {
	_, err := s.storage.Execute(
		`INSERT INTO rss_sources (name, url, "group", enabled, rss_type, last_update_time, custom_order) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		src.Name, src.URL, src.Group, boolToInt(src.Enabled), src.RSSType, src.LastUpdate, src.CustomOrder,
	)
	if err != nil {
		return err
	}
	s.sources[src.ID] = src
	s.bus.Publish(&core.RssRefreshedEvent{SourceID: src.Name, NewCount: 0})
	return nil
}

// RemoveSource deletes an RSS source and its articles.
func (s *RSSService) RemoveSource(id int64) error {
	_, err := s.storage.Execute("DELETE FROM rss_articles WHERE source_id = ?", id)
	if err != nil {
		return err
	}
	_, err = s.storage.Execute("DELETE FROM rss_sources WHERE id = ?", id)
	if err != nil {
		return err
	}
	delete(s.sources, id)
	return nil
}

// Refresh fetches articles for a single RSS source.
func (s *RSSService) Refresh(ctx context.Context, sourceID int64) ([]*RSSArticle, error) {
	s.mu.RLock()
	src, ok := s.sources[sourceID]
	s.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("rss source %d not found", sourceID)
	}

	resp, err := s.httpC.Get(ctx, src.URL, nil)
	if err != nil {
		return nil, fmt.Errorf("fetch rss: %w", err)
	}

	body := resp.BodyString()
	doc, err := xmlquery.Parse(strings.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("parse rss xml: %w", err)
	}

	items := xmlquery.Find(doc, "//item")
	articles := make([]*RSSArticle, 0, len(items))
	for _, item := range items {
		title := xmlquery.FindOne(item, "title")
		link := xmlquery.FindOne(item, "link")
		pubDate := xmlquery.FindOne(item, "pubDate")
		enclosure := xmlquery.FindOne(item, "enclosure")
		description := xmlquery.FindOne(item, "description")

		a := &RSSArticle{
			SourceID: sourceID,
			Title:    nodeText(title),
			URL:      nodeAttr(link, "href"),
			PubDate:  nodeText(pubDate),
			Image:    nodeAttr(enclosure, "url"),
			Read:     false,
			Starred:  false,
			Text:     nodeText(description),
			CreatedAt: time.Now().UnixMilli(),
		}
		articles = append(articles, a)
	}

	// Save to DB
	txErr := s.storage.Transaction(func(tx *sql.Tx) error {
		stmt, _ := tx.Prepare(`INSERT INTO rss_articles (source_id, title, url, author, pub_date, image, read, starred, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
		defer stmt.Close()
		for _, a := range articles {
			_, err := stmt.Exec(a.SourceID, a.Title, a.URL, "", a.PubDate, a.Image, 0, 0, a.Text, a.CreatedAt)
			if err != nil {
				return err
			}
		}
		_, err := tx.Exec("UPDATE rss_sources SET last_update_time = ? WHERE id = ?", time.Now().UnixMilli(), sourceID)
		return err
	})
	if txErr != nil {
		return articles, txErr
	}

	return articles, nil
}

// GetArticles returns articles for a source with pagination.
func (s *RSSService) GetArticles(sourceID int64, offset, limit int) ([]*RSSArticle, error) {
	query := `SELECT id, title, url, author, pub_date, image, read, starred, text, created_at
	          FROM rss_articles WHERE source_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?`
	rows, err := s.storage.Query(query, sourceID, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	articles := make([]*RSSArticle, 0)
	for rows.Next() {
		var a RSSArticle
		var readInt, starredInt int
		if scanErr := rows.Scan(&a.ID, &a.Title, &a.URL, &a.Author, &a.PubDate, &a.Image, &readInt, &starredInt, &a.Text, &a.CreatedAt); scanErr != nil {
			continue
		}
		a.Read = readInt == 1
		a.Starred = starredInt == 1
		articles = append(articles, &a)
	}
	return articles, rows.Err()
}

// MarkRead marks articles by URL as read.
func (s *RSSService) MarkRead(urls []string) error {
	for _, u := range urls {
		s.storage.Execute("UPDATE rss_articles SET read = 1 WHERE url = ?", u)
	}
	return nil
}

// Star toggles starred status for an article.
func (s *RSSService) Star(url string, starred bool) error {
	val := 0
	if starred {
		val = 1
	}
	_, err := s.storage.Execute("UPDATE rss_articles SET starred = ? WHERE url = ?", val, url)
	return err
}

// RefreshAll refreshes all enabled RSS sources concurrently.
func (s *RSSService) RefreshAll(ctx context.Context) []error {
	s.mu.RLock()
	srcs := make([]*RSSSource, 0, len(s.sources))
	for _, src := range s.sources {
		srcs = append(srcs, src)
	}
	s.mu.RUnlock()

	var mu sync.Mutex
	errs := make([]error, 0)
	sem := make(chan struct{}, 5)

	for _, src := range srcs {
		go func(src *RSSSource) {
			sem <- struct{}{}
			defer func() { <-sem }()
			_, err := s.Refresh(ctx, src.ID)
			if err != nil {
				mu.Lock()
				errs = append(errs, fmt.Errorf("refresh %s: %w", src.Name, err))
				mu.Unlock()
			}
		}(src)
	}

	time.Sleep(time.Second * 30) // Wait up to 30s
	return errs
}

// nodeText returns the text content of an XML element node, or "" if nil/empty.
func nodeText(n *xmlquery.Node) string {
	if n == nil {
		return ""
	}
	// xmlquery.Text() already exists as a method
	return n.Data
}

// nodeAttr returns the attribute value for a key from an XML node, or "" if not found.
func nodeAttr(n *xmlquery.Node, key string) string {
	if n == nil {
		return ""
	}
	for _, attr := range n.Attr {
		if attr.Name.Local == key {
			return attr.Value
		}
	}
	return ""
}
