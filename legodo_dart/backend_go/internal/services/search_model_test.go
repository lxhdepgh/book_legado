package services

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

func testStrPtr(value string) *string { return &value }

func testSource(url, name, group string, enabled bool) models.BookSource {
	source := models.BookSource{
		BookSourceURL:  url,
		BookSourceName: name,
		Enabled:        enabled,
		SearchURL:      testStrPtr("https://example.test/search?key={{key}}"),
	}
	if group != "" {
		source.BookSourceGroup = testStrPtr(group)
	}
	return source
}

func sourceURLs(sources []models.BookSource) []string {
	urls := make([]string, 0, len(sources))
	for _, source := range sources {
		urls = append(urls, source.BookSourceURL)
	}
	return urls
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestSearchableSourcesScopeFiltering(t *testing.T) {
	withURL := testSource("https://a.test", "a", "玄幻, 都市", true)
	grouped := testSource("https://f.test", "f", "玄幻", true)
	spacedGroup := testSource("https://b.test", "b", "  ", true) // strip 后为空 = 未分组
	emptyGroup := testSource("https://e.test", "e", "", true)    // BookSourceGroup 为 nil = 未分组
	disabled := testSource("https://c.test", "c", "玄幻", false)   // 停用
	noSearch := testSource("https://d.test", "d", "", true)
	noSearch.SearchURL = nil

	sources := []models.BookSource{withURL, grouped, spacedGroup, emptyGroup, disabled, noSearch}

	if got := len(SearchableSources(sources, ScopeAll)); got != 4 {
		t.Fatalf("ScopeAll 应留 4 条可搜源，得 %d", got)
	}
	if got := len(SearchableSources(sources, "")); got != 4 {
		t.Fatalf("空 scope 与 ScopeAll 同义，应留 4 条，得 %d", got)
	}
	if got := sourceURLs(SearchableSources(sources, "玄幻")); !equalStrings(got, []string{"https://a.test", "https://f.test"}) {
		t.Fatalf("玄幻 组应留 a、f，得 %v", got)
	}
	if got := sourceURLs(SearchableSources(sources, "都市")); !equalStrings(got, []string{"https://a.test"}) {
		t.Fatalf("都市 组应留 a，得 %v", got)
	}
	// "未分组"必须只留在外层守卫之后仍未被任何组收下的源：停用与没配 searchUrl
	// 的源不进数（与分组下拉同一口径），空白分组算未分组。
	if got := sourceURLs(SearchableSources(sources, ScopeUngrouped)); !equalStrings(got, []string{"https://b.test", "https://e.test"}) {
		t.Fatalf("未分组应留 b、e，得 %v", got)
	}
	if got := len(SearchableSources(sources, "不存在")); got != 0 {
		t.Fatalf("不存在的组不回退到全部，应留 0 条，得 %d", got)
	}
}

func TestScopeTableCountsAndOrder(t *testing.T) {
	sources := []models.BookSource{
		testSource("https://a.test", "a", "玄幻 , 都市", true),
		testSource("https://f.test", "f", "玄幻", true),
		testSource("https://b.test", "b", " ", true),
		testSource("https://c.test", "c", "玄幻", false),
	}

	table := ScopeTable(sources)
	if len(table) != 4 {
		t.Fatalf("应有两哨兵 + 两个分组，得 %d 行", len(table))
	}
	want := []struct {
		scope string
		count int
	}{
		{ScopeAll, 3},
		{ScopeUngrouped, 1},
		{"玄幻", 2}, // 分组名带空格也 strip 后计入；停用的 c 不算
		{"都市", 1},
	}
	for i, row := range want {
		if table[i]["scope"] != row.scope || table[i]["count"] != row.count {
			t.Fatalf("第 %d 行应 { %s, %d }，得 %v", i, row.scope, row.count, table[i])
		}
	}
}

func TestMergeSearchItemsBucketsAggregateAndPrecision(t *testing.T) {
	existing := []*models.BookItem{
		{Name: "斗破", Author: "天蚕", Kind: "玄幻", Origin: "s1", URL: "u1"},
	}
	newItems := []*models.BookItem{
		{Name: "斗破", Author: "天蚕", Origin: "s2", URL: "u2"},                // equal 桶去重 → 聚合 origin
		{Name: "斗破同人", Author: "y", Kind: "斗破周边", Origin: "s5", URL: "u5"}, // tags 桶
		{Name: "斗破苍穹", Author: "x", Origin: "s4", URL: "u4"},               // contains 桶
		{Name: "完全无关", Author: "x", Origin: "s3", URL: "u3"},               // other 桶
	}

	merged := MergeSearchItems(existing, newItems, "斗破", false)
	names := make([]string, 0, len(merged))
	for _, book := range merged {
		names = append(names, book.Name)
	}
	if !equalStrings(names, []string{"斗破", "斗破同人", "斗破苍穹", "完全无关"}) {
		t.Fatalf("桶序应为 equal→tags→contains→other，得 %v", names)
	}
	if got := merged[0].OriginSet(); !equalStrings(got, []string{"s1", "s2"}) {
		t.Fatalf("同名同作者应聚合 origins=[s1 s2]，得 %v", got)
	}
	if merged[0].URL != "u1" {
		t.Fatalf("去重应保留先到的那一条，URL 得 %s", merged[0].URL)
	}

	precision := MergeSearchItems(existing, newItems, "斗破", true)
	if len(precision) != 3 {
		t.Fatalf("precision 应丢弃 other 桶，得 %d 条", len(precision))
	}
}

// TestSearchModelConcurrencyPageAndEvents 验收三条：并发数=min(16,32,n)、
// 同号翻页 page+1、事件序列 partial×n+final（partial 由模型推出、final 由调用方收口）。
func TestSearchModelConcurrencyPageAndEvents(t *testing.T) {
	var inFlight, maxInFlight, calls int32
	var mu sync.Mutex
	pagesByCall := map[int]int{}
	partials := []int{}

	model := NewSearchModel(func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		current := atomic.AddInt32(&inFlight, 1)
		for {
			old := atomic.LoadInt32(&maxInFlight)
			if current <= old || atomic.CompareAndSwapInt32(&maxInFlight, old, current) {
				break
			}
		}
		time.Sleep(25 * time.Millisecond)
		atomic.AddInt32(&inFlight, -1)
		atomic.AddInt32(&calls, 1)
		mu.Lock()
		pagesByCall[page]++
		mu.Unlock()
		return []*models.BookItem{{
			Name:   fmt.Sprintf("%s-p%d", source.BookSourceName, page),
			Author: "作者",
			Origin: source.BookSourceURL,
			URL:    fmt.Sprintf("%s/%d", source.BookSourceURL, page),
		}}, nil
	}, nil, func(books []*models.BookItem, searchID int64) {
		mu.Lock()
		partials = append(partials, len(books))
		mu.Unlock()
	})

	sources := make([]models.BookSource, 40)
	for i := range sources {
		sources[i] = models.BookSource{
			BookSourceURL:  fmt.Sprintf("https://s%d.test", i),
			BookSourceName: fmt.Sprintf("s%d", i),
			Enabled:        true,
		}
	}

	books, current := model.Search(context.Background(), 1001, "斗破", sources)
	if !current {
		t.Fatal("首轮不应被判为取消")
	}
	if got := atomic.LoadInt32(&maxInFlight); got != SearchDefaultThread {
		t.Fatalf("并发峰值应为 min(16,32,40)=16，得 %d", got)
	}
	if len(books) != 40 {
		t.Fatalf("首轮应合并 40 条，得 %d", len(books))
	}
	if pagesByCall[1] != 40 || len(pagesByCall) != 1 {
		t.Fatalf("首轮全部调用 page=1，得 %v", pagesByCall)
	}

	// partial 序列：新一轮先发空快照（对齐 Python _notify），随后逐源全量递增。
	wantFirst := make([]int, 0, 41)
	wantFirst = append(wantFirst, 0)
	for i := 1; i <= 40; i++ {
		wantFirst = append(wantFirst, i)
	}
	mu.Lock()
	firstPartial := append([]int(nil), partials...)
	mu.Unlock()
	if !equalInts(firstPartial, wantFirst) {
		t.Fatalf("partial 序列应为 0..40，得 %v", firstPartial)
	}

	// 同一个 search_id 再调 = 翻页：page+1，结果累计（不重置）。
	books2, current2 := model.Search(context.Background(), 1001, "斗破", sources)
	if !current2 {
		t.Fatal("同号翻页不应被判为取消")
	}
	if pagesByCall[2] != 40 {
		t.Fatalf("翻页应全部调用 page=2，得 %v", pagesByCall)
	}
	if len(books2) != 80 {
		t.Fatalf("翻页后应累计 80 条，得 %d", len(books2))
	}

	// 并发上限的封顶：threadCount 拉到 64 也被 MAX_THREAD=32 截住。
	atomic.StoreInt32(&maxInFlight, 0)
	model.threadCount = 64
	if _, current3 := model.Search(context.Background(), 1002, "斗破", sources); !current3 {
		t.Fatal("新号首轮不应被判为取消")
	}
	if got := atomic.LoadInt32(&maxInFlight); got != SearchMaxThread {
		t.Fatalf("封顶后并发峰值应为 32，得 %d", got)
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestSearchModelCancelStopsMergingAndDropsFinal 验收"stop 后无新帧"：
// 取消后不再有新 partial，且本轮被判为 superseded（调用方不得发 finished=true）。
func TestSearchModelCancelStopsMergingAndDropsFinal(t *testing.T) {
	fake := func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(300 * time.Millisecond):
			return []*models.BookItem{{
				Name:   source.BookSourceName,
				Author: "作者",
				Origin: source.BookSourceURL,
			}}, nil
		}
	}
	var mu sync.Mutex
	partials := 0
	model := NewSearchModel(fake, nil, func(books []*models.BookItem, searchID int64) {
		mu.Lock()
		partials++
		mu.Unlock()
	})

	sources := make([]models.BookSource, 6)
	for i := range sources {
		sources[i] = models.BookSource{
			BookSourceURL:  fmt.Sprintf("https://s%d.test", i),
			BookSourceName: fmt.Sprintf("s%d", i),
			Enabled:        true,
		}
	}

	type outcome struct {
		books   []*models.BookItem
		current bool
	}
	done := make(chan outcome, 1)
	go func() {
		books, current := model.Search(context.Background(), 2001, "斗破", sources)
		done <- outcome{books, current}
	}()

	time.Sleep(60 * time.Millisecond) // 让 worker 都进入在飞
	model.Cancel()

	var result outcome
	select {
	case result = <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Cancel 后本轮应在 3 秒内收束")
	}
	if result.current {
		t.Fatal("被取消的一轮 current 必须为 false（不得发收口事件）")
	}
	if result.books != nil {
		t.Fatalf("被取消的一轮不应回带结果，得 %d 条", len(result.books))
	}

	mu.Lock()
	frozen := partials
	mu.Unlock()
	time.Sleep(400 * time.Millisecond) // 晚到的源结果窗口
	mu.Lock()
	after := partials
	mu.Unlock()
	if after != frozen {
		t.Fatalf("取消后不应再推出 partial：取消时 %d，400ms 后 %d", frozen, after)
	}

	// 取消后新号重开照常工作。
	books, current := model.Search(context.Background(), 2002, "斗破", sources)
	if !current || len(books) != 6 {
		t.Fatalf("取消后的新一轮应正常收 6 条，得 current=%v len=%d", current, len(books))
	}
}

// TestSearchModelDifferentIDSupersedesOldRun 验收"异号取消旧轮"：
// 旧轮判 superseded 且它的晚到结果不得进入新一轮。
func TestSearchModelDifferentIDSupersedesOldRun(t *testing.T) {
	fake := func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(200 * time.Millisecond):
			return []*models.BookItem{{
				Name:   source.BookSourceName,
				Author: "作者",
				Origin: source.BookSourceURL,
			}}, nil
		}
	}
	model := NewSearchModel(fake, nil, nil)

	slowSources := make([]models.BookSource, 8)
	for i := range slowSources {
		slowSources[i] = models.BookSource{
			BookSourceURL:  fmt.Sprintf("https://slow%d.test", i),
			BookSourceName: fmt.Sprintf("slow%d", i),
		}
	}

	type outcome struct {
		books   []*models.BookItem
		current bool
	}
	oldDone := make(chan outcome, 1)
	go func() {
		books, current := model.Search(context.Background(), 3001, "斗破", slowSources)
		oldDone <- outcome{books, current}
	}()

	time.Sleep(50 * time.Millisecond)
	fastSources := []models.BookSource{
		{BookSourceURL: "https://fast0.test", BookSourceName: "fast0"},
	}
	newBooks, newCurrent := model.Search(context.Background(), 3002, "斗破", fastSources)
	if !newCurrent {
		t.Fatal("新号一轮不应被判为取消")
	}
	if len(newBooks) != 1 || newBooks[0].Name != "fast0" {
		t.Fatalf("新轮结果只应含新号源，得 %d 条", len(newBooks))
	}

	select {
	case old := <-oldDone:
		if old.current {
			t.Fatal("被顶替的旧轮 current 必须为 false")
		}
		if old.books != nil {
			t.Fatalf("被顶替的旧轮不应回带结果，得 %d 条", len(old.books))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("旧轮应在被顶替后快速收束")
	}
}

// TestSearchInScopeEndToEndEvents 是 P2 的真实链路验收：源列表 → 范围过滤 →
// SearchModel → 规则引擎真实 HTTP → 合并 → 事件流（partial×n + final×1）。
func TestSearchInScopeEndToEndEvents(t *testing.T) {
	var mu sync.Mutex
	hits := map[string]int{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query := r.URL.Query()
		sourceID, page := query.Get("src"), query.Get("page")
		mu.Lock()
		hits[fmt.Sprintf("%s:p%s", sourceID, page)]++
		mu.Unlock()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<html><body><div class="book">
			<a class="name" href="/book/%s-%s">%s 第%s页</a>
			<span class="author">作者%s</span></div></body></html>`,
			sourceID, page, sourceID, page, sourceID)
	}))
	defer server.Close()

	hub := NewServiceHub()
	bus := core.NewEventBus()
	hub.eventBus = bus
	hub.ruleEngine = engine.NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
	makeSource := func(id string) models.BookSource {
		return models.BookSource{
			BookSourceURL:  "https://source-" + id + ".test",
			BookSourceName: "源" + id,
			Enabled:        true,
			SearchURL:      testStrPtr(server.URL + "/search?src=" + id + "&key={{key}}&page={{page}}"),
			RuleSearch: &models.SearchRule{
				BookList: testStrPtr(".book"),
				Name:     testStrPtr("a.name"),
				Author:   testStrPtr("span.author"),
				BookURL:  testStrPtr("a.name"),
			},
		}
	}
	sourceA, sourceB := makeSource("a"), makeSource("b")
	hub.sourceSvc = &SourceService{cache: map[string]*models.BookSource{
		sourceA.BookSourceURL: &sourceA,
		sourceB.BookSourceURL: &sourceB,
	}}
	hub.readingSvc = NewReadingService(hub)

	var eventsMu sync.Mutex
	var events []*core.SearchFinishedEvent
	bus.Subscribe("SearchFinishedEvent", func(event core.AppEvent) {
		if ev, ok := event.(*core.SearchFinishedEvent); ok {
			eventsMu.Lock()
			events = append(events, ev)
			eventsMu.Unlock()
		}
	})

	// 收集到条件满足为止；每轮交付共 partial 序列 + final 各一条，
	// partial 之间与 final 的交付顺序不定（typed handler 是并发派发的）。
	snapshot := func() ([]int, []int) {
		eventsMu.Lock()
		defer eventsMu.Unlock()
		var partialLengths, finalLengths []int
		for _, event := range events {
			items, _ := event.Items.([]map[string]interface{})
			if event.Finished {
				finalLengths = append(finalLengths, len(items))
			} else {
				partialLengths = append(partialLengths, len(items))
			}
		}
		return partialLengths, finalLengths
	}
	waitFor := func(pred func(partials, finals []int) bool) {
		deadline := time.Now().Add(5 * time.Second)
		for {
			partials, finals := snapshot()
			if pred(partials, finals) {
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("等待事件超时：partial=%v final=%v", partials, finals)
			}
			time.Sleep(10 * time.Millisecond)
		}
	}

	hub.readingSvc.SearchInScope(context.Background(), "斗破", ScopeAll, 777)

	// 首轮：2 个源 → partial 空快照 + 逐源全量递增（0,1,2）+ 一条 finished=true。
	waitFor(func(partials, finals []int) bool { return len(partials) >= 3 && len(finals) >= 1 })
	partials, finals := snapshot()
	sort.Ints(partials)
	if !equalInts(partials, []int{0, 1, 2}) {
		t.Fatalf("首轮 partial 长度序列应为 0,1,2，得 %v", partials)
	}
	if !equalInts(finals, []int{2}) {
		t.Fatalf("首轮 final 应带 2 条结果，得 %v", finals)
	}

	eventsMu.Lock()
	for i, event := range events {
		if event.SearchID != "777" {
			t.Fatalf("第 %d 条事件应回带 search_id=777，得 %s", i, event.SearchID)
		}
	}
	eventsMu.Unlock()

	finalItems := func() []map[string]interface{} {
		eventsMu.Lock()
		defer eventsMu.Unlock()
		items, _ := events[len(events)-1].Items.([]map[string]interface{})
		// 取最后一条 finished=true 的（顺序不可靠时兜底）
		for i := len(events) - 1; i >= 0; i-- {
			if events[i].Finished {
				items, _ = events[i].Items.([]map[string]interface{})
				break
			}
		}
		return items
	}
	items := finalItems()
	// 快照是 35 格全字段（对齐 Python asdict(BookItem)），键名一律 snake_case。
	if len(items[0]) != 35 {
		t.Fatalf("快照应含 35 个键，得 %d：%v", len(items[0]), items[0])
	}
	if items[0]["origin_name"] != "源a" && items[0]["origin_name"] != "源b" {
		t.Fatalf("快照键名应为 snake_case（origin_name），得 %v", items[0]["origin_name"])
	}

	mu.Lock()
	pageOneHits := hits["a:p1"] + hits["b:p1"]
	mu.Unlock()
	if pageOneHits != 2 {
		t.Fatalf("首轮两个源都应带 page=1，得 %d", pageOneHits)
	}

	// 同一 search_id 再调 = 翻页：请求带 page=2，结果累计到 4 条（无空快照那一发）。
	hub.readingSvc.SearchInScope(context.Background(), "斗破", ScopeAll, 777)
	waitFor(func(partials, finals []int) bool { return len(partials) >= 5 && len(finals) >= 2 })
	partials, finals = snapshot()
	sort.Ints(partials)
	if !equalInts(partials, []int{0, 1, 2, 3, 4}) {
		t.Fatalf("翻页后 partial 长度序列应为 0..4，得 %v", partials)
	}
	if !equalInts(finals, []int{2, 4}) {
		t.Fatalf("翻页后 final 应依次带 2、4 条结果，得 %v", finals)
	}
	mu.Lock()
	pageTwoHits := hits["a:p2"] + hits["b:p2"]
	mu.Unlock()
	if pageTwoHits != 2 {
		t.Fatalf("翻页两个源都应带 page=2，得 %d", pageTwoHits)
	}
}
