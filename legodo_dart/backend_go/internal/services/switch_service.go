package services

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"runtime/metrics"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// 换源扫描（移植 Python reading_service.py 的 switch_* 一族）。
//
// 语义逐条对齐：
//   - OrderSwitchSources：可搜过滤（复用 SearchableSources）+ 排己 + 同站优先；
//   - switchSource：有界并发 8、每源 10s 超时、单源失败只计数、"本机繁忙"与
//     "书源失败"分开计、合作式停止（取下一个源**之前**收手）、0.5s 节流进度；
//   - SwitchScan 三种收尾（扫完 / 被叫停 / 起扫前就出错）都发 finished 事件；
//   - 事件载荷键与 Python dataclass 同名（进度：switch_id/done/total/failures/
//     starved；收尾：switch_id/candidates/cancelled/error；另有 timestamp）；
//   - 快照 switch_id 是**字符串**（Python sid = str(switch_id or 0)），
//     界面按文本比对身份，按数字读会永远读到 0。

const (
	// SwitchConcurrency 换源扫描并发数（对齐 Python SWITCH_CONCURRENCY=8：
	// 参考实现那 32 路摊在 9 条 OS 线程上，而实测拐点见 Python 侧注释）。
	SwitchConcurrency = 8
	// SwitchTimeout 单源核对超时。
	SwitchTimeout = 10 * time.Second
	// SwitchBusyRatio 一次"超时"里本机占用比高于此值 ⇒ 改记"本机繁忙"。
	SwitchBusyRatio = 0.5
	// SwitchPublishInterval 进度事件节流窗口。
	SwitchPublishInterval = 500 * time.Millisecond
	// SwitchBusyReason 唯一一条"不是书源的错"的失败原因。
	SwitchBusyReason = "本机繁忙"
	// switchTimeoutReason 超时失败的用户可读原因。
	switchTimeoutReason = "超时"
)

// siteToken 书源 URL 的站点名（对齐 Python _site_token：去 www 的主机首段）。
func siteToken(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	host := strings.ToLower(parsed.Hostname())
	host = strings.TrimPrefix(host, "www.")
	if host == "" {
		return ""
	}
	return strings.Split(host, ".")[0]
}

// OrderSwitchSources 换源扫描的候选队列：过滤 + 排序，全库唯一的一条规则。
//
// 过滤走 SearchableSources（启用 + 配了 searchUrl + 分组匹配），再排掉当前源自身。
// group 为空 = 全部启用源；**分组不存在时不回退到全部**——空队列就是空队列。
// 排序：同站（host 首段相同）优先，其次书源 URL 字典序。
func OrderSwitchSources(sources []models.BookSource, ownURL, group string) []models.BookSource {
	own := strings.TrimSpace(ownURL)
	token := siteToken(own)
	pool := make([]models.BookSource, 0, len(sources))
	for _, source := range SearchableSources(sources, group) {
		if source.BookSourceURL == own {
			continue
		}
		pool = append(pool, source)
	}
	sort.SliceStable(pool, func(i, j int) bool {
		rankI, rankJ := 1, 1
		if token != "" && siteToken(pool[i].BookSourceURL) == token {
			rankI = 0
		}
		if token != "" && siteToken(pool[j].BookSourceURL) == token {
			rankJ = 0
		}
		if rankI != rankJ {
			return rankI < rankJ
		}
		return pool[i].BookSourceURL < pool[j].BookSourceURL
	})
	return pool
}

// switchFailureReason 把一次书源搜索失败归一成用户看得懂的短原因。
func switchFailureReason(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return switchTimeoutReason
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return "网络错误"
	}
	message := err.Error()
	switch {
	case strings.Contains(message, "缺少搜索规则"),
		strings.Contains(message, "未配置搜索 URL"):
		return "规则不支持"
	case strings.Contains(message, "UnsupportedJavaApi"),
		strings.Contains(message, "接口未模拟"):
		return "接口未模拟"
	}
	return "规则或解析错误"
}

// readProcessCPU 读进程累计 CPU 秒（runtime/metrics，跨平台）。
// 测试可替换以造成确定的占用比。
var readProcessCPU = func() float64 {
	samples := []metrics.Sample{{Name: "/cpu/classes/total:cpu-seconds"}}
	metrics.Read(samples)
	return samples[0].Value.Float64()
}

// switchShareStarved 占用比判据（纯函数，可单测）：等待里 CPU/墙钟 >= 0.5。
func switchShareStarved(wallSeconds, cpuSeconds float64) bool {
	if wallSeconds <= 0 {
		return false
	}
	share := cpuSeconds / wallSeconds
	if share < 0 {
		share = 0
	}
	if share > 1 {
		share = 1
	}
	return share >= SwitchBusyRatio
}

// switchStarvedCheck 一次"超时"该不该改记成本机排队。
//
// 语义适配（非照搬）：Python 读的是**单事件循环线程**的 CPU 占比（32 路只是
// 一条线程上的 32 个协程，所以占比 ≤1.0 且能测出"线程一直在替别人算"）；
// Go 的搜索是并行 goroutine，没有"那一条线程"，改读**整个进程**在这段等待里
// 烧掉的 CPU 占比。阈值同为 0.5：等待期间本机大多在算（多半是替别的源解析）
// ⇒ 这一发超时不是书源的错。测试可替换。
var switchStarvedCheck = func(wallStart time.Time, cpuStart float64) bool {
	wall := time.Since(wallStart).Seconds()
	if wall <= 0 {
		return false
	}
	return switchShareStarved(wall, readProcessCPU()-cpuStart)
}

// switchMatch 一条命中：书 + 它来自哪条源。
type switchMatch struct {
	source models.BookSource
	book   *models.BookItem
}

// RankSwitchMatches 换源列表排序：书名对得上最前，其次作者，再次书源优先级。
// 返回行形状 {source_url, source_name, book: 35 键快照}（golden switch.snapshot 同形）。
func RankSwitchMatches(targetName, targetAuthor string, matches []switchMatch) []map[string]interface{} {
	wantedName := strings.ToLower(targetName)
	wantedAuthor := strings.ToLower(targetAuthor)

	ranked := append([]switchMatch(nil), matches...)
	sort.SliceStable(ranked, func(i, j int) bool {
		a, b := ranked[i], ranked[j]
		exactA := strings.ToLower(a.book.Name) == wantedName
		exactB := strings.ToLower(b.book.Name) == wantedName
		if exactA != exactB {
			return exactA
		}
		authorA := wantedAuthor != "" && strings.Contains(strings.ToLower(a.book.Author), wantedAuthor)
		authorB := wantedAuthor != "" && strings.Contains(strings.ToLower(b.book.Author), wantedAuthor)
		if authorA != authorB {
			return authorA
		}
		if a.source.BookSourceURL != b.source.BookSourceURL {
			return a.source.BookSourceURL < b.source.BookSourceURL
		}
		return strings.ToLower(a.source.BookSourceName) < strings.ToLower(b.source.BookSourceName)
	})

	rows := make([]map[string]interface{}, 0, len(ranked))
	for _, match := range ranked {
		rows = append(rows, map[string]interface{}{
			"source_url":  match.source.BookSourceURL,
			"source_name": match.source.BookSourceName,
			"book":        BookSnapshot(match.book),
		})
	}
	return rows
}

// switchRound 一轮换源扫描的服务端状态（对齐 Python self._switch_run +
// self._switch_stop + run["finished"] 三件的合体）。
type switchRound struct {
	mu         sync.Mutex
	switchID   string
	running    bool
	cancelled  bool
	done       int
	total      int
	failures   int
	starved    int
	candidates []map[string]interface{}
	errMsg     string
	stop       *atomic.Bool
	finished   chan struct{}
}

// snapshot 纯 JSON 快照（对齐 Python switch_run_dict：摘掉内部持有者）。
func (r *switchRound) snapshot() map[string]interface{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	candidates := r.candidates
	if candidates == nil {
		candidates = []map[string]interface{}{}
	}
	return map[string]interface{}{
		"switch_id":  r.switchID,
		"running":    r.running,
		"cancelled":  r.cancelled,
		"done":       r.done,
		"total":      r.total,
		"failures":   r.failures,
		"starved":    r.starved,
		"candidates": candidates,
		"error":      r.errMsg,
	}
}

// IdleSwitchRun 没有轮次时的快照（键一个不少，界面按同一形状读）。
// 导出供 FFI 层在服务缺席时回同一形状。
func IdleSwitchRun() map[string]interface{} {
	return map[string]interface{}{
		"switch_id":  "",
		"running":    false,
		"cancelled":  false,
		"done":       0,
		"total":      0,
		"failures":   0,
		"starved":    0,
		"candidates": []map[string]interface{}{},
		"error":      "",
	}
}

// SwitchScan 起一轮换源扫描（对齐 Python switch_scan）：
// 进度发 SourceSwitchProgressEvent，三种收尾（扫完 / 被叫停 / 起扫前就出错）
// 都发 SourceSwitchFinishedEvent——不发，界面的转圈就永远停不下来。
// 已经在跑时先把那一轮合作式叫停、等它收口，再开新的。
func (s *ReadingService) SwitchScan(ctx context.Context, record map[string]interface{},
	switchID int64, keyword, group string) map[string]interface{} {
	if ctx == nil {
		ctx = context.Background()
	}
	sid := strconv.FormatInt(switchID, 10)

	stop := &atomic.Bool{}
	round := &switchRound{
		switchID: sid,
		running:  true,
		stop:     stop,
		finished: make(chan struct{}),
	}
	// 一轮只跑一条：在跑时叫停它、等它收口再开新的。检查与占位在同一把锁里
	// 完成，并发进来的两个 Scan 因此一定串行（后到者会叫停先到者）。
	for {
		s.switchMu.Lock()
		prev := s.switchRound
		if prev == nil {
			s.switchRound = round
			s.switchMu.Unlock()
			break
		}
		prev.mu.Lock()
		prevRunning, prevStop, prevDone := prev.running, prev.stop, prev.finished
		prev.mu.Unlock()
		if !prevRunning || prevStop == nil {
			s.switchRound = round
			s.switchMu.Unlock()
			break
		}
		s.switchMu.Unlock()
		prevStop.Store(true)
		<-prevDone
	}

	ownURL := recordStr(record, "source_url")
	if ownURL == "" {
		ownURL = recordStr(record, "origin")
	}
	targetName := recordStr(record, "name")
	targetAuthor := recordStr(record, "author")
	key := keyword
	if key == "" {
		key = targetName
	}

	var candidates []map[string]interface{}
	errMsg := ""
	func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				errMsg = fmt.Sprintf("PanicError: %v", recovered)
			}
		}()
		sourceSvc := s.hub.SourceService()
		if sourceSvc == nil {
			errMsg = "RuntimeError: SourceService 未就绪：换源扫描没有源可查"
			return
		}
		sources := OrderSwitchSources(sourceSvc.List(), ownURL, group)
		round.mu.Lock()
		round.total = len(sources)
		round.mu.Unlock()
		candidates = s.switchSource(ctx, key, targetName, targetAuthor, sources, stop, round)
		round.mu.Lock()
		round.candidates = candidates
		round.mu.Unlock()
	}()

	round.mu.Lock()
	round.cancelled = stop.Load()
	round.running = false
	round.stop = nil
	round.errMsg = errMsg
	finishedID := round.switchID
	finishedCandidates := round.candidates
	finishedCancelled := round.cancelled
	finishedError := round.errMsg
	round.mu.Unlock()

	close(round.finished)
	s.publishSwitchFinished(finishedID, finishedCandidates, finishedCancelled, finishedError)
	return round.snapshot()
}

// SwitchCancel 叫停在跑的换源扫描（对齐 Python switch_cancel）：
// 只置停止位不杀任务——这一轮已扫出的候选要照常排序返回。
// cancelled=false 说的是"这一发没有真叫停下什么"，不是"调用失败"。
func (s *ReadingService) SwitchCancel() map[string]interface{} {
	s.switchMu.Lock()
	round := s.switchRound
	s.switchMu.Unlock()
	if round == nil {
		return map[string]interface{}{"cancelled": false}
	}
	round.mu.Lock()
	stop := round.stop
	running := round.running
	round.mu.Unlock()
	if stop == nil || !running || stop.Load() {
		return map[string]interface{}{"cancelled": false}
	}
	stop.Store(true)
	return map[string]interface{}{"cancelled": true}
}

// SwitchSnapshot 当前/最近一轮换源扫描的回放快照（断线重连补进度用）。
func (s *ReadingService) SwitchSnapshot() map[string]interface{} {
	s.switchMu.Lock()
	round := s.switchRound
	s.switchMu.Unlock()
	if round == nil {
		return IdleSwitchRun()
	}
	return round.snapshot()
}

// ApplySwitch 换源写回（对齐 Python apply_switch）：源按 url 取回，书与书架记录
// 交给写回链路。源取不到时**抛**而不是回 false——三种失败成因（源没了 / 记录被
// 删了 / 与别的书冲突）折成一句"没换成"，界面就再也分不出该提示哪一个。
func (s *ReadingService) ApplySwitch(record map[string]interface{}, sourceURL string,
	book *models.BookItem) (map[string]interface{}, error) {
	sourceSvc := s.hub.SourceService()
	if sourceSvc == nil {
		return nil, fmt.Errorf("SourceService 未就绪")
	}
	source, ok := sourceSvc.Get(sourceURL)
	if !ok {
		return nil, fmt.Errorf("书源不存在: %s", sourceURL)
	}
	applied := false
	if shelf := s.hub.BookshelfService(); shelf != nil {
		oldURL := recordStr(record, "bookUrl")
		if oldURL == "" {
			oldURL = recordStr(record, "book_url")
		}
		if oldURL == "" {
			oldURL = recordStr(record, "url")
		}
		applied = shelf.SwitchSource(oldURL, source, book)
	}
	return map[string]interface{}{"applied": applied, "source_url": sourceURL}, nil
}

// switchSource 有界并发核对 + 候选排序（对齐 Python switch_source）。
// should_stop 在**取下一个源之前**判：在途那一发已经花掉的钱不退，但也不再
// 往网络上添新的；剩下的源不计入 completed——快照读到 done < total 才是
// "被叫停了"的形状。
func (s *ReadingService) switchSource(ctx context.Context, key, targetName, targetAuthor string,
	candidates []models.BookSource, stop *atomic.Bool, round *switchRound) []map[string]interface{} {
	wanted := strings.ToLower(strings.TrimSpace(key))
	if wanted == "" || len(candidates) == 0 {
		return []map[string]interface{}{}
	}

	workerCount := s.switchWorkerCount
	if workerCount <= 0 {
		workerCount = SwitchConcurrency
	}
	timeout := s.switchSourceTimeout
	if timeout <= 0 {
		timeout = SwitchTimeout
	}
	interval := s.switchPublishEvery
	noThrottle := false
	// 负窗口 = 这个测试口子上的"完全不节流"：Windows 上 time.Now() 粒度 ~1ms，
	// 用 1ns 冒充"无节流"会被同一次时钟刻度吞掉（E2E 实测 2 源偶发只出 1 帧）。
	if interval < 0 {
		noThrottle = true
	}
	if interval == 0 {
		interval = SwitchPublishInterval
	}

	queue := make(chan models.BookSource, len(candidates))
	for _, source := range candidates {
		queue <- source
	}
	total := len(candidates)

	var mu sync.Mutex
	matches := make([]switchMatch, 0)
	failures := map[string]int{}
	completed := 0
	lastPublish := time.Now()

	publish := func() {
		// 计数、判定、盖章同在一个临界区：Python 那边 publish 跑在单事件循环里，
		// 采样与盖章天然有序；这里拆开就会让"强发帧先盖章、后到的普通帧拿着
		// 更早采的钟"被误吞（E2E 实测 2 源偶发只出 1 帧）。
		mu.Lock()
		completed++
		now := time.Now()
		if !noThrottle && completed != total && now.Sub(lastPublish) < interval {
			mu.Unlock()
			return
		}
		lastPublish = now
		done := completed
		starved := failures[SwitchBusyReason]
		sum := 0
		for _, count := range failures {
			sum += count
		}
		mu.Unlock()

		failureCount := sum - starved
		round.mu.Lock()
		round.done = done
		round.total = total
		round.failures = failureCount
		round.starved = starved
		round.mu.Unlock()
		s.publishSwitchProgress(round.switchID, done, total, failureCount, starved)
	}

	searchOne := s.switchSearch()
	count := min(workerCount, total)
	var wg sync.WaitGroup
	for i := 0; i < count; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				if stop.Load() {
					return
				}
				var source models.BookSource
				select {
				case source = <-queue:
				default:
					return
				}
				wallStart := time.Now()
				cpuStart := readProcessCPU()
				reqCtx, cancelReq := context.WithTimeout(ctx, timeout)
				books, err := searchOne(reqCtx, &source, key, 1)
				cancelReq()

				if err != nil {
					if ctx.Err() != nil {
						// 整轮被上层取消（引擎收尾）：对应 Python CancelledError 上抛，
						// 这一发不再计失败，也不再往队列上添新的。
						return
					}
					reason := switchFailureReason(err)
					if reason == switchTimeoutReason && switchStarvedCheck(wallStart, cpuStart) {
						reason = SwitchBusyReason
					}
					mu.Lock()
					failures[reason]++
					mu.Unlock()
				} else {
					if len(books) > 0 {
						// 落库与本轮命中无关：这次没对上的结果，下次换源还用得上。
						_ = s.ensureSearchStore().Insert(books)
					}
					mu.Lock()
					for _, book := range books {
						if book == nil {
							continue
						}
						if strings.Contains(strings.ToLower(book.Name), wanted) {
							matches = append(matches, switchMatch{source: source, book: book})
						}
					}
					mu.Unlock()
				}

				publish()
			}
		}()
	}
	wg.Wait()

	mu.Lock()
	finalMatches := append([]switchMatch(nil), matches...)
	mu.Unlock()
	return RankSwitchMatches(targetName, targetAuthor, finalMatches)
}

// switchSearch 单源搜索：默认走规则引擎（与找书同一条），测试可注入。
func (s *ReadingService) switchSearch() SearchOneFunc {
	if s.switchSearchOne != nil {
		return s.switchSearchOne
	}
	return func(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
		return s.hub.RuleEngine().SearchBook(ctx, source, keyword, page)
	}
}

// ensureSearchStore 种子库（换源落库与种子读取共用一个实例）。
func (s *ReadingService) ensureSearchStore() *SearchBookStore {
	s.searchMu.Lock()
	defer s.searchMu.Unlock()
	if s.searchStore == nil {
		s.searchStore = NewSearchBookStore(s.hub.StorageService())
	}
	return s.searchStore
}

func (s *ReadingService) publishSwitchProgress(switchID string, done, total, failures, starved int) {
	bus := s.hub.EventBus()
	if bus == nil {
		return
	}
	bus.Publish(&core.SourceSwitchProgressEvent{
		SwitchID:  switchID,
		Done:      done,
		Total:     total,
		Failures:  failures,
		Starved:   starved,
		Timestamp: core.NowSeconds(),
	})
}

func (s *ReadingService) publishSwitchFinished(switchID string, candidates []map[string]interface{},
	cancelled bool, errMsg string) {
	bus := s.hub.EventBus()
	if bus == nil {
		return
	}
	if candidates == nil {
		candidates = []map[string]interface{}{}
	}
	bus.Publish(&core.SourceSwitchFinishedEvent{
		SwitchID:   switchID,
		Candidates: candidates,
		Cancelled:  cancelled,
		Error:      errMsg,
		Timestamp:  core.NowSeconds(),
	})
}

// recordStr 从记录/快照字典里取字符串键（两种键风格都吃：snake 优先，camel 兜底
// 由调用点各自处理）。
func recordStr(record map[string]interface{}, key string) string {
	if record == nil {
		return ""
	}
	value, _ := record[key].(string)
	return value
}
