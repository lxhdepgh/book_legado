package services

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/legodo-booksource/legodo_go/core"
)

// CachedChapterDoc 是"已缓存的某一章"的段列表——建索引的原料，由阅读服务
// 从会话缓存里挑出来（对齐 Python rebuild_index 的缓存遍历结果）。
type CachedChapterDoc struct {
	ChapterIndex int
	Paragraphs   []string
}

const (
	ftsTable           = "content_search"
	ftsParagraphColumn = "para_index"
	ftsHitLimit        = 200
)

// ContentSearchService 对已缓存章节建 FTS5 索引并按关键词检索
// （对齐 Python app/services/reading/content_search.py）。
//
// 一行 = 一个自然段，不是引擎给的一个 text block：正文整章常常只有一两个
// <p>，按块建行等于"章级偏移"，命中段落定位不到。切段与段首偏移都取自
// text_layout 那把尺，命中跳转才与阅读进度落在同一个坐标系。
//
// 索引只进主库（无散落文件），文本来源是阅读会话已缓存的章节；未缓存章节
// 不建索引。虚表不进版本化迁移——虚表随索引重建（派生数据）。
type ContentSearchService struct {
	base         *core.BaseService
	storage      *core.StorageService
	bus          *core.EventBus
	ftsAvailable bool
	ensureOnce   sync.Once
}

// NewContentSearchService creates a new content search service.
func NewContentSearchService(storage *core.StorageService, bus *core.EventBus) *ContentSearchService {
	return &ContentSearchService{
		base:         core.NewBaseService("ContentSearchService"),
		storage:      storage,
		bus:          bus,
		ftsAvailable: true,
	}
}

// Start 初始化服务。FTS5 不可用只是降级（检索回空结果并报 ftsAvailable=false），
// 不是启动失败——不能为它把整个服务树拦下。
func (s *ContentSearchService) Start() error {
	s.base.SetOnStart(func() error {
		s.ensureOnce.Do(s.ensureFTSTable)
		return nil
	})
	return s.base.Start()
}
func (s *ContentSearchService) Stop() error { return s.base.Stop() }

// available 这台机器能不能用这一发（没存储 / 没编 FTS5 都算不能）。
func (s *ContentSearchService) available() bool {
	return s.storage != nil && s.ftsAvailable
}

// ensureFTSTable 按需建虚表（幂等）：先探一眼表在哪一版，是段落级（有 para_index）
// 就原样用；是旧块级表就整表拆掉重建（索引是派生数据，下一次书内搜索按需重建）。
func (s *ContentSearchService) ensureFTSTable() {
	if s.storage == nil {
		s.ftsAvailable = false
		return
	}
	if s.hasParagraphColumn() {
		return
	}
	_, err := s.storage.Execute(
		"CREATE VIRTUAL TABLE IF NOT EXISTS " + ftsTable + " USING fts5(" +
			"book_url UNINDEXED, chapter_index UNINDEXED, offset UNINDEXED, " +
			ftsParagraphColumn + " UNINDEXED, text, tokenize='trigram')")
	if err != nil {
		// 该 sqlite 未编译 FTS5：检索降级为空结果，ftsAvailable 如实报 false。
		s.ftsAvailable = false
		log.Printf("[ContentSearch] FTS5 不可用，书内全文检索降级为空结果：%v", err)
	}
}

// hasParagraphColumn 表已经在且是段落级则 true；旧块级表（缺 para_index 列）
// 当场拆掉。先探再建：虚表已在时 CREATE 会抛"no such column: rowid"那条真错，
// 不探就会把它认成"这台 sqlite 没有 FTS5"，把功能整本降级掉。
func (s *ContentSearchService) hasParagraphColumn() bool {
	rows, err := s.storage.Query(fmt.Sprintf("PRAGMA table_info(%s)", ftsTable))
	if err != nil {
		return false
	}
	columns := make(map[string]bool)
	for rows.Next() {
		var cid, notNull, pk int
		var name, columnType string
		var defaultValue interface{}
		if scanErr := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &pk); scanErr != nil {
			continue
		}
		columns[name] = true
	}
	rows.Close() // 拆表要拿写口：单连接下必须先关掉这条读
	if len(columns) == 0 { // 表还不存在
		return false
	}
	if columns[ftsParagraphColumn] {
		return true
	}
	_, _ = s.storage.Execute("DROP TABLE " + ftsTable)
	return false
}

// SearchEnsured 是书内搜索的一发面：先按需补索引，再查，并回"为什么可能是 0 条"。
// 把这两步拆给客户端是留陷阱：索引还没建时查回空列表，跨进程那侧看起来与
// "这本书里没有这个词"一模一样。所以把 indexedChapters（索引覆盖了几章）与
// ftsAvailable（这台机器有没有 FTS5）一起报回去，界面据此说"只搜了已缓存的 N 章"。
//
// docs 即"这本书已缓存的章节"（阅读服务从会话缓存里挑出），索引只覆盖它们。
func (s *ContentSearchService) SearchEnsured(bookURL string, docs []CachedChapterDoc, keyword string) map[string]interface{} {
	s.ensureOnce.Do(s.ensureFTSTable)
	bookURL = strings.TrimSpace(bookURL)
	written := s.ensureIndex(bookURL, docs)
	hits := s.search(bookURL, keyword)
	return map[string]interface{}{
		"hits":            hits,
		"written":         written,
		"indexedChapters": s.indexedChapters(bookURL),
		"ftsAvailable":    s.available(),
	}
}

// ensureIndex 缓存里已有的章节都得在索引里；缺了才重建，返回写入行数。
// 判据只有一条：已缓存的章节数 > 索引覆盖的章节数。反向不一致（缓存被清理掉、
// 索引还留着）不触发重建——留着的那几条命中跳过去只是重新取一次正文，
// 而先删后写的重建会把整本索引清成 0 行。
func (s *ContentSearchService) ensureIndex(bookURL string, docs []CachedChapterDoc) int {
	if !s.available() || bookURL == "" {
		return 0
	}
	if len(docs) <= s.indexedChapters(bookURL) {
		return 0
	}
	return s.rebuildIndex(bookURL, docs)
}

// rebuildIndex 对已缓存章节重建索引，写入行数（docs 里没有的章节天然跳过）。
// 清旧索引与写新索引是一笔事务：中途失败不会留下"半本书"的索引。
func (s *ContentSearchService) rebuildIndex(bookURL string, docs []CachedChapterDoc) int {
	rows := make([][]interface{}, 0, 64)
	for _, doc := range docs {
		// 段序号与段首偏移都取自位置契约那把尺（ParagraphOffsets）：命中跳转
		// 要把 para_index/offset 交回阅读器定位，而它落进度用的是同一把尺导出的
		// 字符偏移。自己数一遍（逐段累加 len）每段少算一个换行格，跳过去就偏一段。
		offsets := ParagraphOffsets(doc.Paragraphs)
		for index, text := range doc.Paragraphs {
			rows = append(rows, []interface{}{
				bookURL, doc.ChapterIndex, float64(offsets[index]), index, text,
			})
		}
	}
	err := s.storage.Transaction(func(tx *sql.Tx) error {
		if _, execErr := tx.Exec("DELETE FROM "+ftsTable+" WHERE book_url=?", bookURL); execErr != nil {
			return execErr
		}
		for _, row := range rows {
			if _, execErr := tx.Exec(
				"INSERT INTO "+ftsTable+"(book_url, chapter_index, offset, para_index, text) VALUES(?,?,?,?,?)",
				row...); execErr != nil {
				return execErr
			}
		}
		return nil
	})
	if err != nil {
		log.Printf("[ContentSearch] 重建索引失败：%v", err)
		return 0
	}
	return len(rows)
}

// search 检索某书：命中回 chapter_index / para_index / 段首偏移 / 摘要（<mark> 高亮）。
func (s *ContentSearchService) search(bookURL, keyword string) []map[string]interface{} {
	hits := make([]map[string]interface{}, 0)
	keyword = strings.TrimSpace(keyword)
	if !s.available() || bookURL == "" || keyword == "" {
		return hits
	}

	columns := "chapter_index, para_index, offset, snippet(" + ftsTable + ", 4, '<mark>', '</mark>', '…', 16)"
	var (
		rows *sql.Rows
		err  error
	)
	if utf8.RuneCountInString(keyword) < 3 {
		// trigram 分词器对 >=3 字符的子串匹配良好；更短的查询（常见于中文双字词）
		// FTS MATCH 无法命中，退化为 LIKE 扫描以保证可用。
		rows, err = s.storage.Query(
			"SELECT "+columns+" FROM "+ftsTable+
				" WHERE book_url=? AND text LIKE ? ORDER BY chapter_index, offset LIMIT ?",
			bookURL, "%"+keyword+"%", ftsHitLimit)
	} else {
		phrase := `"` + strings.ReplaceAll(keyword, `"`, " ") + `"`
		rows, err = s.storage.Query(
			"SELECT "+columns+" FROM "+ftsTable+
				" WHERE book_url=? AND "+ftsTable+" MATCH ? ORDER BY chapter_index, offset LIMIT ?",
			bookURL, phrase, ftsHitLimit)
	}
	if err != nil {
		return hits
	}
	defer rows.Close()

	for rows.Next() {
		// 列从 driver 回来的形状随列而变（整数可能回 int64 或字符串），统一走
		// interface{} 再按值转——按类型硬扫会在"这台机器恰好回了另一种形状"时抛错。
		var chapterIndex, paraIndex, offset, snippet interface{}
		if scanErr := rows.Scan(&chapterIndex, &paraIndex, &offset, &snippet); scanErr != nil {
			continue
		}
		chapter, _ := intOrNone(chapterIndex)
		paragraph, _ := intOrNone(paraIndex)
		text, _ := snippet.(string)
		hits = append(hits, map[string]interface{}{
			"chapter_index": chapter,
			"para_index":    paragraph,
			"offset":        floatOrZero(offset),
			"snippet":       text,
		})
	}
	return hits
}

// indexedChapters 索引覆盖了几章（COUNT DISTINCT chapter_index）。
func (s *ContentSearchService) indexedChapters(bookURL string) int {
	if !s.available() || bookURL == "" {
		return 0
	}
	row := s.storage.QueryRow(
		"SELECT COUNT(DISTINCT chapter_index) FROM "+ftsTable+" WHERE book_url=?", bookURL)
	var count int
	if err := row.Scan(&count); err != nil {
		return 0
	}
	return count
}
