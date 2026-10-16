package services

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/internal/models"
)

const (
	// SearchMaxThread 对齐 Python SearchModel.MAX_THREAD：Legado 的 9 只是 OkHttp
	// 派发线程数，搜索 flow 本身按 threadCount（默认 32）在飞。
	SearchMaxThread = 32
	// SearchDefaultThread 是 thread_count 的默认值。
	SearchDefaultThread = 16
	// SearchSourceTimeout 单源搜索超时（历史 30 秒）。
	SearchSourceTimeout = 30 * time.Second
	// SearchStoreTTL 换源种子的存活期。
	SearchStoreTTL = 24 * time.Hour

	// ScopeAll / ScopeUngrouped 是搜索范围的两个哨兵（服务端定义，客户端原样传回）。
	ScopeAll       = "__all__"
	ScopeUngrouped = "__ungrouped__"
)

// SearchOneFunc 单源搜索回调（对齐 Python SearchModel.search_one）：
// 换源扫描与找书都复用这一条模型，测试也在这里注入假源。
type SearchOneFunc func(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error)

// SearchModel 是有状态的多源搜索模型（移植 Python search_model.SearchModel）：
// 同一个 search_id 再调即翻页；换 id 即取消旧一轮（generation++）并回到第 1 页。
// 单个源失败只跳过那一个源，结果逐源合并后经 onUpdate 推出全量快照。
type SearchModel struct {
	mu          sync.Mutex
	searchOne   SearchOneFunc
	store       *SearchBookStore
	onUpdate    func(books []*models.BookItem, searchID int64)
	threadCount int
	precision   bool

	searchID   int64
	page       int
	key        string
	sources    []models.BookSource
	books      []*models.BookItem
	generation int

	runSeq uint64
	runs   map[uint64]context.CancelFunc
}

// NewSearchModel wires the model; onUpdate 每合并一个源调用一次（含本轮开始时的空快照）。
func NewSearchModel(searchOne SearchOneFunc, store *SearchBookStore,
	onUpdate func(books []*models.BookItem, searchID int64)) *SearchModel {
	return &SearchModel{
		searchOne:   searchOne,
		store:       store,
		onUpdate:    onUpdate,
		threadCount: SearchDefaultThread,
		runs:        make(map[uint64]context.CancelFunc),
	}
}

// Search 跑一轮（或一页）多源搜索。返回的 current=false 表示这轮已被取消/顶替，
// 调用方不得再发收口事件 —— 对应 Python 里 gather 抛 CancelledError 直接中断
// search_books 的行为（晚到的那发 finished=True 会顶掉用户刚按下的"停止"）。
func (m *SearchModel) Search(ctx context.Context, searchID int64, key string,
	sources []models.BookSource) ([]*models.BookItem, bool) {
	if ctx == nil {
		ctx = context.Background()
	}

	m.mu.Lock()
	if searchID != m.searchID {
		if key == "" {
			m.cancelRunsLocked()
			m.generation++
			m.searchID = 0
			m.mu.Unlock()
			return nil, false
		}
		m.cancelRunsLocked()
		m.generation++
		m.searchID = searchID
		m.page = 1
		m.key = key
		m.sources = append([]models.BookSource(nil), sources...)
		m.books = nil
		m.notifyLocked()
		if len(m.sources) == 0 {
			m.mu.Unlock()
			return []*models.BookItem{}, true
		}
	} else {
		m.page++
		if sources != nil {
			m.sources = append([]models.BookSource(nil), sources...)
		}
	}
	gen := m.generation
	page := m.page
	runKey := m.key
	runSources := append([]models.BookSource(nil), m.sources...)
	m.mu.Unlock()

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	m.mu.Lock()
	m.runSeq++
	runID := m.runSeq
	m.runs[runID] = cancel
	m.mu.Unlock()

	count := m.threadCount
	if count > SearchMaxThread {
		count = SearchMaxThread
	}
	if count > len(runSources) {
		count = len(runSources)
	}
	if count < 0 {
		count = 0
	}

	if count > 0 {
		queue := make(chan models.BookSource)
		go func() {
			defer close(queue)
			for _, source := range runSources {
				select {
				case queue <- source:
				case <-runCtx.Done():
					return
				}
			}
		}()
		var wg sync.WaitGroup
		for i := 0; i < count; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for {
					select {
					case <-runCtx.Done():
						return
					case source, ok := <-queue:
						if !ok {
							return
						}
						m.runOne(runCtx, gen, runKey, page, source)
					}
				}
			}()
		}
		wg.Wait()
	}

	m.mu.Lock()
	delete(m.runs, runID)
	current := gen == m.generation
	books := append([]*models.BookItem(nil), m.books...)
	m.mu.Unlock()

	if !current {
		return nil, false
	}
	return books, true
}

// runOne 跑单个源：超时 30s、失败跳过；只有仍是当前 generation 才合并与推出。
func (m *SearchModel) runOne(ctx context.Context, gen int, key string, page int,
	source models.BookSource) {
	reqCtx, cancel := context.WithTimeout(ctx, SearchSourceTimeout)
	defer cancel()

	items, err := m.searchOne(reqCtx, &source, key, page)
	if err != nil || len(items) == 0 {
		return
	}

	m.mu.Lock()
	if gen != m.generation {
		m.mu.Unlock()
		return
	}
	m.books = MergeSearchItems(m.books, items, m.key, m.precision)
	m.notifyLocked()
	m.mu.Unlock()

	if m.store != nil {
		// 落库只是给换源列表备用，写失败不该吞掉已经合并好的这一条。
		_ = m.store.Insert(items)
	}
}

// notifyLocked 把当前全量结果推给调用方；必须在持锁时调用，保证 partial 顺序 = 合并顺序。
func (m *SearchModel) notifyLocked() {
	if m.onUpdate == nil {
		return
	}
	snapshot := append([]*models.BookItem(nil), m.books...)
	m.onUpdate(snapshot, m.searchID)
}

// Cancel 停止当前搜索：取消在飞任务并自增 generation（晚到的结果不再回界面）。
func (m *SearchModel) Cancel() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cancelRunsLocked()
	m.generation++
	m.searchID = 0
}

func (m *SearchModel) cancelRunsLocked() {
	for id, cancel := range m.runs {
		cancel()
		delete(m.runs, id)
	}
}

// --- 结果合并（移植 Python merge_search_items / SearchModel.mergeItems） ---

const (
	bucketEqual = iota
	bucketTags
	bucketContains
	bucketOther
)

func searchBucket(book *models.BookItem, searchKey string) int {
	if book.Name == searchKey || book.Author == searchKey {
		return bucketEqual
	}
	// 分类名命中关键词也算相关：搜索词经常是"玄幻"这类题材而不是书名。
	if searchKey != "" && strings.Contains(book.Kind, searchKey) {
		return bucketTags
	}
	if strings.Contains(book.Name, searchKey) || strings.Contains(book.Author, searchKey) {
		return bucketContains
	}
	return bucketOther
}

// MergeSearchItems 四个桶（精确/标签/包含/其他）：同桶内按 (name,author) 去重并聚合
// origin，桶内按来源数降序；precision 时丢弃"其他"桶（含新来的与已有的都不动）。
func MergeSearchItems(existing, newItems []*models.BookItem, searchKey string, precision bool) []*models.BookItem {
	var buckets [4][]*models.BookItem

	for _, book := range existing {
		if book == nil {
			continue
		}
		book.Origins = book.OriginSet()
		bucket := searchBucket(book, searchKey)
		buckets[bucket] = append(buckets[bucket], book)
	}
	for _, book := range newItems {
		if book == nil {
			continue
		}
		bucket := searchBucket(book, searchKey)
		if bucket == bucketOther && precision {
			continue
		}
		var duplicate *models.BookItem
		for _, item := range buckets[bucket] {
			if item.Name == book.Name && item.Author == book.Author {
				duplicate = item
				break
			}
		}
		if duplicate != nil {
			duplicate.AddOrigin(book.Origin)
		} else {
			book.Origins = book.OriginSet()
			buckets[bucket] = append(buckets[bucket], book)
		}
	}

	// Python 只对前三个桶排序；"其他"桶保持合并顺序。
	for i := bucketEqual; i <= bucketContains; i++ {
		sort.SliceStable(buckets[i], func(a, b int) bool {
			return len(buckets[i][a].OriginSet()) > len(buckets[i][b].OriginSet())
		})
	}

	result := make([]*models.BookItem, 0, len(existing)+len(newItems))
	result = append(result, buckets[bucketEqual]...)
	result = append(result, buckets[bucketTags]...)
	result = append(result, buckets[bucketContains]...)
	if !precision {
		result = append(result, buckets[bucketOther]...)
	}
	return result
}

// --- 搜索范围（移植 Python searchable_sources / scope_table） ---

// SearchableSources 按搜索范围过滤：只留已启用且配了搜索 URL 的书源。
// scope 取分组名，或两个哨兵：ScopeAll（空值同义）、ScopeUngrouped（一个组名都没有的源）。
// "未分组"判据按切完 strip 再丢空，不按原串 truthiness —— 与分组名的切法同一条。
func SearchableSources(sources []models.BookSource, scope string) []models.BookSource {
	scope = strings.TrimSpace(scope)
	result := make([]models.BookSource, 0, len(sources))
	for _, source := range sources {
		if !source.Enabled {
			continue
		}
		searchURL := ""
		if source.SearchURL != nil {
			searchURL = *source.SearchURL
		}
		if strings.TrimSpace(searchURL) == "" {
			continue
		}
		if scope != "" && scope != ScopeAll {
			group := ""
			if source.BookSourceGroup != nil {
				group = *source.BookSourceGroup
			}
			groups := splitSourceGroups(group)
			if scope == ScopeUngrouped {
				if len(groups) > 0 {
					continue
				}
			} else if !containsString(groups, scope) {
				continue
			}
		}
		result = append(result, source)
	}
	return result
}

// ScopeTable 搜索范围表：两个哨兵 + 排序去重的分组名，各带可搜源数。
// 只有服务端一份算法 —— 客户端各自数一遍就是同一规则的第二份实现。
func ScopeTable(sources []models.BookSource) []map[string]interface{} {
	seen := make(map[string]bool)
	var groups []string
	for _, source := range sources {
		group := ""
		if source.BookSourceGroup != nil {
			group = *source.BookSourceGroup
		}
		for _, name := range splitSourceGroups(group) {
			if !seen[name] {
				seen[name] = true
				groups = append(groups, name)
			}
		}
	}
	sort.Strings(groups)

	scopes := make([]string, 0, len(groups)+2)
	scopes = append(scopes, ScopeAll, ScopeUngrouped)
	scopes = append(scopes, groups...)

	table := make([]map[string]interface{}, 0, len(scopes))
	for _, scope := range scopes {
		table = append(table, map[string]interface{}{
			"scope": scope,
			"count": len(SearchableSources(sources, scope)),
		})
	}
	return table
}

func splitSourceGroups(group string) []string {
	var result []string
	for _, part := range strings.Split(group, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			result = append(result, trimmed)
		}
	}
	return result
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
