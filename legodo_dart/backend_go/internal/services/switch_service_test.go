package services

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
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

// newSwitchTestHub 换源扫描的最小装配：sources 非 nil 时挂源服务（空切片=挂空库），
// nil 时源服务缺席（供"起扫前就出错"那一支用）。storage 可为 nil（种子落库会静默跳过）。
func newSwitchTestHub(t *testing.T, sources []models.BookSource, bus *core.EventBus,
	storage *core.StorageService) (*ServiceHub, *ReadingService) {
	t.Helper()
	hub := NewServiceHub()
	if bus != nil {
		hub.eventBus = bus
	}
	if storage != nil {
		hub.storageSvc = storage
	}
	hub.readingSvc = NewReadingService(hub)
	if sources != nil {
		cache := make(map[string]*models.BookSource, len(sources))
		for i := range sources {
			src := sources[i]
			cache[src.BookSourceURL] = &src
		}
		hub.sourceSvc = &SourceService{cache: cache}
	}
	return hub, hub.readingSvc
}

func switchMatchBook(name, author, originURL string) *models.BookItem {
	return &models.BookItem{
		Name:   name,
		Author: author,
		Origin: originURL,
		URL:    originURL + "/book/1",
		Intro:  "简介",
		Kind:   "玄幻",
	}
}

// switchEvents 收集换源事件（typed handler 是并发派发，读侧自锁 + 轮询等待）。
type switchEvents struct {
	mu       sync.Mutex
	progress []*core.SourceSwitchProgressEvent
	finished []*core.SourceSwitchFinishedEvent
}

func (e *switchEvents) subscribe(bus *core.EventBus) {
	bus.Subscribe("SourceSwitchProgressEvent", func(event core.AppEvent) {
		if ev, ok := event.(*core.SourceSwitchProgressEvent); ok {
			e.mu.Lock()
			e.progress = append(e.progress, ev)
			e.mu.Unlock()
		}
	})
	bus.Subscribe("SourceSwitchFinishedEvent", func(event core.AppEvent) {
		if ev, ok := event.(*core.SourceSwitchFinishedEvent); ok {
			e.mu.Lock()
			e.finished = append(e.finished, ev)
			e.mu.Unlock()
		}
	})
}

func (e *switchEvents) progressSnapshot() []*core.SourceSwitchProgressEvent {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]*core.SourceSwitchProgressEvent(nil), e.progress...)
}

func (e *switchEvents) finishedSnapshot() []*core.SourceSwitchFinishedEvent {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]*core.SourceSwitchFinishedEvent(nil), e.finished...)
}

func waitUntil(t *testing.T, what string, pred func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if pred() {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("等待超时：%s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// ---------------------------------------------------------------------------
// OrderSwitchSources / RankSwitchMatches（纯函数）
// ---------------------------------------------------------------------------

func TestOrderSwitchSources(t *testing.T) {
	noSearch := testSource("https://noseach.test", "noSearch", "", true)
	noSearch.SearchURL = nil

	sources := []models.BookSource{
		testSource("https://www.same.com", "same", "", true), // 自身：必须排掉
		testSource("https://same.com", "sameRoot", "", true), // 同站（www 剥离后同名）
		testSource("https://other.test/a", "a", "", true),
		testSource("https://zeta.test", "z", "", true),
		testSource("https://alpha.test", "al", "", true),
		testSource("https://disabled.test", "d", "", false), // 停用
		testSource("https://novel.test", "g", "玄幻", true),
		noSearch,
	}

	got := OrderSwitchSources(sources, "https://www.same.com", "")
	want := []string{"https://same.com", "https://alpha.test", "https://novel.test", "https://other.test/a", "https://zeta.test"}
	if !equalStrings(sourceURLs(got), want) {
		t.Fatalf("同站优先 + 组内 URL 字典序；自身/停用/无搜索规则要排掉。得 %v", sourceURLs(got))
	}

	// 分组限定：只留该组。
	if got := sourceURLs(OrderSwitchSources(sources, "", "玄幻")); !equalStrings(got, []string{"https://novel.test"}) {
		t.Fatalf("玄幻组应只留 novel，得 %v", got)
	}
	// 分组不存在不回退到全部。
	if got := OrderSwitchSources(sources, "", "不存在"); len(got) != 0 {
		t.Fatalf("不存在的组应回空队列，得 %v", sourceURLs(got))
	}
	// 自己不在列表里时照常全部返回（无同站则全按 URL 字典序）。
	got = OrderSwitchSources(sources, "https://not-in-list.test", "玄幻")
	if !equalStrings(sourceURLs(got), []string{"https://novel.test"}) {
		t.Fatalf("自身缺席时组过滤照常，得 %v", sourceURLs(got))
	}
}

func TestRankSwitchMatches(t *testing.T) {
	src := func(url, name string) models.BookSource {
		return models.BookSource{BookSourceURL: url, BookSourceName: name}
	}
	matches := []switchMatch{
		{source: src("https://b.test", "乙源"), book: switchMatchBook("斗破同人", "天蚕土豆", "https://b.test")},
		{source: src("https://c.test", "丙源"), book: switchMatchBook("斗破", "别人", "https://c.test")},
		{source: src("https://a.test", "甲源"), book: switchMatchBook("斗破", "天蚕土豆", "https://a.test")},
		{source: src("https://0.test", "零源"), book: switchMatchBook("斗破", "天蚕土豆", "https://0.test")},
	}

	rows := RankSwitchMatches("斗破", "天蚕土豆", matches)
	if len(rows) != 4 {
		t.Fatalf("应回 4 行，得 %d", len(rows))
	}
	// 书名精确 + 作者命中（再按 URL 字典序）→ 书名精确 → 仅包含。
	wantSources := []string{"https://0.test", "https://a.test", "https://c.test", "https://b.test"}
	gotSources := make([]string, 0, len(rows))
	for _, row := range rows {
		gotSources = append(gotSources, row["source_url"].(string))
	}
	if !equalStrings(gotSources, wantSources) {
		t.Fatalf("排序应为 精确+作者 → 精确 → 包含，得 %v", gotSources)
	}

	// 行形状：{source_url, source_name, book: 35 键快照}。
	if len(rows[0]) != 3 {
		t.Fatalf("行应只有 3 个键，得 %v", rows[0])
	}
	book, ok := rows[0]["book"].(map[string]interface{})
	if !ok {
		t.Fatalf("book 应是字典，得 %T", rows[0]["book"])
	}
	if len(book) != 35 {
		t.Fatalf("book 快照应 35 键，得 %d", len(book))
	}
	if book["name"] != "斗破" || book["origin"] != "https://0.test" {
		t.Fatalf("book 快照键口径错：%v", book)
	}
	if rows[0]["source_name"] != "零源" {
		t.Fatalf("source_name 应为零源，得 %v", rows[0]["source_name"])
	}

	// 大小写折叠：目标小写、书名大写也算精确。
	upper := RankSwitchMatches("abc", "", []switchMatch{
		{source: src("https://u.test", "u"), book: switchMatchBook("ABC 后传", "", "https://u.test")},
		{source: src("https://l.test", "l"), book: switchMatchBook("xyz abc", "", "https://l.test")},
	})
	if upper[0]["source_url"] != "https://l.test" {
		t.Fatalf("abc 精确命中应排最前（大小写折叠），得 %v", upper[0]["source_url"])
	}
}

// ---------------------------------------------------------------------------
// SwitchScan 主流程
// ---------------------------------------------------------------------------

// TestSwitchScanBoundedConcurrency 验收并发封顶=8、done=total、候选逐源收集。
func TestSwitchScanBoundedConcurrency(t *testing.T) {
	sources := make([]models.BookSource, 40)
	for i := range sources {
		sources[i] = testSource(fmt.Sprintf("https://s%d.test", i), fmt.Sprintf("s%d", i), "", true)
	}
	_, reading := newSwitchTestHub(t, sources, nil, nil)
	reading.switchWorkerCount = SwitchConcurrency
	reading.switchPublishEvery = time.Hour

	var inFlight, maxInFlight int32
	reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		current := atomic.AddInt32(&inFlight, 1)
		for {
			old := atomic.LoadInt32(&maxInFlight)
			if current <= old || atomic.CompareAndSwapInt32(&maxInFlight, old, current) {
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
		atomic.AddInt32(&inFlight, -1)
		return []*models.BookItem{switchMatchBook("斗破", "天蚕土豆", source.BookSourceURL)}, nil
	}

	snapshot := reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 41, "斗破", "")
	if got := atomic.LoadInt32(&maxInFlight); got != SwitchConcurrency {
		t.Fatalf("并发峰值应为 %d，得 %d", SwitchConcurrency, got)
	}
	if snapshot["done"] != 40 || snapshot["total"] != 40 {
		t.Fatalf("done/total 应为 40/40，得 %v/%v", snapshot["done"], snapshot["total"])
	}
	if snapshot["failures"] != 0 || snapshot["starved"] != 0 {
		t.Fatalf("全成功轮不该有失败计数，得 failures=%v starved=%v", snapshot["failures"], snapshot["starved"])
	}
	if snapshot["candidates"].([]map[string]interface{}) == nil || len(snapshot["candidates"].([]map[string]interface{})) != 40 {
		t.Fatalf("40 个源各 1 条命中，应聚 40 个候选，得 %v", snapshot["candidates"])
	}
	if snapshot["running"] != false || snapshot["cancelled"] != false || snapshot["error"] != "" {
		t.Fatalf("收尾形状错：running=%v cancelled=%v error=%v", snapshot["running"], snapshot["cancelled"], snapshot["error"])
	}
}

// TestSwitchScanTimeoutAndStarved 超时归因两个方向：本机不忙记"超时"（failures），
// 本机忙记"本机繁忙"（starved），两者分开计数（对齐 Python @0.5 占用比判据）。
func TestSwitchScanTimeoutAndStarved(t *testing.T) {
	old := switchStarvedCheck
	defer func() { switchStarvedCheck = old }()

	run := func(starved bool, switchID int64) map[string]interface{} {
		sources := []models.BookSource{testSource("https://s0.test", "s0", "", true)}
		_, reading := newSwitchTestHub(t, sources, nil, nil)
		reading.switchWorkerCount = 1
		reading.switchSourceTimeout = 30 * time.Millisecond
		reading.switchPublishEvery = time.Hour
		reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
			page int) ([]*models.BookItem, error) {
			<-ctx.Done() // 真走 30ms 超时线
			return nil, ctx.Err()
		}
		switchStarvedCheck = func(wallStart time.Time, cpuStart float64) bool { return starved }
		return reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, switchID, "斗破", "")
	}

	snapshot := run(false, 51)
	if snapshot["failures"] != 1 || snapshot["starved"] != 0 {
		t.Fatalf("本机不忙时超时应记 failures=1 starved=0，得 %v/%v", snapshot["failures"], snapshot["starved"])
	}
	snapshot = run(true, 52)
	if snapshot["starved"] != 1 || snapshot["failures"] != 0 {
		t.Fatalf("本机忙时超时应记 starved=1 failures=0，得 %v/%v", snapshot["starved"], snapshot["failures"])
	}
	if snapshot["done"] != 1 || snapshot["total"] != 1 {
		t.Fatalf("超时也算跑完这一源：done/total 应 1/1，得 %v/%v", snapshot["done"], snapshot["total"])
	}
}

// TestSwitchScanCooperativeStop 合作式停止：叫停后不再取新源，在途那一发跑完即收口；
// 已扫出的候选照常排序返回；done < total 才是"被叫停"的形状。
func TestSwitchScanCooperativeStop(t *testing.T) {
	sources := make([]models.BookSource, 10)
	for i := range sources {
		sources[i] = testSource(fmt.Sprintf("https://s%d.test", i), fmt.Sprintf("s%d", i), "", true)
	}
	bus := core.NewEventBus()
	_, reading := newSwitchTestHub(t, sources, bus, nil)
	events := &switchEvents{}
	events.subscribe(bus)
	reading.switchWorkerCount = 2
	// 负窗口 = 完全不节流：done 随每次收尾帧推进（对齐 Python：run["done"] 只在
	// on_progress 真正发出时更新，被节流吞掉的那些不推进快照）。
	reading.switchPublishEvery = -time.Nanosecond
	reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(120 * time.Millisecond):
		}
		return []*models.BookItem{switchMatchBook("斗破", "天蚕土豆", source.BookSourceURL)}, nil
	}

	done := make(chan map[string]interface{}, 1)
	go func() {
		done <- reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 61, "斗破", "")
	}()

	time.Sleep(40 * time.Millisecond) // 两个 worker 都在飞、谁都没回
	first := reading.SwitchCancel()
	if first["cancelled"] != true {
		t.Fatalf("在跑的一轮应叫停成功，得 %v", first)
	}
	if again := reading.SwitchCancel(); again["cancelled"] != false {
		t.Fatalf("重复叫停应回 cancelled=false，得 %v", again)
	}

	var snapshot map[string]interface{}
	select {
	case snapshot = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("叫停后本轮应在 5 秒内收口")
	}
	if snapshot["switch_id"] != "61" || snapshot["cancelled"] != true || snapshot["running"] != false {
		t.Fatalf("被叫停轮快照形状错：%v", snapshot)
	}
	gotDone, _ := snapshot["done"].(int)
	// 在途 2 个源跑完即收口、剩余 8 个不取：done 只随收尾帧推进（可能被同一
	// 时钟刻度吞掉一格），但一定 < total 且不超过在途数。
	if gotDone < 1 || gotDone > 2 {
		t.Fatalf("被叫停轮 done 应在 1..2（在途跑完、剩余不取），得 %d", gotDone)
	}
	candidates := snapshot["candidates"].([]map[string]interface{})
	if len(candidates) != 2 {
		t.Fatalf("两个在途源各 1 条命中，候选应 2 条，得 %d", len(candidates))
	}
	waitUntil(t, "被叫停轮的 finished 事件", func() bool { return len(events.finishedSnapshot()) >= 1 })
	finished := events.finishedSnapshot()[0]
	if !finished.Cancelled || finished.SwitchID != "61" {
		t.Fatalf("finished 事件应 cancelled=true switch_id=61，得 %+v", finished)
	}
}

// TestSwitchScanSupersedesPrevious 新一轮在跑时先把旧轮合作式叫停并等它收口，
// 再接手（旧轮 cancelled=true、done<total；新轮照常跑满）。
func TestSwitchScanSupersedesPrevious(t *testing.T) {
	sources := make([]models.BookSource, 6)
	for i := range sources {
		sources[i] = testSource(fmt.Sprintf("https://s%d.test", i), fmt.Sprintf("s%d", i), "", true)
	}
	bus := core.NewEventBus()
	_, reading := newSwitchTestHub(t, sources, bus, nil)
	events := &switchEvents{}
	events.subscribe(bus)
	reading.switchWorkerCount = 1
	reading.switchPublishEvery = time.Hour
	reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(60 * time.Millisecond):
		}
		return []*models.BookItem{switchMatchBook("斗破", "天蚕土豆", source.BookSourceURL)}, nil
	}

	oldDone := make(chan map[string]interface{}, 1)
	go func() {
		oldDone <- reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 71, "斗破", "")
	}()
	time.Sleep(15 * time.Millisecond) // 旧轮在飞

	newSnapshot := reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 72, "斗破", "")

	select {
	case oldSnapshot := <-oldDone:
		if oldSnapshot["switch_id"] != "71" || oldSnapshot["cancelled"] != true {
			t.Fatalf("旧轮应被叫停收口：%v", oldSnapshot)
		}
		oldDoneCount, _ := oldSnapshot["done"].(int)
		if oldDoneCount >= 6 {
			t.Fatalf("旧轮不该跑满 6 个源，得 done=%d", oldDoneCount)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("旧轮应在被顶替后收口")
	}
	if newSnapshot["switch_id"] != "72" || newSnapshot["cancelled"] != false || newSnapshot["done"] != 6 {
		t.Fatalf("新轮应不受影响跑满：%v", newSnapshot)
	}
	waitUntil(t, "新旧两轮各一条 finished", func() bool { return len(events.finishedSnapshot()) >= 2 })
	byID := map[string]*core.SourceSwitchFinishedEvent{}
	for _, ev := range events.finishedSnapshot() {
		byID[ev.SwitchID] = ev
	}
	if byID["71"] == nil || !byID["71"].Cancelled {
		t.Fatalf("旧轮 finished 应 cancelled=true，得 %+v", byID["71"])
	}
	if byID["72"] == nil || byID["72"].Cancelled {
		t.Fatalf("新轮 finished 应 cancelled=false，得 %+v", byID["72"])
	}
}

// TestSwitchScanThreeEndingsEmitFinished 三种收尾（扫完 / 被叫停 / 起扫前就出错）
// 都必须发恰好一条 finished——不发，界面的转圈就永远停不下来。
func TestSwitchScanThreeEndingsEmitFinished(t *testing.T) {
	t.Run("扫完", func(t *testing.T) {
		sources := []models.BookSource{
			testSource("https://a.test", "a", "", true),
			testSource("https://b.test", "b", "", true),
		}
		bus := core.NewEventBus()
		_, reading := newSwitchTestHub(t, sources, bus, nil)
		events := &switchEvents{}
		events.subscribe(bus)
		reading.switchPublishEvery = time.Hour
		reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
			page int) ([]*models.BookItem, error) {
			return []*models.BookItem{switchMatchBook("斗破", "天蚕土豆", source.BookSourceURL)}, nil
		}

		reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 81, "斗破", "")
		waitUntil(t, "扫完的 finished", func() bool { return len(events.finishedSnapshot()) >= 1 })
		time.Sleep(50 * time.Millisecond)
		finished := events.finishedSnapshot()
		if len(finished) != 1 {
			t.Fatalf("应恰好一条 finished，得 %d", len(finished))
		}
		if finished[0].Cancelled || finished[0].Error != "" || finished[0].SwitchID != "81" {
			t.Fatalf("扫完收尾形状错：%+v", finished[0])
		}
		candidates, ok := finished[0].Candidates.([]map[string]interface{})
		if !ok || len(candidates) != 2 {
			t.Fatalf("finished 应带 2 条候选，得 %v", finished[0].Candidates)
		}
	})

	t.Run("被叫停", func(t *testing.T) {
		sources := make([]models.BookSource, 4)
		for i := range sources {
			sources[i] = testSource(fmt.Sprintf("https://s%d.test", i), fmt.Sprintf("s%d", i), "", true)
		}
		bus := core.NewEventBus()
		_, reading := newSwitchTestHub(t, sources, bus, nil)
		events := &switchEvents{}
		events.subscribe(bus)
		reading.switchWorkerCount = 2
		reading.switchPublishEvery = time.Hour
		reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
			page int) ([]*models.BookItem, error) {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(80 * time.Millisecond):
			}
			return nil, nil
		}

		done := make(chan struct{})
		go func() {
			reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 82, "斗破", "")
			close(done)
		}()
		time.Sleep(20 * time.Millisecond)
		reading.SwitchCancel()
		<-done
		waitUntil(t, "被叫停的 finished", func() bool { return len(events.finishedSnapshot()) >= 1 })
		time.Sleep(50 * time.Millisecond)
		finished := events.finishedSnapshot()
		if len(finished) != 1 || !finished[0].Cancelled {
			t.Fatalf("被叫停应恰好一条 cancelled=true，得 %+v", finished)
		}
	})

	t.Run("起扫前就出错", func(t *testing.T) {
		bus := core.NewEventBus()
		_, reading := newSwitchTestHub(t, nil, bus, nil) // 源服务缺席
		events := &switchEvents{}
		events.subscribe(bus)

		snapshot := reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 83, "斗破", "")
		errMsg, _ := snapshot["error"].(string)
		if !strings.HasPrefix(errMsg, "RuntimeError") {
			t.Fatalf("错误收尾应记 RuntimeError，得 %q", errMsg)
		}
		if snapshot["running"] != false {
			t.Fatalf("错误收尾也必须把 running 置 false，得 %v", snapshot["running"])
		}
		waitUntil(t, "错误收尾的 finished", func() bool { return len(events.finishedSnapshot()) >= 1 })
		finished := events.finishedSnapshot()[0]
		if finished.Error != errMsg || finished.Cancelled {
			t.Fatalf("错误收尾事件应带 errMsg、cancelled=false，得 %+v", finished)
		}
	})
}

// TestSwitchCancelWithoutRun 没跑过 / 已收口的一轮：cancelled=false（不是调用失败）。
func TestSwitchCancelWithoutRun(t *testing.T) {
	sources := []models.BookSource{testSource("https://a.test", "a", "", true)}
	_, reading := newSwitchTestHub(t, sources, nil, nil)

	if got := reading.SwitchCancel(); got["cancelled"] != false {
		t.Fatalf("从未跑过应回 cancelled=false，得 %v", got)
	}

	reading.switchSearchOne = func(ctx context.Context, source *models.BookSource, keyword string,
		page int) ([]*models.BookItem, error) {
		return nil, nil
	}
	reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 91, "斗破", "")
	if got := reading.SwitchCancel(); got["cancelled"] != false {
		t.Fatalf("已收口的一轮应回 cancelled=false，得 %v", got)
	}
	snapshot := reading.SwitchSnapshot()
	if snapshot["switch_id"] != "91" || snapshot["running"] != false {
		t.Fatalf("快照应回放最近一轮（running=false），得 %v", snapshot)
	}
}

func TestSwitchSnapshotIdleShape(t *testing.T) {
	_, reading := newSwitchTestHub(t, []models.BookSource{}, nil, nil)
	snapshot := reading.SwitchSnapshot()
	if len(snapshot) != len(IdleSwitchRun()) {
		t.Fatalf("空轮快照应与 IdleSwitchRun 同键数，得 %d", len(snapshot))
	}
	if snapshot["switch_id"] != "" || snapshot["running"] != false || snapshot["total"] != 0 {
		t.Fatalf("空轮形状错：%v", snapshot)
	}
	if _, ok := snapshot["candidates"].([]map[string]interface{}); !ok {
		t.Fatalf("空轮 candidates 应是空数组，得 %T", snapshot["candidates"])
	}
}

// ---------------------------------------------------------------------------
// 种子与写回
// ---------------------------------------------------------------------------

func TestSwitchSeedsFromStore(t *testing.T) {
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储失败：%v", err)
	}
	defer storage.Close()
	_, reading := newSwitchTestHub(t, []models.BookSource{}, nil, storage)

	store := NewSearchBookStore(storage)
	books := []*models.BookItem{
		switchMatchBook("斗破", "天蚕土豆", "https://s1.test"),
		switchMatchBook("斗破", "天蚕土豆", "https://s2.test"),
		switchMatchBook("斗破", "别家作者", "https://s3.test"),
		switchMatchBook("斗破同人", "天蚕土豆", "https://s4.test"),
	}
	if err := store.Insert(books); err != nil {
		t.Fatalf("种子落库失败：%v", err)
	}

	// 作者带筛：只回同名同作者的 2 条，按 (origin_order, book_url) 排。
	seeds := reading.SwitchSeeds(&models.BookItem{Name: "斗破", Author: "天蚕土豆"})
	if len(seeds) != 2 {
		t.Fatalf("同名同作者应回 2 条种子，得 %d：%v", len(seeds), seeds)
	}
	if seeds[0]["origin"] != "https://s1.test" || seeds[1]["origin"] != "https://s2.test" {
		t.Fatalf("种子按 book_url 序应 s1→s2，得 %v/%v", seeds[0]["origin"], seeds[1]["origin"])
	}
	if len(seeds[0]) != 35 {
		t.Fatalf("种子行应是 35 键快照，得 %d", len(seeds[0]))
	}

	// 作者为空 = 同名全要（3 条；同人书不算）。
	if seeds := reading.SwitchSeeds(&models.BookItem{Name: "斗破"}); len(seeds) != 3 {
		t.Fatalf("不带作者应回同名 3 条，得 %d", len(seeds))
	}
	// 空名不回全库。
	if seeds := reading.SwitchSeeds(&models.BookItem{}); len(seeds) != 0 {
		t.Fatalf("空名应回空，得 %d", len(seeds))
	}
	if seeds := reading.SwitchSeeds(nil); len(seeds) != 0 {
		t.Fatalf("nil 书应回空，得 %d", len(seeds))
	}
}

func TestApplySwitchWriteBack(t *testing.T) {
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储失败：%v", err)
	}
	defer storage.Close()
	bus := core.NewEventBus()

	oldBookURL := "https://old.test/book/1"
	newBookURL := "https://new.test/book/1"
	readingJSON := `{"chapter_url":"https://old.test/c/9","chapter_name":"第9章","chapter_index":8,"scroll_offset":123.5,"scroll_ratio":0.75,"chapter_pos":10,"font_size":18,"last_read_at":1790000000}`
	if _, err := storage.Execute(
		`INSERT INTO bookshelf (book_url, name, author, kind, cover_url, source_url, in_bookshelf, reading, vars, payload, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, 1, ?, '{}', '{}', 0)`,
		oldBookURL, "斗破", "天蚕土豆", "玄幻", "https://img/old.jpg", "https://old.test", readingJSON); err != nil {
		t.Fatalf("铺书架行失败：%v", err)
	}

	newSource := models.BookSource{BookSourceURL: "https://new.test", BookSourceName: "新源", Enabled: true}
	hub, reading := newSwitchTestHub(t, []models.BookSource{newSource}, bus, storage)
	hub.bookshelfSvc = NewBookshelfService(storage, bus)

	record := map[string]interface{}{"book_url": oldBookURL}
	candidate := switchMatchBook("斗破", "天蚕土豆", "https://new.test")
	candidate.URL = newBookURL
	candidate.CoverURL = "https://img/new.jpg"

	// 1) 成功写回：url/source_url 换新，章节定位清空、按章序号进度保留。
	result, err := reading.ApplySwitch(record, "https://new.test", candidate)
	if err != nil {
		t.Fatalf("写回不应报错：%v", err)
	}
	if result["applied"] != true || result["source_url"] != "https://new.test" {
		t.Fatalf("写回结果形状错：%v", result)
	}
	var gotURL, gotSourceURL, gotReading string
	if err := storage.QueryRow("SELECT book_url, source_url, reading FROM bookshelf WHERE book_url = ?",
		newBookURL).Scan(&gotURL, &gotSourceURL, &gotReading); err != nil {
		t.Fatalf("新行应存在：%v", err)
	}
	if gotSourceURL != "https://new.test" {
		t.Fatalf("source_url 应换新，得 %s", gotSourceURL)
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(gotReading), &parsed); err != nil {
		t.Fatalf("reading 应是 JSON：%v", err)
	}
	if parsed["chapter_url"] != "" || parsed["scroll_offset"] != 0.0 || parsed["scroll_ratio"] != 0.0 {
		t.Fatalf("旧源章节定位应清空，得 %v", parsed)
	}
	if parsed["chapter_index"] != 8.0 || parsed["font_size"] != 18.0 || parsed["chapter_name"] != "第9章" {
		t.Fatalf("按章序号的进度应原样保留，得 %v", parsed)
	}

	// 2) 冲突：目标 url 已有另一行 → applied=false，原行不动。
	otherURL := "https://other.test/book/2"
	if _, err := storage.Execute(
		`INSERT INTO bookshelf (book_url, name, author, in_bookshelf, reading, vars, payload, updated_at)
		 VALUES (?, ?, ?, 1, '{}', '{}', '{}', 0)`, otherURL, "别的书", "别人"); err != nil {
		t.Fatalf("铺冲突行失败：%v", err)
	}
	conflictBook := switchMatchBook("斗破", "天蚕土豆", "https://new.test")
	conflictBook.URL = otherURL
	result, err = reading.ApplySwitch(map[string]interface{}{"bookUrl": newBookURL}, "https://new.test", conflictBook)
	if err != nil {
		t.Fatalf("冲突不应报错：%v", err)
	}
	if result["applied"] != false {
		t.Fatalf("url 冲突应回 applied=false，得 %v", result)
	}
	var stillThere string
	if err := storage.QueryRow("SELECT book_url FROM bookshelf WHERE book_url = ?", newBookURL).Scan(&stillThere); err != nil {
		t.Fatalf("冲突后原行应原地保留：%v", err)
	}

	// 3) 源不在库里 → 报错（不折成 false）。
	if _, err := reading.ApplySwitch(record, "https://missing.test", candidate); err == nil ||
		!strings.Contains(err.Error(), "书源不存在") {
		t.Fatalf("缺失源应报「书源不存在」，得 %v", err)
	}

	// 4) 记录不在书架 → applied=false（源存在但旧行查无）。
	missing := switchMatchBook("斗破", "天蚕土豆", "https://new.test")
	missing.URL = "https://new.test/book/404"
	result, err = reading.ApplySwitch(map[string]interface{}{"book_url": "https://gone.test/book/0"},
		"https://new.test", missing)
	if err != nil {
		t.Fatalf("记录缺失不应报错：%v", err)
	}
	if result["applied"] != false {
		t.Fatalf("记录缺失应回 applied=false，得 %v", result)
	}
}

// ---------------------------------------------------------------------------
// 真实链路 E2E（规则引擎 + 假源 + 事件流）
// ---------------------------------------------------------------------------

// TestSwitchScanEndToEndViaRuleEngine 走真 HTTP（httptest 假源）与真规则引擎：
// 候选排序、进度节流两个方向、finished 一条，键名 snake_case。
func TestSwitchScanEndToEndViaRuleEngine(t *testing.T) {
	titles := map[string]string{"a": "斗破", "b": "斗破同人"}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		src := r.URL.Query().Get("src")
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<html><body><div class="book">
			<a class="name" href="/book/%s">%s</a>
			<span class="author">作者%s</span></div></body></html>`, src, titles[src], src)
	}))
	defer server.Close()

	makeSource := func(id string) models.BookSource {
		return models.BookSource{
			BookSourceURL:  "https://source-" + id + ".test",
			BookSourceName: "源" + id,
			Enabled:        true,
			SearchURL:      testStrPtr(server.URL + "/search?src=" + id + "&key={{key}}&page={{page}}"),
			RuleSearch: &models.SearchRule{
				BookList: testStrPtr(".book"),
				Name:     testStrPtr("a.name@text"),
				Author:   testStrPtr("span.author@text"),
				BookURL:  testStrPtr("a.name@href"),
			},
		}
	}

	run := func(publishEvery time.Duration) (map[string]interface{}, []*core.SourceSwitchProgressEvent, []*core.SourceSwitchFinishedEvent) {
		sources := []models.BookSource{makeSource("a"), makeSource("b")}
		bus := core.NewEventBus()
		hub, reading := newSwitchTestHub(t, sources, bus, nil)
		hub.ruleEngine = engine.NewRuleEngine(httpclient.NewClient(), jsruntime.NewRuntime())
		events := &switchEvents{}
		events.subscribe(bus)
		reading.switchPublishEvery = publishEvery
		snapshot := reading.SwitchScan(context.Background(), map[string]interface{}{"name": "斗破"}, 101, "斗破", "")
		waitUntil(t, "finished 事件", func() bool { return len(events.finishedSnapshot()) >= 1 })
		time.Sleep(50 * time.Millisecond)
		return snapshot, events.progressSnapshot(), events.finishedSnapshot()
	}

	// 节流：窗口 1 小时 → 只有 done==total 那一发强发，恰好 1 帧。
	snapshot, progress, finished := run(time.Hour)
	if len(progress) != 1 {
		t.Fatalf("1 小时节流应只强发 1 帧，得 %d：%+v", len(progress), progress)
	}
	if progress[0].Done != 2 || progress[0].Total != 2 || progress[0].SwitchID != "101" {
		t.Fatalf("强发帧应 done=total=2 switch_id=101，得 %+v", progress[0])
	}

	// 候选排序：源 a 回精确书名"斗破"，必须排在"斗破同人"（源 b）前面。
	candidates, ok := snapshot["candidates"].([]map[string]interface{})
	if !ok || len(candidates) != 2 {
		t.Fatalf("应聚 2 条候选，得 %v", snapshot["candidates"])
	}
	firstBook := candidates[0]["book"].(map[string]interface{})
	if firstBook["name"] != "斗破" || candidates[0]["source_url"] != "https://source-a.test" {
		t.Fatalf("精确书名应排最前，得 %v", candidates[0])
	}
	if candidates[0]["source_name"] != "源a" {
		t.Fatalf("source_name 应是源a，得 %v", candidates[0]["source_name"])
	}
	// 事件载荷键：finished 一条、五格（switch_id/candidates/cancelled/error）。
	if len(finished) != 1 {
		t.Fatalf("应恰好一条 finished，得 %d", len(finished))
	}
	if finished[0].Error != "" || finished[0].Cancelled {
		t.Fatalf("扫完的 finished 形状错：%+v", finished[0])
	}

	// 无节流（负窗口）：每个完成源一发，帧数 == total，收口那一发必到 done==total。
	// typed handler 是并发派发，到达顺序没有保证——按值核对，不按"末帧"位置核对
	// （发布侧的顺序保证在 ring/SubscribeAll 那条路上，那才是 FFI 用的路）。
	_, progress, _ = run(-time.Nanosecond)
	if len(progress) != 2 {
		dump := make([]string, 0, len(progress))
		for _, frame := range progress {
			dump = append(dump, fmt.Sprintf("{done:%d total:%d switch:%s}", frame.Done, frame.Total, frame.SwitchID))
		}
		t.Fatalf("无节流应每源 1 帧（2 条源），得 %d：%v", len(progress), dump)
	}
	dones := map[int]int{}
	for _, frame := range progress {
		dones[frame.Done]++
	}
	if dones[1] != 1 || dones[2] != 1 {
		t.Fatalf("应恰好一发 done=1、一发 done=2（收口帧），得 %v", dones)
	}
}
