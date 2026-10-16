package services

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// BookSnapshot 把 BookItem 拍成不可变快照字典（对齐 Python _book_item_dict）。
//
// 整字段快照而不是挑几个字段：换源/开读要从快照原样还原 BookItem，漏掉的
// toc_url/kind/last_chapter 会让目录、漫画通道和书架记录变哑。键名一律
// snake_case —— 事件载荷与 search_books.payload 共用这一份形状。
func BookSnapshot(b *models.BookItem) map[string]interface{} {
	if b == nil {
		return map[string]interface{}{}
	}
	origins := b.Origins
	if origins == nil {
		origins = []string{}
	}
	readConfig := b.ReadConfig
	if readConfig == nil {
		readConfig = map[string]interface{}{}
	}
	variables := b.Variables
	if variables == nil {
		variables = map[string]interface{}{}
	}
	var downloadURLs interface{}
	if b.DownloadURLs != nil {
		downloadURLs = b.DownloadURLs
	}
	return map[string]interface{}{
		"name":                 b.Name,
		"author":               b.Author,
		"intro":                b.Intro,
		"url":                  b.URL,
		"cover_url":            b.CoverURL,
		"toc_url":              b.TocURL,
		"kind":                 b.Kind,
		"last_chapter":         b.LastChapter,
		"latest_chapter_title": b.LatestChapterTitle,
		"word_count":           b.WordCount,
		"download_urls":        downloadURLs,
		"origin":               b.Origin,
		"origin_name":          b.OriginName,
		"type":                 b.Type,
		"total_chapter_num":    b.TotalChapterNum,
		"dur_chapter_index":    b.DurChapterIndex,
		"dur_chapter_title":    b.DurChapterTitle,
		"can_update":           b.CanUpdate,
		"custom_intro":         b.CustomIntro,
		"custom_tag":           b.CustomTag,
		"custom_cover_url":     b.CustomCoverURL,
		"charset":              b.Charset,
		"group":                b.Group,
		"latest_chapter_time":  b.LatestChapterTime,
		"last_check_time":      b.LastCheckTime,
		"last_check_count":     b.LastCheckCount,
		"dur_chapter_pos":      b.DurChapterPos,
		"dur_chapter_time":     b.DurChapterTime,
		"order":                b.Order,
		"origin_order":         b.OriginOrder,
		"read_config":          readConfig,
		"info_html":            b.InfoHTML,
		"toc_html":             b.TocHTML,
		"variables":            variables,
		"origins":              origins,
	}
}

// BookFromSnapshot 还原 BookSnapshot 产出的 snake_case 字典（FFI 入参解码同用）。
func BookFromSnapshot(raw map[string]interface{}) *models.BookItem {
	book := &models.BookItem{}
	str := func(key string) string {
		value, ok := raw[key].(string)
		if !ok {
			return ""
		}
		return value
	}
	num := func(key string) int {
		if value, ok := raw[key].(float64); ok {
			return int(value)
		}
		return 0
	}
	book.Name = str("name")
	book.Author = str("author")
	book.Intro = str("intro")
	book.URL = str("url")
	book.CoverURL = str("cover_url")
	book.TocURL = str("toc_url")
	book.Kind = str("kind")
	book.LastChapter = str("last_chapter")
	book.LatestChapterTitle = str("latest_chapter_title")
	book.WordCount = str("word_count")
	book.Origin = str("origin")
	book.OriginName = str("origin_name")
	book.Type = num("type")
	book.TotalChapterNum = num("total_chapter_num")
	book.DurChapterIndex = num("dur_chapter_index")
	book.DurChapterTitle = str("dur_chapter_title")
	if value, ok := raw["can_update"].(bool); ok {
		book.CanUpdate = value
	}
	book.CustomIntro = str("custom_intro")
	book.CustomTag = str("custom_tag")
	book.CustomCoverURL = str("custom_cover_url")
	book.Charset = str("charset")
	book.Group = num("group")
	book.LatestChapterTime = int64(num("latest_chapter_time"))
	book.LastCheckTime = int64(num("last_check_time"))
	book.LastCheckCount = num("last_check_count")
	book.DurChapterPos = num("dur_chapter_pos")
	book.DurChapterTime = int64(num("dur_chapter_time"))
	book.Order = num("order")
	book.OriginOrder = num("origin_order")
	book.InfoHTML = str("info_html")
	book.TocHTML = str("toc_html")
	if value, ok := raw["download_urls"].([]interface{}); ok {
		for _, item := range value {
			if s, ok := item.(string); ok {
				book.DownloadURLs = append(book.DownloadURLs, s)
			}
		}
	}
	if value, ok := raw["origins"].([]interface{}); ok {
		for _, item := range value {
			if s, ok := item.(string); ok {
				book.Origins = append(book.Origins, s)
			}
		}
	}
	if value, ok := raw["read_config"].(map[string]interface{}); ok {
		book.ReadConfig = value
	}
	if value, ok := raw["variables"].(map[string]interface{}); ok {
		book.Variables = value
	}
	return book
}

// SearchBookStore 是换源种子的落地表（对齐 Python SearchBookStore 主库模式）：
// 每次搜索逐源 INSERT OR REPLACE，book_url 主键即"一本一行"。
type SearchBookStore struct {
	storage *core.StorageService
}

// NewSearchBookStore wraps the main database.
func NewSearchBookStore(storage *core.StorageService) *SearchBookStore {
	return &SearchBookStore{storage: storage}
}

// Insert 批量落库（一批一条事务）。写失败由调用方决定是否吞掉 ——
// 种子只是给换源列表加速，落库失败不该吞掉已经合并好的这一条。
func (s *SearchBookStore) Insert(books []*models.BookItem) error {
	if s == nil || s.storage == nil || len(books) == 0 {
		return nil
	}
	now := time.Now().UnixMilli()
	rows := make([][]interface{}, 0, len(books))
	for _, book := range books {
		if book == nil || book.URL == "" {
			continue
		}
		payload, err := json.Marshal(BookSnapshot(book))
		if err != nil {
			continue
		}
		rows = append(rows, []interface{}{
			book.URL, book.Name, book.Author, book.Kind, book.Origin,
			book.OriginOrder, now, string(payload), now,
		})
	}
	if len(rows) == 0 {
		return nil
	}
	return s.storage.Transaction(func(tx *sql.Tx) error {
		for _, row := range rows {
			if _, err := tx.Exec(
				`INSERT OR REPLACE INTO search_books
				 (book_url, name, author, kind, origin, source_group, origin_order,
				  saved_at, payload, updated_at)
				 VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?)`,
				row...); err != nil {
				return fmt.Errorf("insert search book: %w", err)
			}
		}
		return nil
	})
}

// ListByNameAuthor 取书名精确、作者可模糊（空=任意）的已存书（对齐 Python
// list_by_name_author / Legado changeSourceByGroup），ORDER BY origin_order, book_url。
func (s *SearchBookStore) ListByNameAuthor(name, author string, limit int) []*models.BookItem {
	if s == nil || s.storage == nil || name == "" {
		return nil
	}
	if limit <= 0 {
		limit = 1
	}
	query := "SELECT payload FROM search_books WHERE name = ?"
	args := []interface{}{name}
	if author != "" {
		query += " AND author LIKE ?"
		args = append(args, "%"+author+"%")
	}
	query += " ORDER BY origin_order, book_url LIMIT ?"
	args = append(args, limit)

	rows, err := s.storage.Query(query, args...)
	if err != nil {
		return nil
	}
	defer rows.Close()

	var books []*models.BookItem
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			continue
		}
		var raw map[string]interface{}
		if err := json.Unmarshal([]byte(payload), &raw); err != nil {
			continue
		}
		books = append(books, BookFromSnapshot(raw))
	}
	return books
}

// ClearExpired 清掉 TTL 之外的搜索落库行（对齐 Python clear_expired）。
func (s *SearchBookStore) ClearExpired(beforeMs int64) {
	if s == nil || s.storage == nil {
		return
	}
	_, _ = s.storage.Execute("DELETE FROM search_books WHERE saved_at < ?", beforeMs)
}
