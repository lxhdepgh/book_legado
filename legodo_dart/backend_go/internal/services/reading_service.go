package services

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// ReadingSession represents an active book reading session.
//
// 会话是引擎里的单例；reader 面每一发都按 (source_url, book_url) 显式核对，
// 不靠"上一次调用留了什么"猜（对齐 Python ReadingService._session_key）。
type ReadingSession struct {
	SourceURL   string `json:"sourceUrl"`
	SourceName  string `json:"sourceName"`
	Book        *models.BookItem `json:"book"`
	BookURL     string `json:"bookUrl"`
	Name        string `json:"name"`
	Author      string `json:"author"`
	TocURL      string `json:"tocUrl"`
	LastChapter int    `json:"lastChapter"`
	ReadTime    time.Time `json:"readTime"`
	// Position 会话当前落点：position_save 落库前会写回这里，close 落的正是它。
	Position    ReadingPosition   `json:"-"`
	CachedChaps []*models.Chapter `json:"-"`
}

// chapterLoaderFunc 载目录的可注入缝（测试用替身，生产走规则引擎）。
type chapterLoaderFunc func(source *models.BookSource, book *models.BookItem) ([]*models.Chapter, error)

// ReadingService manages reading sessions, chapter prefetching, and progress persistence.
type ReadingService struct {
	base       *core.BaseService
	hub        *ServiceHub
	mu         sync.RWMutex
	session    *ReadingSession
	cache      map[string]*models.ChapterContent // chapter URL -> content
	prefetchWg sync.WaitGroup

	// chapterLoader 为 nil 时走规则引擎载目录（先书面 info 取 toc_url 再取目录）。
	chapterLoader chapterLoaderFunc

	searchMu    sync.Mutex
	searchStore *SearchBookStore
	searchModel *SearchModel

	// 换源扫描（P3）：一轮的服务端状态；三个参数可注入（测试收窄并发/超时/节流）。
	switchMu            sync.Mutex
	switchRound         *switchRound
	switchWorkerCount   int
	switchSourceTimeout time.Duration
	switchPublishEvery  time.Duration
	switchSearchOne     SearchOneFunc
}

// NewReadingService creates a new reading service.
func NewReadingService(hub *ServiceHub) *ReadingService {
	s := &ReadingService{
		base:  core.NewBaseService("ReadingService", "SourceService", "RuleEngineService"),
		hub:   hub,
		cache: make(map[string]*models.ChapterContent),
	}
	return s
}

// Start initializes the service.
func (s *ReadingService) Start() error { return s.base.Start() }
func (s *ReadingService) Stop() error  { return s.base.Stop() }

// Close releases the current reading session.
func (s *ReadingService) Close() {
	s.mu.Lock()
	s.cache = make(map[string]*models.ChapterContent)
	s.session = nil
	s.mu.Unlock()
	s.prefetchWg.Wait()
}

// GetSession returns the current reading session (nil if none open).
func (s *ReadingService) GetSession() *ReadingSession {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.session
}

// ---------------------------------------------------------------------------
// 阅读面（对齐 Python reading_service.open_by_url / close_by_url / position /
// staged / chapter_document / search_ensured 那一族）
//
// 共同点：**身份全部按 url 显式传进来**。会话态是进程内单例，跨进程那侧看不见
// 它，也不该靠"上一次调用留了什么"来猜这一章属于哪本书。
// ---------------------------------------------------------------------------

// sessionMatches 当前会话是不是这一本书（book_url 必须对上；source_url 双方
// 都非空时才比）—— 对齐 Python _session_matches。
func (s *ReadingService) sessionMatches(sourceURL, bookURL string) bool {
	sess := s.GetSession()
	if sess == nil {
		return false
	}
	if bookURL != sess.BookURL {
		return false
	}
	return sess.SourceURL == "" || sourceURL == "" || sourceURL == sess.SourceURL
}

// readerSessionError 会话不匹配时的协议面错误（对齐 Python _require_session 文案：
// 带上当前会话键，排障时一眼看出关/存的是哪本书）。
func (s *ReadingService) readerSessionError(sourceURL, bookURL string) error {
	currentSource, currentBook := "", ""
	if sess := s.GetSession(); sess != nil {
		currentSource, currentBook = sess.SourceURL, sess.BookURL
	}
	return fmt.Errorf("ReaderSessionError: 当前会话不是这本书，先调 reader.open：source='%s' book='%s'（会话=('%s', '%s')）",
		sourceURL, bookURL, currentSource, currentBook)
}

// TocByURL 目录行（对齐 Python chapter_rows）：会话不是这本书就报错而不是回空
// 目录（回空目录看起来像书坏了）。refresh=true 忽略缓存重取并顶掉会话里那一份
// ——刷新时磁盘目录也跳过（force_refresh 语义），成功取回后照常写回磁盘。
func (s *ReadingService) TocByURL(sourceURL, bookURL string, refresh bool) ([]*models.Chapter, error) {
	if !s.sessionMatches(sourceURL, bookURL) {
		return nil, s.readerSessionError(sourceURL, bookURL)
	}
	sess := s.GetSession()
	if !refresh {
		return sess.CachedChaps, nil
	}
	var source *models.BookSource
	if srcSvc := s.hub.SourceService(); srcSvc != nil {
		lookup := sourceURL
		if lookup == "" {
			lookup = sess.SourceURL
		}
		source, _ = srcSvc.Get(lookup)
	}
	if source == nil {
		return nil, fmt.Errorf("书源不存在：%s", sourceURL)
	}
	chapters, err := s.loadChapters(source, sess.Book, refresh)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	if s.session != nil && s.session.BookURL == bookURL {
		s.session.CachedChaps = chapters
	}
	s.mu.Unlock()
	return chapters, nil
}

// readerCache 当前存储的读者磁盘缓存（没有存储就是 nil——测试替身场景）。
// 根目录取 StorageService.CacheDir()，reader-cache 那层由 NewReadingCache 补。
func (s *ReadingService) readerCache() *ReadingCache {
	storage := s.hub.StorageService()
	if storage == nil {
		return nil
	}
	return NewReadingCache(storage.CacheDir())
}

// loadChapters 载目录（对齐 Python load_chapters）：磁盘缓存优先（refresh 跳过
// 读、成功仍写回）> 测试注入的 chapterLoader > 规则引擎。缓存与新取的目录是
// 同一张盘上的同一份形状——flet 时代落的目录 Dart 侧这就直接能看了。
func (s *ReadingService) loadChapters(source *models.BookSource, book *models.BookItem, refresh bool) ([]*models.Chapter, error) {
	if book == nil || book.URL == "" {
		return nil, fmt.Errorf("missing book url")
	}
	cache := s.readerCache()
	sourceURL := ""
	if source != nil {
		sourceURL = source.BookSourceURL
	}
	if cache != nil && !refresh {
		if chapters, ok := cache.LoadCatalog(sourceURL, book.URL); ok && len(chapters) > 0 {
			return chapters, nil
		}
	}
	var chapters []*models.Chapter
	var err error
	if s.chapterLoader != nil {
		chapters, err = s.chapterLoader(source, book)
	} else {
		chapters, err = s.fetchChaptersFromEngine(source, book)
	}
	if err != nil {
		return nil, err
	}
	if cache != nil && len(chapters) > 0 {
		cache.SaveCatalog(sourceURL, book.URL, chapters)
	}
	return chapters, nil
}

// fetchChaptersFromEngine 走规则引擎取目录（对齐 Python chapters 的详情面）：
// 书没带 toc_url 就先取详情页信息——GetBookInfo 在 tocUrl 缺失时回落详情页
// url，之后把 (tocURL, bookURL) 交给目录管线（空目录回详情页一次、还空报错
// 都在管线里）。
func (s *ReadingService) fetchChaptersFromEngine(source *models.BookSource, book *models.BookItem) ([]*models.Chapter, error) {
	if s.hub.RuleEngine() == nil {
		return nil, fmt.Errorf("rule engine unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	tocURL := book.TocURL
	if tocURL == "" {
		info, err := s.hub.RuleEngine().GetBookInfo(ctx, source, book.URL)
		if err != nil {
			return nil, fmt.Errorf("get book info: %w", err)
		}
		tocURL = info.TocURL
	}
	return s.hub.RuleEngine().FetchChapterList(ctx, source, tocURL, book.URL)
}

// OpenByURL 开会话的 JSON 面（对齐 Python open_by_url）：载目录、恢复进度、
// 按需入书架/试读暂存，回会话快照 **+ 位置**（目录不在这一发里）。
func (s *ReadingService) OpenByURL(sourceURL string, book *models.BookItem,
	shelve, resume bool) (map[string]interface{}, error) {
	if book == nil || book.URL == "" {
		return nil, fmt.Errorf("missing book url")
	}
	var source *models.BookSource
	if srcSvc := s.hub.SourceService(); srcSvc != nil {
		if sourceURL != "" {
			source, _ = srcSvc.Get(sourceURL)
		}
		if source == nil && book.Origin != "" {
			// 书架点开只有记录、没有源：按记录的 origin 反查书源（对齐 Python open_book）。
			if reversed, ok := srcSvc.Get(book.Origin); ok && reversed != nil {
				source = reversed
				sourceURL = book.Origin
			}
		}
	}
	if source == nil {
		return nil, fmt.Errorf("书源不存在：%s", sourceURL)
	}

	chapters, err := s.loadChapters(source, book, false)
	if err != nil {
		return nil, err
	}

	position := DefaultReadingPosition()
	if resume {
		if restored := s.LoadProgress(book.URL); restored != nil {
			position = *restored
		}
	}

	// notShelf 语义：shelved=false 的试读暂存与在架记录同形，列表过滤掉。
	// 同键已存在不覆盖（对齐 add_to_bookshelf 返回 False 的那一支）。
	if shelfSvc := s.hub.BookshelfService(); shelfSvc != nil {
		shelfSvc.CreateShelfRecord(sourceURL, source.BookSourceName, book, shelve)
	}

	s.mu.Lock()
	s.session = &ReadingSession{
		SourceURL:   sourceURL,
		SourceName:  source.BookSourceName,
		Book:        book,
		BookURL:     book.URL,
		Name:        book.Name,
		Author:      book.Author,
		TocURL:      book.TocURL,
		LastChapter: position.ChapterIndex,
		ReadTime:    time.Now(),
		Position:    position,
		CachedChaps: chapters,
	}
	s.cache = make(map[string]*models.ChapterContent)
	s.mu.Unlock()

	if bus := s.hub.EventBus(); bus != nil {
		bus.Publish(&core.ReadingSessionChangedEvent{
			BookURL:      book.URL,
			ChapterIndex: position.ChapterIndex,
		})
	}

	return map[string]interface{}{
		"book":         BookSnapshot(book),
		"sourceUrl":    sourceURL,
		"bookUrl":      book.URL,
		"chapterCount": len(chapters),
		"position":     position.asReading(),
	}, nil
}

// CloseByURL 结束会话（对齐 Python close_by_url）：url 对不上就什么都不关
// （关错书不能把 A 的进度落到 B 那一行）。staged 在关**之前**取——关完就找不到
// 那条记录了。回 {closed, staged}。
func (s *ReadingService) CloseByURL(sourceURL, bookURL string, saveProgress bool) map[string]interface{} {
	if !s.sessionMatches(sourceURL, bookURL) {
		return map[string]interface{}{"closed": false, "staged": nil}
	}
	staged, hasStaged := s.StagedRecordBySession()
	var stagedOut interface{}
	if hasStaged {
		stagedOut = staged
	}
	if saveProgress {
		sess := s.GetSession()
		if sess != nil {
			s.saveProgress(sess.Position)
		}
	}
	s.mu.Lock()
	s.session = nil
	s.cache = make(map[string]*models.ChapterContent)
	s.mu.Unlock()
	return map[string]interface{}{"closed": true, "staged": stagedOut}
}

// StagedRecordBySession 当前书若是试读暂存给出那条书架记录（对齐 Python
// staged_record）；在架的书、没记录、没会话都回 false——在架的书退出时不该被问。
func (s *ReadingService) StagedRecordBySession() (map[string]interface{}, bool) {
	sess := s.GetSession()
	if sess == nil || sess.BookURL == "" {
		return nil, false
	}
	shelfSvc := s.hub.BookshelfService()
	if shelfSvc == nil {
		return nil, false
	}
	record, ok := shelfSvc.FindShelfRecord(sess.SourceURL, sess.BookURL)
	if !ok {
		return nil, false
	}
	if inShelf, _ := record["in_bookshelf"].(bool); inShelf {
		return nil, false
	}
	return record, true
}

// PromoteStaged 试读转正：暂存记录原地变书架记录，进度/变量/目录缓存一律不动。
func (s *ReadingService) PromoteStaged() bool {
	sess := s.GetSession()
	if sess == nil || sess.BookURL == "" {
		return false
	}
	shelfSvc := s.hub.BookshelfService()
	if shelfSvc == nil {
		return false
	}
	return shelfSvc.PromoteShelfRecord(sess.SourceURL, sess.BookURL)
}

// DiscardStaged 丢弃试读：记录、进度与缓存一起清掉（没入架的书不该留下痕迹）。
// 进度落在 books 表、不随书架记录消失——只删书架行等于删了个寂寞。
func (s *ReadingService) DiscardStaged() bool {
	sess := s.GetSession()
	if sess == nil || sess.BookURL == "" {
		return false
	}
	removed := false
	if shelfSvc := s.hub.BookshelfService(); shelfSvc != nil {
		removed = shelfSvc.DiscardShelfRecord(sess.SourceURL, sess.BookURL)
	}
	if storage := s.hub.StorageService(); storage != nil {
		_, _ = storage.Execute("DELETE FROM books WHERE book_url = ?", sess.BookURL)
	}
	s.mu.Lock()
	s.cache = make(map[string]*models.ChapterContent)
	s.mu.Unlock()
	return removed
}

// ReadPositionByURL 续读位置的 JSON 面（对齐 Python read_position）：没有记录
// 回 false（不是"位置为 0"），有则回**存档形状**（chapter_url/name 恒空，
// 续读那一章由 chapter_index 在目录里定位）。
func (s *ReadingService) ReadPositionByURL(sourceURL, bookURL string) (map[string]interface{}, bool) {
	position := s.LoadProgress(bookURL)
	if position == nil {
		return nil, false
	}
	return position.archiveReading(), true
}

// SavePositionByURL 落一篇阅读位置（对齐 Python save_position）：
// 章按 index 认（chapter_url 从目录重新取）、按章内字符流收口偏移与段号、
// 落库并把落库的那一份写回会话（close 落的正是会话上那份），回落库形状。
func (s *ReadingService) SavePositionByURL(sourceURL, bookURL string,
	incoming map[string]interface{}) (map[string]interface{}, error) {
	if !s.sessionMatches(sourceURL, bookURL) {
		return nil, s.readerSessionError(sourceURL, bookURL)
	}
	position := readingPositionFromDict(incoming)
	position = s.anchorPosition(position)
	position = s.clampPosition(position)
	s.saveProgress(position)
	s.mu.Lock()
	if s.session != nil {
		s.session.Position = position
		s.session.LastChapter = position.ChapterIndex
	}
	s.mu.Unlock()
	return position.asReading(), nil
}

// anchorPosition chapter_url 按 chapter_index 从目录里重新取；目录里没有这个
// index 就不动（对齐 Python _anchored）。
func (s *ReadingService) anchorPosition(position ReadingPosition) ReadingPosition {
	sess := s.GetSession()
	if sess == nil || len(sess.CachedChaps) == 0 {
		return position
	}
	index := position.ChapterIndex
	if index < 0 || index >= len(sess.CachedChaps) {
		return position
	}
	chapter := sess.CachedChaps[index]
	if chapter == nil || chapter.URL == position.ChapterURL {
		return position
	}
	position.ChapterURL = chapter.URL
	position.ChapterName = chapter.Title
	return position
}

// clampPosition 偏移与段号按本章可显示自然段收口；这一章不在缓存里时**不动值**
// （宁可存一个未核过的偏移，也不要为核它去联网拉一整章）—— 对齐 Python _clamped。
func (s *ReadingService) clampPosition(position ReadingPosition) ReadingPosition {
	document := s.cachedDocument(position.ChapterURL)
	if document == nil {
		return position
	}
	paragraphs := ChapterParagraphs(contentTexts(document))
	total := ChapterTextLength(paragraphs)
	if position.ParagraphIndex != nil {
		index := *position.ParagraphIndex
		if index < 0 {
			index = 0
		}
		if last := len(paragraphs) - 1; last < 0 {
			index = 0
		} else if index > last {
			index = last
		}
		position.ParagraphIndex = &index
	}
	if position.CharacterOffset < 0 {
		position.CharacterOffset = 0
	}
	if position.CharacterOffset > total {
		position.CharacterOffset = total
	}
	return position
}

// cachedDocument 会话内容缓存里的一章（进程内；预取窗口只有几章很正常）。
func (s *ReadingService) cachedDocument(chapterURL string) *models.ChapterContent {
	if chapterURL == "" {
		return nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.cache[chapterURL]
}

// loadDiskContent 正文取不到内存副本时看盘：flet 时代预渲染落的 reader-cache
// 里就躺着这些章（Python 落盘布局，键公式一致）。命中回填内存缓存，上层按
// 既有路径出 blocks；miss 回 nil 让调用方走联网。
func (s *ReadingService) loadDiskContent(sourceURL, bookURL, chapterURL string) *models.ChapterContent {
	cache := s.readerCache()
	if cache == nil || bookURL == "" || chapterURL == "" {
		return nil
	}
	content, ok := cache.LoadContent(sourceURL, bookURL, chapterURL)
	if !ok {
		return nil
	}
	s.CacheChapterDocument(chapterURL, content)
	return content
}

// contentTexts 一章的可显示文本块（Go 侧正文是一段文本 + 可选副内容；
// 与 Python display_blocks 的文本块口径一致）。
func contentTexts(content *models.ChapterContent) []string {
	if content == nil {
		return nil
	}
	texts := make([]string, 0, 2)
	if content.Content != "" {
		texts = append(texts, content.Content)
	}
	if content.SubContent != nil && *content.SubContent != "" {
		texts = append(texts, *content.SubContent)
	}
	return texts
}

// saveProgress 落一篇进度：books 表结构化列（对齐 Python save_progress 的
// INSERT … ON CONFLICT DO UPDATE；真库的 books 没有 reading 列，进度是
// chapter_index/scroll_* /chapter_pos/font_size 这几列），再把同一份镜像进
// 书架行的 reading 子对象（flet 那屏"读到 X"读的就是它）。
// 身份列（name/author/kind/cover_url/source_url）取自会话上的书 —— 写错书就是静默写坏。
func (s *ReadingService) saveProgress(position ReadingPosition) {
	storage := s.hub.StorageService()
	if storage == nil {
		return
	}
	sess := s.GetSession()
	if sess == nil || sess.BookURL == "" {
		return
	}
	// 落库那份的时刻：updated_at 与书架镜像的 last_read_at 都用**这次保存的时刻**
	// （对齐 Python save_progress：position_get 回读的必须是入库时间，而不是客户端
	// 报上来的那个值）。响应面（position_save）回的是内存里那份 —— 不算篡改。
	now := time.Now().Unix()
	name, author, kind, cover := "", "", "", ""
	if sess.Book != nil {
		name, author, kind, cover = sess.Book.Name, sess.Book.Author, sess.Book.Kind, sess.Book.CoverURL
	}
	var paragraph interface{}
	if position.ParagraphIndex != nil {
		paragraph = *position.ParagraphIndex
	}
	_, _ = storage.Execute(
		`INSERT INTO books (book_url, source_url, name, author, kind, cover_url,
		 chapter_index, scroll_offset, scroll_ratio, chapter_pos, chapter_paragraph,
		 font_size, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(book_url) DO UPDATE SET
		   source_url = excluded.source_url, name = excluded.name,
		   author = excluded.author, kind = excluded.kind,
		   cover_url = excluded.cover_url, chapter_index = excluded.chapter_index,
		   scroll_offset = excluded.scroll_offset, scroll_ratio = excluded.scroll_ratio,
		   chapter_pos = excluded.chapter_pos,
		   chapter_paragraph = excluded.chapter_paragraph,
		   font_size = excluded.font_size, updated_at = excluded.updated_at`,
		sess.BookURL, sess.SourceURL, name, author, kind, cover,
		position.ChapterIndex, position.ScrollOffset, position.ScrollRatio,
		position.CharacterOffset, paragraph, position.FontSize,
		float64(now))
	// 书架行镜像：书不在架上（从没入架/试读暂存已丢）时镜像静默跳过，
	// 进度本体在上面那一行里。
	if shelf := s.hub.BookshelfService(); shelf != nil {
		mirror := position
		mirror.LastReadAt = now
		shelf.patchShelfReading(sess.BookURL, mirror.asReading(), nil)
	}
}

// LoadProgress 从 books 表结构化列读回一篇进度；没有记录回 nil（对齐 Python
// restore_position：chapter_url/name 不入库，续读那一章按 chapter_index 在目录里定位）。
func (s *ReadingService) LoadProgress(bookURL string) *ReadingPosition {
	storage := s.hub.StorageService()
	if storage == nil || bookURL == "" {
		return nil
	}
	var chapterIndex, chapterPos, fontSize int
	var scrollOffset, scrollRatio, updatedAt float64
	var paragraph sql.NullInt64
	err := storage.QueryRow(
		`SELECT chapter_index, scroll_offset, scroll_ratio, chapter_pos, font_size,
		 updated_at, chapter_paragraph FROM books WHERE book_url = ?`,
		bookURL).Scan(&chapterIndex, &scrollOffset, &scrollRatio, &chapterPos,
		&fontSize, &updatedAt, &paragraph)
	if err != nil {
		return nil
	}
	position := DefaultReadingPosition()
	position.ChapterIndex = chapterIndex
	position.ScrollOffset = scrollOffset
	position.ScrollRatio = scrollRatio
	position.CharacterOffset = chapterPos
	if fontSize > 0 {
		position.FontSize = fontSize
	}
	position.LastReadAt = int64(updatedAt)
	if paragraph.Valid && paragraph.Int64 >= 0 {
		index := int(paragraph.Int64)
		position.ParagraphIndex = &index
	}
	return &position
}

// CacheChapterDocument 把一章正文放进会话内容缓存（正文管线与测试的落点）。
func (s *ReadingService) CacheChapterDocument(chapterURL string, content *models.ChapterContent) {
	if chapterURL == "" || content == nil {
		return
	}
	s.mu.Lock()
	s.cache[chapterURL] = content
	s.mu.Unlock()
}

// ChapterDocument 一章正文的 JSON 面（对齐 Python chapter_document）：正文已是
// 文本块、段数与字符流总长由服务端那把尺量好；顺带起一轮预取。
func (s *ReadingService) ChapterDocument(sourceURL, bookURL, chapterURL string) (map[string]interface{}, error) {
	if !s.sessionMatches(sourceURL, bookURL) {
		return nil, s.readerSessionError(sourceURL, bookURL)
	}
	sess := s.GetSession()
	var chapter *models.Chapter
	for _, candidate := range sess.CachedChaps {
		if candidate != nil && candidate.URL == chapterURL {
			chapter = candidate
			break
		}
	}
	if chapter == nil {
		chapter = &models.Chapter{URL: chapterURL, BookURL: bookURL}
	}

	content := s.cachedDocument(chapterURL)
	if content == nil {
		content = s.loadDiskContent(sourceURL, bookURL, chapterURL)
	}
	if content == nil {
		var source *models.BookSource
		if srcSvc := s.hub.SourceService(); srcSvc != nil {
			source, _ = srcSvc.Get(sourceURL)
		}
		if source == nil {
			return nil, fmt.Errorf("书源不存在：%s", sourceURL)
		}
		if s.hub.RuleEngine() == nil {
			return nil, fmt.Errorf("rule engine unavailable")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		fetched, err := s.hub.RuleEngine().GetContent(ctx, source, chapter)
		if err != nil {
			return nil, fmt.Errorf("get content: %w", err)
		}
		content = fetched
		s.CacheChapterDocument(chapterURL, content)
	}

	paragraphs := ChapterParagraphs(contentTexts(content))
	blocks := make([]interface{}, 0, 2)
	for _, text := range contentTexts(content) {
		blocks = append(blocks, map[string]interface{}{
			"kind": "text", "value": text, "url": "", "binary": false,
			"mediaKey": "", "title": "", "headers": map[string]interface{}{},
		})
	}

	// 预取后续几章（会话还在这本书时才有意义；不等它）。
	s.prefetchAfter(sess, chapter)

	return map[string]interface{}{
		"chapterUrl":     chapterURL,
		"chapterIndex":   chapter.Index,
		"chapterName":    chapter.Title,
		"title":          content.Title,
		"imageStyle":     "",
		"blocks":         blocks,
		"paragraphCount": len(paragraphs),
		"totalChars":     ChapterTextLength(paragraphs),
	}, nil
}

// prefetchAfter 正文取到手后预取下一批章节（对齐 Python 顺带一轮预取）。
func (s *ReadingService) prefetchAfter(sess *ReadingSession, chapter *models.Chapter) {
	if s.hub.RuleEngine() == nil {
		return
	}
	start := chapter.Index + 1
	end := start + 3
	if end > len(sess.CachedChaps) {
		end = len(sess.CachedChaps)
	}
	s.prefetchWg.Add(1)
	go func() {
		defer s.prefetchWg.Done()
		for i := start; i < end; i++ {
			target := sess.CachedChaps[i]
			if target == nil {
				continue
			}
			if s.cachedDocument(target.URL) != nil {
				continue
			}
			if s.loadDiskContent(sess.SourceURL, sess.BookURL, target.URL) != nil {
				continue // 盘上有就不再联网（预取的目标本来就是"以后读着快"）
			}
			var source *models.BookSource
			if srcSvc := s.hub.SourceService(); srcSvc != nil {
				source, _ = srcSvc.Get(sess.SourceURL)
			}
			if source == nil {
				return
			}
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			fetched, err := s.hub.RuleEngine().GetContent(ctx, source, target)
			cancel()
			if err != nil {
				continue // 预取失败不阻断阅读
			}
			s.CacheChapterDocument(target.URL, fetched)
		}
	}()
}

// SearchContentByURL 书内全文检索（对齐 Python search_ensured）：先按需补索引
// 再查，回 written/indexedChapters/ftsAvailable——0 命中要分得清"没缓存"与"真没有"。
func (s *ReadingService) SearchContentByURL(bookURL, keyword, sourceURL string) map[string]interface{} {
	searchSvc := s.hub.ContentSearchService()
	if searchSvc == nil {
		return map[string]interface{}{
			"hits": []interface{}{}, "written": 0, "indexedChapters": 0, "ftsAvailable": false,
		}
	}
	return searchSvc.SearchEnsured(bookURL, s.cachedChapterDocs(), keyword)
}

// cachedChapterDocs 当前会话里已缓存的章节（索引只覆盖它们）。
func (s *ReadingService) cachedChapterDocs() []CachedChapterDoc {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.session == nil {
		return nil
	}
	docs := make([]CachedChapterDoc, 0, len(s.session.CachedChaps))
	for _, chapter := range s.session.CachedChaps {
		if chapter == nil || chapter.URL == "" {
			continue
		}
		content, ok := s.cache[chapter.URL]
		if !ok || content == nil {
			continue
		}
		docs = append(docs, CachedChapterDoc{
			ChapterIndex: chapter.Index,
			Paragraphs:   ChapterParagraphs(contentTexts(content)),
		})
	}
	return docs
}

// ---------------------------------------------------------------------------
// Multi-source search (port of Python ReadingService.search_books family)
// ---------------------------------------------------------------------------

// ensureSearchModel 进程内只留一个模型：每次新建会把分页与取消语义全丢掉。
func (s *ReadingService) ensureSearchModel() *SearchModel {
	s.searchMu.Lock()
	defer s.searchMu.Unlock()
	if s.searchModel == nil {
		s.searchStore = NewSearchBookStore(s.hub.StorageService())
		s.searchStore.ClearExpired(time.Now().Add(-SearchStoreTTL).UnixMilli())
		s.searchModel = NewSearchModel(
			func(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
				return s.hub.RuleEngine().SearchBook(ctx, source, keyword, page)
			},
			s.searchStore,
			func(books []*models.BookItem, searchID int64) {
				// 逐源推增量快照：读的是模型当时的 search_id（取消后为 0，
				// 晚到的 partial 因此天生不被界面认领）。
				s.publishSearchFinished(books, strconv.FormatInt(searchID, 10), false)
			})
	}
	return s.searchModel
}

// SearchInScope 多源找书（对齐 Python search_in_scope → search_books）：
// sources 现取、scope 走 SearchableSources；结果逐源经 finished=false 推出，
// 本轮扫完再发一条 finished=true 收口。被取消/顶替的一轮两件都不发。
func (s *ReadingService) SearchInScope(ctx context.Context, keyword, scope string, searchID int64) {
	if strings.TrimSpace(keyword) == "" {
		return
	}
	model := s.ensureSearchModel()
	sources := SearchableSources(s.hub.SourceService().List(), scope)
	books, current := model.Search(ctx, searchID, keyword, sources)
	if !current {
		return
	}
	s.publishSearchFinished(books, strconv.FormatInt(searchID, 10), true)
}

// StopSearch 停止当前搜索：取消在飞任务，晚到的结果不再回界面。
func (s *ReadingService) StopSearch() {
	s.searchMu.Lock()
	model := s.searchModel
	s.searchMu.Unlock()
	if model != nil {
		model.Cancel()
	}
}

// SearchScopes 搜索范围表（全部/未分组/各分组）与各自的可搜源数。
func (s *ReadingService) SearchScopes() []map[string]interface{} {
	return ScopeTable(s.hub.SourceService().List())
}

// SwitchSeeds 换源对话框打开时的本地种子：search_books 里的同名书（不联网，秒开）。
func (s *ReadingService) SwitchSeeds(book *models.BookItem) []map[string]interface{} {
	if book == nil || book.Name == "" {
		return []map[string]interface{}{}
	}
	store := s.ensureSearchStore()
	stored := store.ListByNameAuthor(book.Name, book.Author, 500)
	seeds := make([]map[string]interface{}, 0, len(stored))
	for _, item := range stored {
		seeds = append(seeds, BookSnapshot(item))
	}
	return seeds
}

// publishSearchFinished 发一条 SearchFinishedEvent；items 是整批快照（全量）。
func (s *ReadingService) publishSearchFinished(books []*models.BookItem, searchID string,
	finished bool) {
	bus := s.hub.EventBus()
	if bus == nil {
		return
	}
	items := make([]map[string]interface{}, 0, len(books))
	for _, book := range books {
		items = append(items, BookSnapshot(book))
	}
	bus.Publish(&core.SearchFinishedEvent{
		SearchID:  searchID,
		Items:     items,
		Finished:  finished,
		Timestamp: core.NowSeconds(),
	})
}
