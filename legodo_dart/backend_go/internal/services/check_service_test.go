package services

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// ---------------------------------------------------------------------------
// 装配：假源（回环 httptest 主站）+ 引擎替身（只替规则面，探测仍走真 HTTP）
// ---------------------------------------------------------------------------

// checkTestSource 一条形状普通的假源：搜索地址相对（same_origin），
// 静态判据不会把它排进深检队列——那几个判据的用例在 tiered 专属测试里。
func checkTestSource(url, name string) models.BookSource {
	return models.BookSource{
		BookSourceURL: url, BookSourceName: name, Enabled: true,
		SearchURL: testStrPtr("/search?key={{key}}"),
	}
}

func newCheckTestHub(t *testing.T, sources []models.BookSource,
	bus *core.EventBus) (*ServiceHub, *SourceCheckService) {
	t.Helper()
	hub := NewServiceHub()
	if bus != nil {
		hub.eventBus = bus
	}
	if sources != nil {
		cache := make(map[string]*models.BookSource, len(sources))
		for i := range sources {
			src := sources[i]
			cache[src.BookSourceURL] = &src
		}
		hub.sourceSvc = &SourceService{cache: cache}
	}
	svc := NewCheckService(hub)
	hub.checkSvc = svc
	return hub, svc
}

// fakeCheckEngine 规则引擎替身：六个入口都可注入行为，nil 时回零值。
type fakeCheckEngine struct {
	searchFn     func(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error)
	exploreFn    func(ctx context.Context, source *models.BookSource, categoryURL string, page int) ([]*models.BookItem, error)
	categoriesFn func(ctx context.Context, source *models.BookSource) ([]engine.ExploreCategory, error)
	bookInfoFn   func(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error)
	chaptersFn   func(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error)
	contentFn    func(ctx context.Context, source *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error)
}

func (f *fakeCheckEngine) Search(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
	if f.searchFn != nil {
		return f.searchFn(ctx, source, keyword, page)
	}
	return nil, nil
}

func (f *fakeCheckEngine) Explore(ctx context.Context, source *models.BookSource, categoryURL string, page int) ([]*models.BookItem, error) {
	if f.exploreFn != nil {
		return f.exploreFn(ctx, source, categoryURL, page)
	}
	return nil, nil
}

func (f *fakeCheckEngine) ExploreCategories(ctx context.Context, source *models.BookSource) ([]engine.ExploreCategory, error) {
	if f.categoriesFn != nil {
		return f.categoriesFn(ctx, source)
	}
	return nil, nil
}

func (f *fakeCheckEngine) BookInfo(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error) {
	if f.bookInfoFn != nil {
		return f.bookInfoFn(ctx, source, bookURL)
	}
	return nil, nil
}

func (f *fakeCheckEngine) Chapters(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error) {
	if f.chaptersFn != nil {
		return f.chaptersFn(ctx, source, tocURL)
	}
	return nil, nil
}

func (f *fakeCheckEngine) Content(ctx context.Context, source *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error) {
	if f.contentFn != nil {
		return f.contentFn(ctx, source, chapter)
	}
	return nil, nil
}

// healthySearchEngine 搜索稳定出两本书（列表链路可用的最小形状）。
func healthySearchEngine() *fakeCheckEngine {
	return &fakeCheckEngine{
		searchFn: func(_ context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
			return []*models.BookItem{
				{Name: "书一", URL: source.BookSourceURL + "/book/1", Origin: source.BookSourceURL},
				{Name: "书二", URL: source.BookSourceURL + "/book/2", Origin: source.BookSourceURL},
			}, nil
		},
	}
}

// fullChainEngine 列表可用 + 正文一路验通（深遍 fully_usable 的最小形状）。
func fullChainEngine() *fakeCheckEngine {
	engine := healthySearchEngine()
	engine.bookInfoFn = func(_ context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error) {
		return &models.BookItem{Name: "书一", URL: bookURL}, nil
	}
	engine.chaptersFn = func(_ context.Context, _ *models.BookSource, tocURL string) ([]*models.Chapter, error) {
		return []*models.Chapter{{Title: "第一章", URL: tocURL + "/c/1"}}, nil
	}
	engine.contentFn = func(_ context.Context, _ *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error) {
		return &models.ChapterContent{ChapterURL: chapter.URL, Content: "正文第一段"}, nil
	}
	return engine
}

func newLoopbackSite(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("<html><body>book site</body></html>"))
	}))
	t.Cleanup(server.Close)
	return server
}

// checkEvents 收集逐源帧（typed handler 并发派发，读侧自锁）。
type checkEvents struct {
	mu     sync.Mutex
	frames []*core.CheckFinishedEvent
}

func (e *checkEvents) subscribe(bus *core.EventBus) {
	bus.Subscribe("CheckFinishedEvent", func(event core.AppEvent) {
		if ev, ok := event.(*core.CheckFinishedEvent); ok {
			e.mu.Lock()
			e.frames = append(e.frames, ev)
			e.mu.Unlock()
		}
	})
}

func (e *checkEvents) snapshot() []*core.CheckFinishedEvent {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]*core.CheckFinishedEvent(nil), e.frames...)
}

// ---------------------------------------------------------------------------
// 判据①：逐源帧数 = 源数
// ---------------------------------------------------------------------------

func TestCheckRunAllPublishesOneFramePerSource(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	events := &checkEvents{}
	events.subscribe(bus)

	sources := []models.BookSource{
		checkTestSource(site.URL+"/a", "源甲"),
		checkTestSource(site.URL+"/b", "源乙"),
		checkTestSource(site.URL+"/c", "源丙"),
	}
	_, svc := newCheckTestHub(t, sources, bus)
	svc.SetEngine(healthySearchEngine())

	summary, err := svc.RunAll(nil)
	if err != nil {
		t.Fatalf("RunAll 出错：%v", err)
	}

	frames := events.snapshot()
	if len(frames) != len(sources) {
		t.Fatalf("逐源帧数应=%d，得 %d", len(sources), len(frames))
	}
	doneSeen := map[int]bool{}
	for _, frame := range frames {
		if frame.SourceID == "" || frame.Summary == "" {
			t.Fatalf("帧缺 source_id/summary：%+v", frame)
		}
		if frame.StateClass != "search_only" {
			t.Fatalf("浅遍健康源应为 search_only，得 %q", frame.StateClass)
		}
		if frame.Total != len(sources) {
			t.Fatalf("帧分母应=%d，得 %d", len(sources), frame.Total)
		}
		doneSeen[frame.Done] = true
	}
	// 浅遍一条源只派一次：done 走满 1..N 且不重复。
	for want := 1; want <= len(sources); want++ {
		if !doneSeen[want] {
			t.Fatalf("done 应走满 1..%d，缺 %d（已见 %v）", len(sources), want, doneSeen)
		}
	}

	if got := summary["total"]; got != len(sources) {
		t.Fatalf("summary.total 应=%d，得 %v", len(sources), got)
	}
	if got := summary["finished"]; got != len(sources) {
		t.Fatalf("summary.finished 应=%d，得 %v", len(sources), got)
	}
	if got := summary["usable"]; got != len(sources) {
		t.Fatalf("summary.usable 应=%d，得 %v", len(sources), got)
	}
	if got := summary["blocked"]; got != 0 {
		t.Fatalf("summary.blocked 应=0，得 %v", got)
	}

	// 分档：三条都浅遍当场排进队列，留给后续轮次。
	tiering, ok := summary["tiering"].(map[string]interface{})
	if !ok {
		t.Fatalf("summary.tiering 形状不对：%T", summary["tiering"])
	}
	for key, want := range map[string]interface{}{
		"plan_total": len(sources), "shallow": len(sources), "deep": 0,
		"deferred": len(sources), "queued": len(sources), "carried": 0,
	} {
		if got := tiering[key]; got != want {
			t.Fatalf("tiering.%s 应=%v，得 %v", key, want, got)
		}
	}
	byReason, _ := tiering["by_reason"].(map[string]interface{})
	if got := byReason[ReasonListUsable]; got != len(sources) {
		t.Fatalf("by_reason.list_usable 应=%d，得 %v", len(sources), got)
	}
}

// ---------------------------------------------------------------------------
// 判据②：坏源计入后 summary 非空（好源坏源各一，两发帧，计数不吞）
// ---------------------------------------------------------------------------

func TestCheckRunAllCountsBadSourceIntoSummary(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	events := &checkEvents{}
	events.subscribe(bus)

	good := checkTestSource(site.URL+"/good", "好源")
	bad := checkTestSource(site.URL+"/bad", "坏源")
	_, svc := newCheckTestHub(t, []models.BookSource{good, bad}, bus)
	engine := healthySearchEngine()
	broken := errors.New("connection refused")
	engine.searchFn = func(_ context.Context, source *models.BookSource, _ string, _ int) ([]*models.BookItem, error) {
		if source.BookSourceURL == bad.BookSourceURL {
			return nil, broken
		}
		return []*models.BookItem{{Name: "书一", URL: source.BookSourceURL + "/book/1"}}, nil
	}
	svc.SetEngine(engine)

	summary, err := svc.RunAll(nil)
	if err != nil {
		t.Fatalf("RunAll 出错：%v", err)
	}
	if summary == nil || len(summary) == 0 {
		t.Fatalf("坏源计入后 summary 不该为空")
	}
	frames := events.snapshot()
	if len(frames) != 2 {
		t.Fatalf("好源坏源各一发，应有 2 帧，得 %d", len(frames))
	}
	classes := map[string]string{}
	for _, frame := range frames {
		classes[frame.SourceID] = frame.StateClass
	}
	if classes[good.BookSourceURL] != "search_only" {
		t.Fatalf("好源应 search_only，得 %q", classes[good.BookSourceURL])
	}
	if classes[bad.BookSourceURL] != "unknown" {
		t.Fatalf("搜索网络错应 unknown，得 %q", classes[bad.BookSourceURL])
	}
	if got := summary["total"]; got != 2 {
		t.Fatalf("summary.total 应=2，得 %v", got)
	}
	if got := summary["finished"]; got != 2 {
		t.Fatalf("summary.finished 应=2，得 %v", got)
	}
	if got := summary["usable"]; got != 1 {
		t.Fatalf("summary.usable 应=1（坏源不吞），得 %v", got)
	}
}

// ---------------------------------------------------------------------------
// 补验切片：队列里"这一轮开跑前就欠着"的那片走深遍，当场排进来的留给后续轮
// ---------------------------------------------------------------------------

func TestCheckRunAllDeepSliceRunsCarriedOnly(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	events := &checkEvents{}
	events.subscribe(bus)

	carried := checkTestSource(site.URL+"/carried", "老欠账")
	fresh := checkTestSource(site.URL+"/fresh", "新欠账")
	_, svc := newCheckTestHub(t, []models.BookSource{carried, fresh}, bus)
	svc.SetEngine(fullChainEngine())
	svc.DeepQueue().Add(carried.BookSourceURL, ReasonListUsable)

	summary, err := svc.RunAll(nil)
	if err != nil {
		t.Fatalf("RunAll 出错：%v", err)
	}
	frames := events.snapshot()
	if len(frames) != 2 {
		t.Fatalf("应有 2 帧，得 %d", len(frames))
	}
	classes := map[string]string{}
	for _, frame := range frames {
		classes[frame.SourceID] = frame.StateClass
	}
	if classes[carried.BookSourceURL] != "fully_usable" {
		t.Fatalf("带欠账的源这轮应走深遍 fully_usable，得 %q", classes[carried.BookSourceURL])
	}
	if classes[fresh.BookSourceURL] != "search_only" {
		t.Fatalf("当场排进来的源这轮应只走浅遍 search_only，得 %q", classes[fresh.BookSourceURL])
	}

	tiering, _ := summary["tiering"].(map[string]interface{})
	if got := tiering["deep"]; got != 1 {
		t.Fatalf("tiering.deep 应=1，得 %v", got)
	}
	if got := tiering["shallow"]; got != 1 {
		t.Fatalf("tiering.shallow 应=1，得 %v", got)
	}
	if got := tiering["carried"]; got != 1 {
		t.Fatalf("tiering.carried 应=1，得 %v", got)
	}
	if got := tiering["deferred"]; got != 1 {
		t.Fatalf("tiering.deferred 应=1，得 %v", got)
	}
	if got := tiering["queued"]; got != 1 {
		t.Fatalf("tiering.queued 应=1（深遍跑过的摘掉，只剩新欠账），得 %v", got)
	}
	byReason, _ := tiering["by_reason"].(map[string]interface{})
	// by_reason 数的是整本欠账（带着的 1 条 + 浅遍新排的 1 条），不是切片。
	if got := byReason[ReasonListUsable]; got != 2 {
		t.Fatalf("by_reason.list_usable 应=2（整本欠账），得 %v", got)
	}
	if reason := svc.DeepQueue().ReasonOf(carried.BookSourceURL); reason != "" {
		t.Fatalf("深遍给了结论的应从队列摘掉，得 %q", reason)
	}
	if reason := svc.DeepQueue().ReasonOf(fresh.BookSourceURL); reason != ReasonListUsable {
		t.Fatalf("浅遍新排的应留在队列，得 %q", reason)
	}
}

// ---------------------------------------------------------------------------
// 快照形状：顶层 8 键（golden check.snapshot.json）
// ---------------------------------------------------------------------------

func TestCheckSnapshotShape(t *testing.T) {
	bus := core.NewEventBus()
	defer bus.Close()
	_, svc := newCheckTestHub(t, []models.BookSource{}, bus)

	empty := svc.Snapshot()
	wantKeys := []string{"running", "cancelled", "total", "depth", "tiering",
		"deep_queue", "summary", "results"}
	sort.Strings(wantKeys)
	gotKeys := make([]string, 0, len(empty))
	for key := range empty {
		gotKeys = append(gotKeys, key)
	}
	sort.Strings(gotKeys)
	if !reflect.DeepEqual(gotKeys, wantKeys) {
		t.Fatalf("空快照键应=%v，得 %v", wantKeys, gotKeys)
	}
	if empty["running"] != false || empty["cancelled"] != false || empty["summary"] != nil {
		t.Fatalf("未跑过时 running/cancelled=false、summary=nil：%+v", empty)
	}
	tiering, _ := empty["tiering"].(map[string]interface{})
	if got := tiering["slice_limit"]; got != DeepPerRoundMin {
		t.Fatalf("空分档 slice_limit 应=%d，得 %v", DeepPerRoundMin, got)
	}
	if rows, ok := empty["deep_queue"].([]interface{}); !ok || len(rows) != 0 {
		t.Fatalf("空队列应是空数组，得 %#v", empty["deep_queue"])
	}
}

func TestCheckSnapshotAfterRun(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	sources := []models.BookSource{
		checkTestSource(site.URL+"/a", "源甲"),
		checkTestSource(site.URL+"/b", "源乙"),
	}
	_, svc := newCheckTestHub(t, sources, bus)
	svc.SetEngine(healthySearchEngine())
	if _, err := svc.RunAll(nil); err != nil {
		t.Fatalf("RunAll 出错：%v", err)
	}

	snap := svc.Snapshot()
	if snap["running"] != false {
		t.Fatalf("跑完 running 应为 false：%v", snap["running"])
	}
	if snap["depth"] != CheckDepthAuto {
		t.Fatalf("批量那轮 depth 应为 auto，得 %v", snap["depth"])
	}
	if got := snap["total"]; got != 2 {
		t.Fatalf("total 应=2，得 %v", got)
	}
	summary, ok := snap["summary"].(map[string]interface{})
	if !ok {
		t.Fatalf("跑完 summary 应非空，得 %#v", snap["summary"])
	}
	if got := summary["finished"]; got != 2 {
		t.Fatalf("summary.finished 应=2，得 %v", got)
	}
	rows, ok := snap["results"].([]interface{})
	if !ok || len(rows) != 2 {
		t.Fatalf("results 应 2 行，得 %#v", snap["results"])
	}
	row, _ := rows[0].(map[string]interface{})
	for _, key := range []string{"source_id", "state_class", "summary", "sample_chapter_urls"} {
		if _, ok := row[key]; !ok {
			t.Fatalf("结果行缺 %s：%#v", key, row)
		}
	}
	queue, ok := snap["deep_queue"].([]interface{})
	if !ok || len(queue) != 2 {
		t.Fatalf("跑完队列应 2 条，得 %#v", snap["deep_queue"])
	}
	entry, _ := queue[0].(map[string]interface{})
	for _, key := range []string{"raw_url", "reason", "attempts", "exhausted"} {
		if _, ok := entry[key]; !ok {
			t.Fatalf("队列条目缺 %s：%#v", key, entry)
		}
	}
}

// ---------------------------------------------------------------------------
// 停止：协作停止后 summary 不写（cancelled 路径），在飞的结果不发布
// ---------------------------------------------------------------------------

func TestCheckCancelKeepsSummaryUnset(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	events := &checkEvents{}
	events.subscribe(bus)

	sources := []models.BookSource{
		checkTestSource(site.URL+"/a", "源甲"),
		checkTestSource(site.URL+"/b", "源乙"),
	}
	_, svc := newCheckTestHub(t, sources, bus)

	entered := make(chan struct{}, 8)
	engine := &fakeCheckEngine{}
	engine.searchFn = func(ctx context.Context, _ *models.BookSource, _ string, _ int) ([]*models.BookItem, error) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-ctx.Done()
		return nil, ctx.Err()
	}
	svc.SetEngine(engine)

	type outcome struct {
		summary map[string]interface{}
		err     error
	}
	done := make(chan outcome, 1)
	go func() {
		summary, err := svc.RunAll(nil)
		done <- outcome{summary: summary, err: err}
	}()

	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("等不到引擎被派发")
	}
	svc.Cancel()

	var got outcome
	select {
	case got = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("取消后 RunAll 没收敛")
	}
	if !errors.Is(got.err, errCheckCancelled) {
		t.Fatalf("取消应收口为 errCheckCancelled，得 %v", got.err)
	}
	if len(got.summary) != 0 {
		t.Fatalf("取消那轮 summary 不写，得 %#v", got.summary)
	}
	if frames := events.snapshot(); len(frames) != 0 {
		t.Fatalf("取消前没有源跑完，不该有帧，得 %d", len(frames))
	}
	snap := svc.Snapshot()
	if snap["cancelled"] != true || snap["running"] != false || snap["summary"] != nil {
		t.Fatalf("取消后快照应 cancelled=true/running=false/summary=nil：%+v", snap)
	}
}

func TestCheckRunAllJoinsRunningRun(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	sources := []models.BookSource{checkTestSource(site.URL+"/a", "源甲")}
	_, svc := newCheckTestHub(t, sources, bus)

	entered := make(chan struct{}, 8)
	release := make(chan struct{})
	engine := healthySearchEngine()
	engine.searchFn = func(ctx context.Context, source *models.BookSource, _ string, _ int) ([]*models.BookItem, error) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		return []*models.BookItem{{Name: "书一", URL: source.BookSourceURL + "/book/1"}}, nil
	}
	svc.SetEngine(engine)

	firstDone := make(chan map[string]interface{}, 1)
	go func() {
		summary, _ := svc.RunAll(nil)
		firstDone <- summary
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("等不到第一轮被派发")
	}

	// 第二轮不该再派一遍全量联网，而是接到第一轮的收尾上。
	joined := make(chan map[string]interface{}, 1)
	go func() {
		summary, _ := svc.RunAll(nil)
		joined <- summary
	}()
	close(release)

	var first map[string]interface{}
	select {
	case first = <-firstDone:
	case <-time.After(5 * time.Second):
		t.Fatal("第一轮没收敛")
	}
	var second map[string]interface{}
	select {
	case second = <-joined:
	case <-time.After(5 * time.Second):
		t.Fatal("第二轮没接到第一轮的收尾")
	}
	if got := first["total"]; got != 1 {
		t.Fatalf("第一轮 total 应=1，得 %v", got)
	}
	if got := second["total"]; got != 1 {
		t.Fatalf("第二轮回的应是同一轮 summary，得 %v", got)
	}
}

// ---------------------------------------------------------------------------
// 单源报告：31 键（golden check.source.json，depth='list'）
// ---------------------------------------------------------------------------

func TestCheckSingleReportShape(t *testing.T) {
	site := newLoopbackSite(t)
	bus := core.NewEventBus()
	defer bus.Close()
	events := &checkEvents{}
	events.subscribe(bus)
	_, svc := newCheckTestHub(t, nil, bus)
	svc.SetEngine(healthySearchEngine())

	report, err := svc.CheckSingle(map[string]interface{}{
		"bookSourceUrl":  site.URL,
		"bookSourceName": "R4健康源",
		"enabled":        true,
		"searchUrl":      "/search?q={{key}}",
	}, CheckDepthList)
	if err != nil {
		t.Fatalf("CheckSingle 出错：%v", err)
	}
	wantKeys := []string{
		"auto_disabled", "book_count", "category_results", "chapter_count",
		"check_state", "error", "idx", "name", "raw_url",
		"reading_check_skipped", "reading_error", "reading_stage",
		"reading_status", "reading_tried", "response_time_ms", "sample_book",
		"sample_chapter", "sample_chapter_urls", "search_error",
		"search_keyword", "search_method", "search_status", "status",
		"status_code", "tag_check_skipped", "tag_check_timeout", "tag_invalid",
		"tag_total", "tag_valid", "unsupported_java_packages", "usable",
	}
	gotKeys := make([]string, 0, len(report))
	for key := range report {
		gotKeys = append(gotKeys, key)
	}
	sort.Strings(gotKeys)
	if !reflect.DeepEqual(gotKeys, wantKeys) {
		t.Fatalf("单源报告键应=%v，得 %v", wantKeys, gotKeys)
	}
	if report["status"] != "ok" || report["check_state"] != "search_only" {
		t.Fatalf("健康源浅检应 ok/search_only：%+v", report)
	}
	if report["reading_check_skipped"] != CheckDepthList {
		t.Fatalf("浅检应带 reading_check_skipped=list，得 %v", report["reading_check_skipped"])
	}
	if report["search_method"] != "rule-engine" || report["search_keyword"] != "我的" {
		t.Fatalf("搜索段读数不对：%+v", report)
	}
	if report["usable"] != true {
		t.Fatalf("search_only 应 usable=true，得 %v", report["usable"])
	}
	if packages, ok := report["unsupported_java_packages"].([]string); !ok || len(packages) != 0 {
		t.Fatalf("unsupported_java_packages 应是空数组，得 %#v", report["unsupported_java_packages"])
	}

	frames := events.snapshot()
	if len(frames) != 1 {
		t.Fatalf("单源检测应发 1 帧，得 %d", len(frames))
	}
	if frames[0].Done != 1 || frames[0].Total != 1 || frames[0].StateClass != "search_only" {
		t.Fatalf("单源帧形状不对：%+v", frames[0])
	}
	if frames[0].SourceID != site.URL {
		t.Fatalf("帧 source_id 应为源 url，得 %q", frames[0].SourceID)
	}
}

func TestCheckSingleRejectsBadInput(t *testing.T) {
	bus := core.NewEventBus()
	defer bus.Close()
	_, svc := newCheckTestHub(t, nil, bus)
	svc.SetEngine(healthySearchEngine())

	if _, err := svc.CheckSingle(nil, CheckDepthFull); err == nil {
		t.Fatal("空 source_json 应报错")
	}
	if _, err := svc.CheckSingle(map[string]interface{}{"bookSourceUrl": "https://a.com"}, "deep"); err == nil {
		t.Fatal("未知深度应报错")
	}
}

// ---------------------------------------------------------------------------
// 九态分类（check_service._classify_result 的判据表）
// ---------------------------------------------------------------------------

func TestClassifyCheckResult(t *testing.T) {
	blockedCode := 403
	cases := []struct {
		name   string
		result CheckResult
		want   string
	}{
		{"正文验通=完整可用", CheckResult{
			Status: "ok", SearchStatus: "ok", ReadingStatus: "ok"}, "fully_usable"},
		{"要浏览器=没验过正文不算失败", CheckResult{
			Status: "ok", SearchStatus: "ok", ReadingStatus: "needs_browser"}, "search_only"},
		{"拦截页排在最前（哪怕搜索无结果）", CheckResult{
			Status: "blocked", SearchStatus: "no_results", ReadingStatus: "skip"}, "blocked"},
		{"403 且搜索没成=拦我们不是源坏了", CheckResult{
			Status: "err", StatusCode: &blockedCode,
			SearchStatus: "rule_error", ReadingStatus: "skip"}, "blocked"},
		{"403 但搜索成了=照常可用", CheckResult{
			Status: "err", StatusCode: &blockedCode,
			SearchStatus: "ok", ReadingStatus: "skip"}, "search_only"},
		{"只搜索可用", CheckResult{
			Status: "ok", SearchStatus: "ok", ReadingStatus: "skip"}, "search_only"},
		{"只发现可用", CheckResult{
			Status: "ok", SearchStatus: "rule_error", TagValid: 1, ReadingStatus: ""}, "explore_only"},
		{"两段都可用但正文未验=列表可用", CheckResult{
			Status: "ok", SearchStatus: "ok", TagValid: 2, ReadingStatus: "skip"}, "list_usable"},
		{"正文超时=没结论", CheckResult{
			Status: "ok", SearchStatus: "ok", ReadingStatus: "timeout"}, "unknown"},
		{"搜索规则错=不可用", CheckResult{
			Status: "ok", SearchStatus: "rule_error", ReadingStatus: "skip"}, "unusable"},
		{"首页 err 且搜索错=没结论", CheckResult{
			Status: "err", SearchStatus: "rule_error", ReadingStatus: "skip"}, "unknown"},
		{"标签抽检超时=没结论", CheckResult{
			Status: "ok", SearchStatus: "rule_error", TagCheckTimeout: true,
			ReadingStatus: "skip"}, "unknown"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := classifyCheckResult(&testCase.result); got != testCase.want {
				t.Fatalf("应为 %s，得 %s", testCase.want, got)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// 静态判据与队列（tiered_check）
// ---------------------------------------------------------------------------

func TestStaticDeepReason(t *testing.T) {
	withSearch := func(home, search string) models.BookSource {
		source := models.BookSource{BookSourceURL: home, Enabled: true}
		if search != "" {
			source.SearchURL = testStrPtr(search)
		}
		return source
	}
	cases := []struct {
		name   string
		source models.BookSource
		want   string
	}{
		{"跨站", withSearch("https://a.com", "https://b.com/s"), ReasonCrossHost},
		{"裸主机跨站", withSearch("https://a.com", "othersite.com/s"), ReasonCrossHost},
		{"同注册域名=同站", withSearch("https://www.a.com", "https://m.a.com/s"), ""},
		{"相对路径=同源", withSearch("https://a.com", "/search?key={{key}}"), ""},
		{"无搜索地址=不按主机判", withSearch("https://a.com", ""), ""},
		{"主站不是 http=判不出", withSearch("ftp://a.com", "https://a.com/s"), ReasonUndecidableHost},
		{"搜索地址是 JS=动态", withSearch("https://a.com", "@js:host()"), ReasonUndecidableHost},
		{"相对文件名不算主机", withSearch("https://a.com", "search.php?key={{key}}"), ""},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := StaticDeepReason(&testCase.source); got != testCase.want {
				t.Fatalf("应为 %q，得 %q", testCase.want, got)
			}
		})
	}
}

func TestPerRoundDeepLimit(t *testing.T) {
	cases := []struct{ plan, want int }{
		{0, 12}, {50, 12}, {100, 12}, {150, 12}, {200, 16}, {1000, 80},
	}
	for _, testCase := range cases {
		if got := PerRoundDeepLimit(testCase.plan); got != testCase.want {
			t.Fatalf("PerRoundDeepLimit(%d) 应=%d，得 %d", testCase.plan, testCase.want, got)
		}
	}
}

func TestShallowDeepReasonRules(t *testing.T) {
	bookCount := 2
	shallow := func(state string) CheckResult {
		return CheckResult{CheckState: state, ReadingCheckSkipped: strPtr(ShallowDepthMark)}
	}
	usable := shallow("search_only")
	usable.BookCount = &bookCount
	if got := ShallowDeepReason(&usable); got != ReasonListUsable {
		t.Fatalf("列表可用+有候选书应排 list_usable，得 %q", got)
	}
	empty := shallow("list_usable")
	if got := ShallowDeepReason(&empty); got != "" {
		t.Fatalf("列表可用但没候选书不该排，得 %q", got)
	}
	unknown := shallow("unknown")
	if got := ShallowDeepReason(&unknown); got != ReasonUnknownShallow {
		t.Fatalf("浅遍没结论应排 unknown_shallow，得 %q", got)
	}
	notShallow := CheckResult{CheckState: "search_only", ReadingCheckSkipped: strPtr(CheckDepthFull)}
	notShallow.BookCount = &bookCount
	if got := ShallowDeepReason(&notShallow); got != "" {
		t.Fatalf("深遍结果不该排浅遍的债，得 %q", got)
	}
	if got := ShallowDeepReason(nil); got != "" {
		t.Fatalf("nil 结果应回空，得 %q", got)
	}
}

func TestDeepQueueReasonChangeResetsBudget(t *testing.T) {
	now := 1000.0
	queue := NewDeepQueue()
	queue.clock = func() float64 { return now }

	queue.Add("https://a.com", ReasonUnknownShallow)
	queue.NoteAttempt("https://a.com")
	queue.NoteAttempt("https://a.com")
	now += 60
	// 同一个原因反复排进来只刷新时间戳：不许洗白次数。
	queue.Add("https://a.com", ReasonUnknownShallow)
	if queue.Len() != 1 {
		t.Fatalf("队列应仍有这一条，得 %d", queue.Len())
	}
	if reason, ok := queue.Pending()["https://a.com"]; !ok || reason != ReasonUnknownShallow {
		t.Fatalf("次数=2 < 上限 3，应仍在待办里：%q %v", reason, ok)
	}
	queue.NoteAttempt("https://a.com")
	if _, ok := queue.Pending()["https://a.com"]; ok {
		t.Fatalf("试满配额的不该出现在待办里")
	}
	// 判据换了就是新信息：预算重置、重新排队。
	now += 60
	queue.Add("https://a.com", ReasonCrossHost)
	if reason, ok := queue.Pending()["https://a.com"]; !ok || reason != ReasonCrossHost {
		t.Fatalf("换判据后应重新待办，得 %q %v", reason, ok)
	}
}

func TestDeepQueueTTLAndCooldown(t *testing.T) {
	now := 2000.0
	queue := NewDeepQueue()
	queue.clock = func() float64 { return now }

	queue.Add("https://expired.com", ReasonListUsable)
	now += DeepEntryTTLSeconds + 1
	if gone := queue.Prune(); len(gone) != 1 || gone[0] != "https://expired.com" {
		t.Fatalf("过 TTL 应被清：%v", gone)
	}

	queue.Add("https://spent.com", ReasonUnknownShallow)
	for i := 0; i < DeepMaxAttempts; i++ {
		queue.NoteAttempt("https://spent.com")
	}
	now += 60
	if gone := queue.Prune(); len(gone) != 0 {
		t.Fatalf("试满但 cooldown 没到不该清：%v", gone)
	}
	if _, ok := queue.Pending()["https://spent.com"]; ok {
		t.Fatalf("试满的条目不该待办")
	}
	now += DeepRetryCooldownSeconds + 1
	if gone := queue.Prune(); len(gone) != 1 || gone[0] != "https://spent.com" {
		t.Fatalf("歇够 cooldown 应被清：%v", gone)
	}
}

func TestDeepQueueSelectRanksByAttemptsThenAge(t *testing.T) {
	now := 3000.0
	queue := NewDeepQueue()
	queue.clock = func() float64 { return now }

	first := models.BookSource{BookSourceURL: "https://one.com", Enabled: true}
	second := models.BookSource{BookSourceURL: "https://two.com", Enabled: true}
	third := models.BookSource{BookSourceURL: "https://three.com", Enabled: true}

	queue.Add(first.BookSourceURL, ReasonListUsable)
	now += 10
	queue.Add(second.BookSourceURL, ReasonListUsable)
	now += 10
	queue.Add(third.BookSourceURL, ReasonListUsable)
	// 第一条已经试过一回：欠得久但试得多，限额 1 时应让给没试过的。
	queue.NoteAttempt(first.BookSourceURL)

	picked := queue.Select([]models.BookSource{first, second, third}, 1)
	if len(picked) != 1 || picked[0].BookSourceURL != second.BookSourceURL {
		t.Fatalf("限额 1 应先挑试得少的（second），得 %+v", picked)
	}
	all := queue.Select([]models.BookSource{first, second, third}, -1)
	if len(all) != 3 {
		t.Fatalf("不限额应全挑，得 %d", len(all))
	}
	if all[0].BookSourceURL != first.BookSourceURL || all[2].BookSourceURL != third.BookSourceURL {
		t.Fatalf("不限额顺序跟源列表，得 %+v", all)
	}
}
