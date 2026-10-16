package services

import (
	"encoding/json"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	internalmodels "github.com/legodo-booksource/legodo_go/internal/models"
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

// List returns shelf record rows (34-key record shape; staged rows only when
// includeStaged). 行正文就是 payload 那一份（flet M6 load_bookshelf 只读 payload），
// 进度在它的 `reading` 子对象里。旧实现从 books 表 JOIN 出 reading 列——那列只存在
// 于 Go 自建库，Python 建的库里 books 根本没有这一列，对真库执行直接 SQL 报错。
func (s *BookshelfService) List(includeStaged bool) ([]map[string]interface{}, error) {
	query := `SELECT payload, book_url, source_url, name, author, kind, cover_url, in_bookshelf
		FROM bookshelf`
	if !includeStaged {
		query += " WHERE in_bookshelf = 1"
	}
	rows, err := s.storage.Query(query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := make([]map[string]interface{}, 0)
	for rows.Next() {
		var payload, bookURL, sourceURL, name, author, kind, coverURL string
		var inShelf int
		if scanErr := rows.Scan(&payload, &bookURL, &sourceURL, &name, &author,
			&kind, &coverURL, &inShelf); scanErr != nil {
			continue
		}
		record := map[string]interface{}{}
		if json.Unmarshal([]byte(payload), &record) != nil || len(record) == 0 {
			record = map[string]interface{}{
				"name": name, "author": author, "kind": kind,
				"cover_url": coverURL,
			}
		}
		record["url"] = bookURL
		record["bookUrl"] = bookURL
		record["source_url"] = sourceURL
		record["in_bookshelf"] = inShelf != 0
		result = append(result, record)
	}
	return result, rows.Err()
}

// patchShelfReading 把一篇进度（+可选规则变量）镜像进书架行：payload 的 `reading`
// 子对象与 durChapter* 别名、reading/vars 列一次落盘同值（对齐 flet save_bookshelf
// 的"payload 与列同一次写"）。行不在架上时静默跳过——进度本体在 books 表，
// 不因书架行缺席而丢。reading 为 nil 表示只落变量。
func (s *BookshelfService) patchShelfReading(bookURL string, reading map[string]interface{},
	variables interface{}) {
	if s == nil || s.storage == nil || bookURL == "" || (reading == nil && variables == nil) {
		return
	}
	var payload, currentReading, currentVars string
	if err := s.storage.QueryRow(
		`SELECT payload, COALESCE(reading, '{}'), COALESCE(vars, '{}')
		 FROM bookshelf WHERE book_url = ?`, bookURL).Scan(
		&payload, &currentReading, &currentVars); err != nil {
		return
	}
	record := map[string]interface{}{}
	if err := json.Unmarshal([]byte(payload), &record); err != nil {
		record = map[string]interface{}{}
	}
	newReading, newVars := currentReading, currentVars
	if reading != nil {
		record["reading"] = reading
		// durChapter* 与 reading 同源（flet update_bookshelf_state 同款别名）。
		if title := mapString(reading, "chapter_name"); title == "" {
			record["durChapterTitle"] = nil
		} else {
			record["durChapterTitle"] = title
		}
		record["durChapterIndex"] = mapInt(reading, "chapter_index")
		record["durChapterPos"] = mapInt(reading, "chapter_pos")
		record["durChapterTime"] = mapInt64(reading, "last_read_at") * 1000
		encoding, err := json.Marshal(reading)
		if err != nil {
			return
		}
		newReading = string(encoding)
	}
	if variables != nil {
		record["variables"] = variables
		encoding, err := json.Marshal(variables)
		if err != nil {
			return
		}
		newVars = string(encoding)
	}
	patched, err := json.Marshal(record)
	if err != nil {
		return
	}
	_, _ = s.storage.Execute(
		`UPDATE bookshelf SET payload = ?, reading = ?, vars = ?, updated_at = ?
		 WHERE book_url = ?`,
		string(patched), newReading, newVars, time.Now().UnixMilli(), bookURL)
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
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "add_to_shelf", SourceID: book.BookURL, Timestamp: core.NowSeconds()})
	return nil
}

// Remove removes a book from the shelf.
func (s *BookshelfService) Remove(bookURL string) error {
	_, err := s.storage.Execute("UPDATE bookshelf SET in_bookshelf = 0 WHERE book_url = ?", bookURL)
	return err
}

// SwitchSource 换源写回（对齐 Python SourceService.switch_bookshelf_source）：
// 找到原行、查重（新书 url 不许撞上另一行）、改写身份字段。章节 url 属于旧源，
// 只清 chapter_url 与滚动偏移，按章序号语义的进度照留。
func (s *BookshelfService) SwitchSource(oldBookURL string, source *internalmodels.BookSource,
	book *internalmodels.BookItem) bool {
	if oldBookURL == "" || source == nil || book == nil || book.URL == "" {
		return false
	}
	var existing string
	if err := s.storage.QueryRow("SELECT book_url FROM bookshelf WHERE book_url = ?",
		oldBookURL).Scan(&existing); err != nil {
		return false
	}
	var conflict int
	_ = s.storage.QueryRow("SELECT COUNT(*) FROM bookshelf WHERE book_url = ? AND book_url != ?",
		book.URL, oldBookURL).Scan(&conflict)
	if conflict > 0 {
		return false
	}
	var payload, reading string
	_ = s.storage.QueryRow("SELECT payload, reading FROM bookshelf WHERE book_url = ?",
		oldBookURL).Scan(&payload, &reading)
	reading = resetReadingChapter(reading)
	// payload 同步改写（对齐 flet switch_bookshelf_source 的 record.update(identity)
	// + save_bookshelf）：读取面（List）只看 payload，列改了 payload 不改 = 换源白换，
	// 旧书名/旧地址照旧上屏。
	record := map[string]interface{}{}
	if json.Unmarshal([]byte(payload), &record) != nil {
		record = map[string]interface{}{}
	}
	variables := book.Variables
	if variables == nil {
		variables = map[string]interface{}{}
	}
	identity := map[string]interface{}{
		"name": book.Name, "author": book.Author,
		"intro": book.Intro, "url": book.URL,
		"cover_url": book.CoverURL,
		"toc_url":   book.TocURL,
		"kind":      book.Kind,
		"last_chapter": book.LastChapter,
		"word_count":   book.WordCount,
		"download_urls": book.DownloadURLs,
		"variables":     variables,
		"source_url":    source.BookSourceURL,
		"source_name":   source.BookSourceName,
		"bookUrl":       book.URL,
		"tocUrl":        book.TocURL,
		"origin":        source.BookSourceURL,
		"originName":    source.BookSourceName,
		"latestChapterTitle": book.LastChapter,
		"lastCheckTime":      time.Now().UnixMilli(),
		"lastCheckCount":     0,
		"latestChapterTime":  0,
		"totalChapterNum":    0,
		"updateError":        "",
		"cachedChapterCount": 0, "cacheTotalChapterCount": 0,
		"cacheErrorCount": 0, "cacheUpdatedAt": 0,
	}
	for key, value := range identity {
		record[key] = value
	}
	// 章节 url 属于旧源：payload 里那份 reading 照列上的复位口径同改；列上那份
	// 若解析得出，镜像回 payload（两处保持同一个真源）。
	if inner, ok := record["reading"].(map[string]interface{}); ok {
		inner["chapter_url"] = ""
		inner["scroll_offset"] = 0.0
		inner["scroll_ratio"] = 0.0
	}
	var decodedReading map[string]interface{}
	if reading != "" {
		_ = json.Unmarshal([]byte(reading), &decodedReading)
	}
	if len(decodedReading) > 0 {
		record["reading"] = decodedReading
	}
	patched, patchErr := json.Marshal(record)
	if patchErr != nil {
		return false
	}
	varsJSON, _ := json.Marshal(variables)
	_, err := s.storage.Execute(
		`UPDATE bookshelf SET book_url = ?, name = ?, author = ?, kind = ?, cover_url = ?,
		 source_url = ?, reading = ?, vars = ?, payload = ?, updated_at = ? WHERE book_url = ?`,
		book.URL, book.Name, book.Author, book.Kind, book.CoverURL,
		source.BookSourceURL, reading, string(varsJSON), string(patched),
		time.Now().UnixMilli(), oldBookURL)
	if err != nil {
		return false
	}
	s.bus.Publish(&core.SourceChangedEvent{
		ChangeType: "switch_source",
		SourceID:   source.BookSourceURL,
		Timestamp:  core.NowSeconds(),
	})
	return true
}

// resetReadingChapter 清掉绑在旧源上的章节定位（对齐 Python：chapter_url="",
// scroll_offset=0, scroll_ratio=0），其余进度字段原样保留。
func resetReadingChapter(reading string) string {
	if reading == "" {
		return reading
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(reading), &parsed); err != nil {
		return reading
	}
	parsed["chapter_url"] = ""
	parsed["scroll_offset"] = 0.0
	parsed["scroll_ratio"] = 0.0
	reset, err := json.Marshal(parsed)
	if err != nil {
		return reading
	}
	return string(reset)
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

// SaveReading 落一份书架进度与规则变量（对齐 flet update_bookshelf_state 的落盘面）。
// 读取面（List）只看 payload，所以这里经 patchShelfReading 把两处一起写——只改列
// 等于这个动作静默无效果。
func (s *BookshelfService) SaveReading(bookURL string, reading interface{}, vars interface{}) error {
	if bookURL == "" {
		return nil
	}
	var decoded map[string]interface{}
	if reading != nil {
		if raw, err := json.Marshal(reading); err == nil {
			_ = json.Unmarshal(raw, &decoded)
		}
	}
	s.patchShelfReading(bookURL, decoded, vars)
	return nil
}

// ToMap converts a BookItem to a map representation.
func ToMap(b models.BookItem) map[string]interface{} {
	return map[string]interface{}{"url": b.BookURL, "name": b.Name, "author": b.Author}
}

// ---------------------------------------------------------------------------
// 书架记录（34 键形状，对齐 Python add_to_bookshelf / find_bookshelf_item）
//
// 整份记录存 `payload` JSON，活列（name/author/kind/cover_url/source_url/
// in_bookshelf）同步一份给 SQL 过滤用；读的时候 `in_bookshelf` 以**列**为准
// （转正/丢掉只改列与 payload 的同一格，不改其他字段）。
// ---------------------------------------------------------------------------

// ShelfRecordDict 建一条 34 键书架记录（对齐 Python add_to_bookshelf 的字典）。
//
// `shelved=false` 即 Legado notShelf 试读暂存：行在库里、`bookshelf.list` 过滤掉。
// 键名两套拼法并存（snake + Legado camel 别名）是服务端历史形状，wire 上照抄。
func ShelfRecordDict(sourceURL, sourceName string, book *internalmodels.BookItem,
	shelved bool) map[string]interface{} {
	if book == nil {
		return map[string]interface{}{}
	}
	var downloadURLs interface{}
	if book.DownloadURLs != nil {
		downloadURLs = book.DownloadURLs
	}
	var variables interface{} = map[string]interface{}{}
	if book.Variables != nil {
		variables = book.Variables
	}
	return map[string]interface{}{
		"in_bookshelf":          shelved,
		"name":                  book.Name,
		"author":                book.Author,
		"intro":                 book.Intro,
		"url":                   book.URL,
		"cover_url":             book.CoverURL,
		"toc_url":               book.TocURL,
		"kind":                  book.Kind,
		"last_chapter":          book.LastChapter,
		"word_count":            book.WordCount,
		"download_urls":         downloadURLs,
		"variables":             variables,
		"source_url":            sourceURL,
		"source_name":           sourceName,
		"bookUrl":               book.URL,
		"tocUrl":                book.TocURL,
		"origin":                sourceURL,
		"originName":            sourceName,
		"latestChapterTitle":    book.LastChapter,
		"totalChapterNum":       0,
		"lastCheckTime":         time.Now().UnixMilli(),
		"lastCheckCount":        0,
		"durChapterTitle":       nil,
		"durChapterIndex":       0,
		"durChapterPos":         0,
		"durChapterTime":        0,
		"canUpdate":             true,
		"order":                 0,
		"group":                 0,
		"groupName":             "",
		"latestChapterTime":     0,
		"cachedChapterCount":    0,
		"cacheTotalChapterCount": 0,
		"cacheErrorCount":       0,
	}
}

// CreateShelfRecord 建一条书架记录；同键已存在返回 false（不覆盖进度/变量）。
func (s *BookshelfService) CreateShelfRecord(sourceURL, sourceName string,
	book *internalmodels.BookItem, shelved bool) bool {
	if s == nil || s.storage == nil || book == nil || book.URL == "" {
		return false
	}
	var existing string
	if err := s.storage.QueryRow("SELECT book_url FROM bookshelf WHERE book_url = ?",
		book.URL).Scan(&existing); err == nil {
		return false
	}
	record := ShelfRecordDict(sourceURL, sourceName, book, shelved)
	payload, err := json.Marshal(record)
	if err != nil {
		return false
	}
	vars, _ := json.Marshal(record["variables"])
	inShelf := 0
	if shelved {
		inShelf = 1
	}
	if _, err := s.storage.Execute(
		`INSERT INTO bookshelf (book_url, source_url, name, author, kind, cover_url,
		 in_bookshelf, reading, vars, payload, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)`,
		book.URL, sourceURL, book.Name, book.Author, book.Kind, book.CoverURL,
		inShelf, string(vars), string(payload), time.Now().UnixMilli(),
	); err != nil {
		return false
	}
	changeType := "staged"
	if shelved {
		changeType = "shelved"
	}
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: changeType, SourceID: book.URL, Timestamp: core.NowSeconds()})
	return true
}

// FindShelfRecord 取一条活记录（payload 解析 + in_bookshelf 列覆盖）。
// 第二条返回值 = 有没有这条记录。source_url 与列上不一致视为另一条源的记录。
func (s *BookshelfService) FindShelfRecord(sourceURL, bookURL string) (map[string]interface{}, bool) {
	if s == nil || s.storage == nil || bookURL == "" {
		return nil, false
	}
	var payload, rowSource string
	var inShelf int
	if err := s.storage.QueryRow(
		"SELECT payload, source_url, in_bookshelf FROM bookshelf WHERE book_url = ?",
		bookURL).Scan(&payload, &rowSource, &inShelf); err != nil {
		return nil, false
	}
	if sourceURL != "" && rowSource != "" && rowSource != sourceURL {
		return nil, false
	}
	record := map[string]interface{}{}
	if err := json.Unmarshal([]byte(payload), &record); err != nil {
		record = map[string]interface{}{}
	}
	record["url"] = bookURL
	record["bookUrl"] = bookURL
	record["source_url"] = rowSource
	if _, ok := record["origin"]; !ok {
		record["origin"] = rowSource
	}
	record["in_bookshelf"] = inShelf != 0
	return record, true
}

// PromoteShelfRecord 暂存转正（对齐 Python promote_to_bookshelf）：原地改
// in_bookshelf，进度/变量/目录缓存一律不动；在架或没这条记录都回 false。
func (s *BookshelfService) PromoteShelfRecord(sourceURL, bookURL string) bool {
	if s == nil || s.storage == nil || bookURL == "" {
		return false
	}
	args := []interface{}{time.Now().UnixMilli(), bookURL}
	where := "book_url = ? AND in_bookshelf = 0"
	if sourceURL != "" {
		where += " AND source_url = ?"
		args = append(args, sourceURL)
	}
	result, err := s.storage.Execute(
		"UPDATE bookshelf SET in_bookshelf = 1, updated_at = ? WHERE "+where, args...)
	if err != nil {
		return false
	}
	if affected, aerr := result.RowsAffected(); aerr != nil || affected == 0 {
		return false
	}
	s.patchPayloadField(bookURL, "in_bookshelf", true)
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "shelved", SourceID: bookURL, Timestamp: core.NowSeconds()})
	return true
}

// DiscardShelfRecord 丢掉一条记录（对齐 Python remove_from_bookshelf）。
// 进度行在 `books` 表、不随这一行消失，调用方（试读放弃）负责一并清。
func (s *BookshelfService) DiscardShelfRecord(sourceURL, bookURL string) bool {
	if s == nil || s.storage == nil || bookURL == "" {
		return false
	}
	args := []interface{}{bookURL}
	where := "book_url = ?"
	if sourceURL != "" {
		where += " AND source_url = ?"
		args = append(args, sourceURL)
	}
	result, err := s.storage.Execute("DELETE FROM bookshelf WHERE "+where, args...)
	if err != nil {
		return false
	}
	if affected, aerr := result.RowsAffected(); aerr != nil || affected == 0 {
		return false
	}
	s.bus.Publish(&core.SourceChangedEvent{ChangeType: "unshelved", SourceID: bookURL, Timestamp: core.NowSeconds()})
	return true
}

// patchPayloadField 改 payload 里的一个键（列与 payload 的同一格保持同值）。
func (s *BookshelfService) patchPayloadField(bookURL, key string, value interface{}) {
	var payload string
	if err := s.storage.QueryRow("SELECT payload FROM bookshelf WHERE book_url = ?",
		bookURL).Scan(&payload); err != nil {
		return
	}
	var record map[string]interface{}
	if err := json.Unmarshal([]byte(payload), &record); err != nil {
		return
	}
	record[key] = value
	patched, err := json.Marshal(record)
	if err != nil {
		return
	}
	_, _ = s.storage.Execute("UPDATE bookshelf SET payload = ? WHERE book_url = ?",
		string(patched), bookURL)
}
