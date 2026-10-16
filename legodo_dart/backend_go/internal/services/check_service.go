package services

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// 书源检测（port 自 app/services/sources/source_check_service.py +
// app/services/check_service.py）：单源 / 批量（浅遍 + 按限额补验正文）/
// 停止，逐源发布九态 CheckFinishedEvent。
//
// 批量的形状：自动分档（DEPTH_AUTO）——浅遍（list）扫全书，补验正文的完整档
// （full）只吃**这一轮开跑前就欠着**的那一片（tiered_check.PerRoundDeepLimit），
// 浅遍当场排进队列的源留给后续轮次。一条源在一轮里只被派发一次。
//
// 与 Python 的已知语义差异（都有注释就地说明）：
//   - 主站探测（probe）用 Go 原生 GET，Python 的 aiohttp 会话语义由 httpclient 承担；
//   - `unsupported_java_packages` 如实回空数组——Go 的 JS 模拟面没有等价扫描器，
//     这个字段是"接口缺口清单"的读数，没扫描器就别编（golden 里普通源也是 []）；
//   - explore/reading 的 browser 渲染判定沿用 Python 的键面判据，但 Go 引擎
//     恒无浏览器回退（hasBrowser=false），webView 阶段照 Python 报 needs_browser。

const (
	// CheckDepthFull 完整档：一路验到正文。
	CheckDepthFull = "full"
	// CheckDepthList 浅档：验到列表链路就收（九态结论停在 search_only /
	// explore_only / list_usable 那一层，不会把"没验过"说成"验过不行"）。
	CheckDepthList = "list"
	// CheckDepthAuto 批量检测那一轮用的高度：两遍都跑，谁进第二遍由判据定。
	CheckDepthAuto = "auto"

	checkConcurrency       = 8
	checkMaxConcurrency    = 16
	checkTagCategories     = 6
	checkTopBooksPerChain  = 3
	checkProbeHeadBytes    = 8192
	checkErrorTextLimit    = 180
	sourceTagTimeout       = 20 * time.Second
	readingChainTimeout    = 15 * time.Second
	readingChainBudget     = 30 * time.Second
	sourceCheckBudget      = 45 * time.Second
	blockedProbeBudget     = 1500 * time.Millisecond
	probeJoinBudget        = 3 * time.Second
	checkProbeDefaultClock = 12 * time.Second
)

var hostUnreachableKinds = map[string]bool{
	"network_error": true, "timeout": true, "js_error": true, "rate_limited": true,
}

var uncertainReadingKinds = map[string]bool{
	"timeout": true, "rate_limited": true, "network_error": true,
	"js_error": true, "unsupported": true,
}

var retryableChainErrors = map[string]bool{
	"正文解析结果为空": true, "目录中没有可读取章节": true,
}

// errCheckCancelled 一轮批量检测被叫停（Python 侧是 CancelledError 穿过
// check_all：summary/tiering 都不写，快照只留已发布的那些行）。
var errCheckCancelled = errors.New("check run cancelled")

// CheckResult 单条源的检测结果（键面照 golden `check.source.json` 的 31 键）。
type CheckResult struct {
	Idx    int
	Name   string
	RawURL string
	tier   string

	Status         string
	StatusCode     *int
	ResponseTimeMs *float64
	Error          *string

	SearchStatus  string
	BookCount     *int
	SearchKeyword string
	SearchMethod  string
	SearchError   *string

	ReadingStatus       string
	ReadingStage        string
	ReadingError        *string
	ChapterCount        *int
	SampleBook          *string
	SampleChapter       *string
	SampleChapterURLs   []string
	ReadingTried        int
	ReadingCheckSkipped *string

	CategoryResults []map[string]interface{}
	TagCheckTimeout bool
	TagCheckSkipped bool
	TagTotal        int
	TagValid        int
	TagInvalid      int

	// Go 侧无等价扫描器，恒为空数组（见文件头注释）。
	UnsupportedJavaPackages []string

	CheckState    string
	Usable        bool
	AutoDisabled  bool
	DisableReason *string
}

// ToMap 出参形状（Python 结果 dict 的 snake_case 键面）。
// 异常路径（stage_error）比正常路径少几个键，这里统一补成 null——
// Dart 侧对缺键/空值同解，多几个 null 不影响解析。
func (r *CheckResult) ToMap() map[string]interface{} {
	urls := r.SampleChapterURLs
	if urls == nil {
		urls = []string{}
	}
	packages := r.UnsupportedJavaPackages
	if packages == nil {
		packages = []string{}
	}
	rows := r.CategoryResults
	if rows == nil {
		rows = []map[string]interface{}{}
	}
	out := map[string]interface{}{
		"idx": r.Idx, "name": r.Name, "raw_url": r.RawURL,
		"status": r.Status, "status_code": intValue(r.StatusCode),
		"response_time_ms": floatValue(r.ResponseTimeMs), "error": strValue(r.Error),
		"search_status": r.SearchStatus, "book_count": intValue(r.BookCount),
		"search_keyword": r.SearchKeyword, "search_method": r.SearchMethod,
		"search_error":   strValue(r.SearchError),
		"reading_status": r.ReadingStatus, "reading_stage": r.ReadingStage,
		"reading_error": strValue(r.ReadingError), "chapter_count": intValue(r.ChapterCount),
		"sample_book": strValue(r.SampleBook), "sample_chapter": strValue(r.SampleChapter),
		"sample_chapter_urls": urls, "reading_tried": r.ReadingTried,
		"reading_check_skipped": strValue(r.ReadingCheckSkipped),
		"category_results":      rows, "tag_check_timeout": r.TagCheckTimeout,
		"tag_check_skipped": r.TagCheckSkipped, "tag_total": r.TagTotal,
		"tag_valid": r.TagValid, "tag_invalid": r.TagInvalid,
		"unsupported_java_packages": packages,
		"check_state":               r.CheckState, "usable": r.Usable,
		"auto_disabled": r.AutoDisabled,
	}
	if r.AutoDisabled {
		out["disable_reason"] = strValue(r.DisableReason)
	}
	return out
}

type probeOutcome struct {
	Status         string
	StatusCode     *int
	ResponseTimeMs *float64
	Error          *string
}

type searchOutcome struct {
	Status  string
	Books   []*models.BookItem
	Count   *int
	Keyword string
	Error   *string
}

type chainOutcome struct {
	Status            string
	Stage             string
	Error             *string
	ChapterCount      *int
	SampleBook        *string
	SampleChapter     *string
	SampleChapterURLs []string
	Tried             int
	Skipped           *string
}

type exploreCategory struct {
	Title string
	URL   string
}

// CheckEngine 检测流水线要用的规则引擎面（测试可注入替身）。
type CheckEngine interface {
	Search(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error)
	Explore(ctx context.Context, source *models.BookSource, searchURL, category string) ([]*models.BookItem, error)
	BookInfo(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error)
	Chapters(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error)
	Content(ctx context.Context, source *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error)
}

type ruleCheckEngine struct{ re *engine.RuleEngine }

func (r ruleCheckEngine) Search(ctx context.Context, source *models.BookSource, keyword string, page int) ([]*models.BookItem, error) {
	return r.re.SearchBook(ctx, source, keyword, page)
}

func (r ruleCheckEngine) Explore(ctx context.Context, source *models.BookSource, searchURL, category string) ([]*models.BookItem, error) {
	return r.re.BrowseBooks(ctx, source, searchURL, category)
}

func (r ruleCheckEngine) BookInfo(ctx context.Context, source *models.BookSource, bookURL string) (*models.BookItem, error) {
	return r.re.GetBookInfo(ctx, source, bookURL)
}

func (r ruleCheckEngine) Chapters(ctx context.Context, source *models.BookSource, tocURL string) ([]*models.Chapter, error) {
	return r.re.GetChapters(ctx, source, tocURL)
}

func (r ruleCheckEngine) Content(ctx context.Context, source *models.BookSource, chapter *models.Chapter) (*models.ChapterContent, error) {
	return r.re.GetContent(ctx, source, chapter)
}

// checkRun 一轮批量检测的回放快照（读写都持 s.mu）。
type checkRun struct {
	total     int
	running   bool
	cancelled bool
	depth     string
	summary   map[string]interface{}
	tiering   map[string]interface{}
	results   []map[string]interface{}
}

// SourceCheckService 书源检测服务。
type SourceCheckService struct {
	base      *core.BaseService
	hub       *ServiceHub
	sourceSvc *SourceService
	settings  *core.SettingsService
	bus       *core.EventBus

	timeout     time.Duration
	concurrency int
	autoDisable bool

	// 注入点（测试用）：engine 为 nil 时走 hub 规则引擎。
	engine CheckEngine

	mu        sync.Mutex
	run       *checkRun
	runDone   chan struct{}
	cancelRun context.CancelFunc
	deepQueue *DeepQueue
}

// NewCheckService 建检测服务（协作者从 hub 现取，和 ReadingService 同一形状）。
func NewCheckService(h *ServiceHub) *SourceCheckService {
	s := &SourceCheckService{
		base:        core.NewBaseService("SourceCheckService", "SourceService", "SettingsService"),
		hub:         h,
		sourceSvc:   h.SourceService(),
		settings:    h.SettingsService(),
		bus:         h.EventBus(),
		timeout:     checkProbeDefaultClock,
		concurrency: checkConcurrency,
		autoDisable: false,
		deepQueue:   NewDeepQueue(),
	}
	s.base.SetOnStart(func() error { return nil })
	s.base.SetOnStop(func() error { return nil })
	return s
}

// Start initializes the service.
func (s *SourceCheckService) Start() error { return s.base.Start() }

// Stop releases resources.
func (s *SourceCheckService) Stop() error { return s.base.Stop() }

// GetSourceService returns the associated source service.
func (s *SourceCheckService) GetSourceService() *SourceService { return s.sourceSvc }

// SetEngine 注入规则引擎替身（测试用；nil 恢复走 hub）。
func (s *SourceCheckService) SetEngine(engine CheckEngine) { s.engine = engine }

// DeepQueue 暴露队列（测试与快照读它）。
func (s *SourceCheckService) DeepQueue() *DeepQueue { return s.deepQueue }

func (s *SourceCheckService) engineOrDefault() CheckEngine {
	if s.engine != nil {
		return s.engine
	}
	if s.hub != nil && s.hub.RuleEngine() != nil {
		return ruleCheckEngine{re: s.hub.RuleEngine()}
	}
	return nil
}

// ---------------------------------------------------------------------------
// 对外入口
// ---------------------------------------------------------------------------

// RunAll 起一轮批量检测（阻塞到收尾；FFI 侧用 go 起它）。
//
// 已有批量在跑时**不再派第二遍全量联网**，而是接到那一轮的收尾上（Python
// check_all 的同款行为）。返回本轮 summary；被叫停时 summary 不写（nil 回 {}）。
func (s *SourceCheckService) RunAll(groups []string) (map[string]interface{}, error) {
	s.applySettings()
	if s.isRunning() {
		s.awaitRunFinished()
		snap := s.Snapshot()
		if summary, ok := snap["summary"].(map[string]interface{}); ok {
			return summary, nil
		}
		return map[string]interface{}{}, nil
	}
	sources := s.collectSources(groups)
	ctx, cancel := context.WithCancel(context.Background())
	run := s.beginRun(len(sources), CheckDepthAuto, cancel)
	defer cancel()
	client := httpclient.NewClient()
	defer client.Close()

	var results []*CheckResult
	var tiering map[string]interface{}
	if len(sources) == 0 {
		tiering = s.emptyTiering()
	} else {
		var cancelled bool
		results, tiering, cancelled = s.runTiered(ctx, sources, run, client)
		if cancelled {
			s.endRun(run)
			return map[string]interface{}{}, errCheckCancelled
		}
	}

	usable, blocked := 0, 0
	rows := make([]interface{}, 0, len(results))
	for _, result := range results {
		if result.Usable {
			usable++
		}
		if result.CheckState == "blocked" {
			blocked++
		}
		rows = append(rows, result.ToMap())
	}
	summary := map[string]interface{}{
		"total": len(results), "finished": len(results),
		"usable": usable, "blocked": blocked,
		"results": rows, "tiering": tiering,
	}
	s.mu.Lock()
	run.tiering = tiering
	run.summary = summary
	s.mu.Unlock()
	s.endRun(run)
	return summary, nil
}

// CheckSingle 只检这一条源（编辑器/自愈那条线问"这一条能不能用"）。
func (s *SourceCheckService) CheckSingle(sourceJSON map[string]interface{}, depth string) (map[string]interface{}, error) {
	if depth == "" {
		depth = CheckDepthFull
	}
	if depth != CheckDepthFull && depth != CheckDepthList {
		return nil, fmt.Errorf("未知的检测深度：%q", depth)
	}
	source, err := checkSourceFromJSON(sourceJSON)
	if err != nil {
		return nil, err
	}
	s.applySettings()
	client := httpclient.NewClient()
	defer client.Close()
	result := s.checkOneSource(context.Background(), *source, depth, client)
	result.Idx = 1
	result.tier = depth
	s.publishFinished(result, nil, 1, 1)
	return result.ToMap(), nil
}

// Snapshot 出参形状照 golden `check.snapshot.json` 顶层 8 键。
func (s *SourceCheckService) Snapshot() map[string]interface{} {
	s.mu.Lock()
	run := s.run
	running, cancelled := false, false
	total := 0
	depth := CheckDepthAuto
	var summary interface{}
	tiering := s.emptyTiering()
	var rows []map[string]interface{}
	if run != nil {
		running, cancelled, total, depth = run.running, run.cancelled, run.total, run.depth
		// 落成无类型 nil：取消那轮 summary 不写，wire 上必须是 null
		// （typed-nil map 经 interface 一绕就不等于 nil 了）。
		if run.summary != nil {
			summary = run.summary
		}
		if run.tiering != nil {
			tiering = run.tiering
		}
		rows = append([]map[string]interface{}(nil), run.results...)
	}
	s.mu.Unlock()

	results := make([]interface{}, len(rows))
	for index, row := range rows {
		results[index] = row
	}
	queueRows := s.deepQueue.Snapshot()
	deepQueue := make([]interface{}, len(queueRows))
	for index, row := range queueRows {
		deepQueue[index] = row
	}
	return map[string]interface{}{
		"running": running, "cancelled": cancelled, "total": total,
		"depth": depth, "tiering": tiering, "deep_queue": deepQueue,
		"summary": summary, "results": results,
	}
}

// Cancel 叫停正在跑的批量检测（协作停止：不再派发新源，在飞的请求尽快收敛）。
func (s *SourceCheckService) Cancel() {
	s.mu.Lock()
	cancel := s.cancelRun
	run := s.run
	running := run != nil && run.running
	s.mu.Unlock()
	if cancel != nil && running {
		cancel()
	}
}

// ---------------------------------------------------------------------------
// 一轮批量：浅遍 + 补验切片
// ---------------------------------------------------------------------------

func (s *SourceCheckService) runTiered(ctx context.Context, sources []models.BookSource,
	run *checkRun, client *httpclient.ClientSession) ([]*CheckResult, map[string]interface{}, bool) {
	inScope := make(map[string]bool, len(sources))
	for _, source := range sources {
		inScope[source.BookSourceURL] = true
	}
	s.deepQueue.Prune()
	carried := make(map[string]string)
	for rawURL, reason := range s.deepQueue.Pending() {
		if inScope[rawURL] {
			carried[rawURL] = reason
		}
	}
	// 本轮的欠账 = 上一轮带着的 + 静态判据（地址形状），静态更具体所以覆写它。
	staticReasons := make(map[string]string)
	for index := range sources {
		if reason := StaticDeepReason(&sources[index]); reason != "" {
			staticReasons[sources[index].BookSourceURL] = reason
		}
	}
	reasons := make(map[string]string, len(carried)+len(staticReasons))
	for rawURL, reason := range carried {
		reasons[rawURL] = reason
	}
	for rawURL, reason := range staticReasons {
		reasons[rawURL] = reason
	}
	for rawURL, reason := range reasons {
		s.deepQueue.Add(rawURL, reason)
	}

	limit := PerRoundDeepLimit(len(sources))
	deepSources := s.deepQueue.Select(sources, limit)
	deepURLs := make(map[string]bool, len(deepSources))
	for _, source := range deepSources {
		deepURLs[source.BookSourceURL] = true
	}
	shallowSources := make([]models.BookSource, 0, len(sources))
	for _, source := range sources {
		if !deepURLs[source.BookSourceURL] {
			shallowSources = append(shallowSources, source)
		}
	}
	carriedInSlice := 0
	for _, source := range deepSources {
		if _, ok := carried[source.BookSourceURL]; ok {
			carriedInSlice++
		}
	}
	tiering := map[string]interface{}{
		"plan_total": len(sources), "shallow": len(shallowSources),
		"deep": len(deepSources), "slice_limit": limit,
		"carried": carriedInSlice, "by_reason": map[string]interface{}{},
	}

	results := []*CheckResult{}
	if len(shallowSources) > 0 {
		results = s.runChecks(ctx, shallowSources, run, CheckDepthList,
			len(sources), 0, client)
	}
	if ctx.Err() != nil {
		s.markCancelled(run)
		return nil, nil, true
	}

	// 浅遍跑完的结论要送回队列：给了明确结论的注销旧债、没结论的排队补验。
	// （静态判据排进来的不注销——那条债要的是"验正文"，列表段的结论回答不了它。）
	deferred := 0
	for _, result := range results {
		rawURL := result.RawURL
		reason := ShallowDeepReason(result)
		if reason == "" {
			if _, wasCarried := carried[rawURL]; wasCarried {
				if _, isStatic := staticReasons[rawURL]; !isStatic {
					s.deepQueue.Drop(rawURL)
				}
			}
			continue
		}
		if _, isStatic := staticReasons[rawURL]; isStatic {
			continue
		}
		if _, seen := reasons[rawURL]; !seen {
			deferred++
		}
		reasons[rawURL] = reason
		s.deepQueue.Add(rawURL, reason)
	}

	if len(deepSources) > 0 {
		deepResults := s.runChecks(ctx, deepSources, run, CheckDepthFull,
			len(sources), len(results), client)
		if ctx.Err() != nil {
			s.markCancelled(run)
			return nil, nil, true
		}
		results = append(results, deepResults...)
		// 深遍跑过的条目从队列里摘掉；只有一时判不出结论的（unknown）留下重试。
		for _, result := range deepResults {
			if result.CheckState == "unknown" {
				s.deepQueue.NoteAttempt(result.RawURL)
			} else {
				s.deepQueue.Drop(result.RawURL)
			}
		}
	}

	tiering["deferred"] = deferred
	// 按原因归组的是**整本欠账**（含这一轮没吃到的那一片），不是切片。
	byReason := map[string]interface{}{}
	counts := map[string]int{}
	for _, reason := range reasons {
		counts[reason]++
	}
	for reason, count := range counts {
		byReason[reason] = count
	}
	tiering["by_reason"] = byReason
	tiering["queued"] = len(s.deepQueue.Pending())

	for _, result := range results {
		if deepURLs[result.RawURL] {
			result.tier = CheckDepthFull
		} else {
			result.tier = CheckDepthList
		}
	}
	order := make(map[string]int, len(sources))
	for index, source := range sources {
		order[source.BookSourceURL] = index
	}
	sort.SliceStable(results, func(i, j int) bool {
		oi, oki := order[results[i].RawURL]
		oj, okj := order[results[j].RawURL]
		if !oki {
			oi = len(order)
		}
		if !okj {
			oj = len(order)
		}
		return oi < oj
	})
	for index, result := range results {
		result.Idx = index + 1
	}
	return results, tiering, false
}

// runChecks 跑一遍流水线；逐源结果同步记进回放快照。
//
// settledBefore 是上一遍已经定论的条数（进度分母是**本轮计划源数** totalPlan，
// 两遍合起来走满 0→1）。协作停止：ctx 取消后不再派发新源，在飞的那条结果丢弃
// （Python 侧是 CancelledError 把整批结果扔掉，快照只留已经发布的行）。
func (s *SourceCheckService) runChecks(ctx context.Context, sources []models.BookSource,
	run *checkRun, depth string, totalPlan, settledBefore int,
	client *httpclient.ClientSession) []*CheckResult {
	if len(sources) == 0 {
		return nil
	}
	workers := s.concurrency
	if workers > checkMaxConcurrency {
		workers = checkMaxConcurrency
	}
	if workers < 1 {
		workers = 1
	}
	if workers > len(sources) {
		workers = len(sources)
	}
	queue := make(chan models.BookSource, len(sources))
	for _, source := range sources {
		queue <- source
	}
	close(queue)

	var mu sync.Mutex
	results := make([]*CheckResult, 0, len(sources))
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for source := range queue {
				if ctx.Err() != nil {
					continue
				}
				result := s.checkOneSource(ctx, source, depth, client)
				if ctx.Err() != nil {
					continue
				}
				mu.Lock()
				results = append(results, result)
				done := settledBefore + len(results)
				s.publishFinished(result, run, done, totalPlan)
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	return results
}

func (s *SourceCheckService) publishFinished(result *CheckResult, run *checkRun, done, total int) {
	stateClass := result.CheckState
	if stateClass == "" {
		stateClass = "unknown"
	}
	summary := checkSummaryLine(result)
	if run != nil {
		urls := make([]string, 0, len(result.SampleChapterURLs))
		for _, rawURL := range result.SampleChapterURLs {
			if rawURL != "" {
				urls = append(urls, rawURL)
			}
		}
		run.results = append(run.results, map[string]interface{}{
			"source_id": result.RawURL, "state_class": stateClass,
			"summary": summary, "sample_chapter_urls": urls,
		})
	}
	if s.bus == nil {
		return
	}
	s.bus.Publish(&core.CheckFinishedEvent{
		SourceID: result.RawURL, StateClass: stateClass, Summary: summary,
		Done: done, Total: total, Timestamp: core.NowSeconds(),
	})
}

// checkSummaryLine 从单源结果拼一句人类可读摘要（CheckFinishedEvent.summary）。
func checkSummaryLine(result *CheckResult) string {
	state := result.CheckState
	if state == "" {
		state = "unknown"
	}
	name := result.Name
	if name == "" {
		name = result.RawURL
	}
	if name == "" {
		name = "(未命名)"
	}
	line := name + "：" + state
	errText := ""
	for _, candidate := range []*string{result.ReadingError, result.Error, result.SearchError} {
		if candidate != nil && strings.TrimSpace(*candidate) != "" {
			errText = strings.TrimSpace(*candidate)
			break
		}
	}
	if errText != "" {
		line += "（" + errText + "）"
	}
	// "没试正文"和"试了失败"在九态里都落进列表那一级，界面上必须点出来是哪种。
	if result.tier == CheckDepthList ||
		(result.ReadingCheckSkipped != nil && *result.ReadingCheckSkipped == CheckDepthList) {
		line += "（本轮快速检测，未验正文）"
	}
	return line
}

// ---------------------------------------------------------------------------
// 单源流水线：probe / search / tags / chain / 分类
// ---------------------------------------------------------------------------

func (s *SourceCheckService) checkOneSource(ctx context.Context, source models.BookSource,
	depth string, client *httpclient.ClientSession) *CheckResult {
	listDepth := depth == CheckDepthList
	result := &CheckResult{Name: source.BookSourceName, RawURL: source.BookSourceURL}
	deadline := time.Now().Add(sourceCheckBudget)

	probeCh := make(chan probeOutcome, 1)
	go func() {
		probeCh <- s.probeSource(ctx, &source, s.timeout, client)
	}()

	engineInst := s.engineOrDefault()
	var probe probeOutcome
	var probeReady bool
	var search searchOutcome
	tagRows := []map[string]interface{}{}
	tagTimeout, tagSkipped := false, false

	// 探测与规则阶段并发；本机拦截响应在毫秒级，给它一个短预算就能省掉
	// 后面整条规则链的超时（见 Python _locally_blocked_probe）。
	if early, ok := awaitBlockedProbe(probeCh, blockedProbeBudget); ok && early.Status == "blocked" {
		probe, probeReady = early, true
		search = searchOutcome{Status: "skip"}
		tagSkipped = true
		applyChain(result, chainStub("probe_blocked"))
	} else {
		if ok {
			probe, probeReady = early, true
		}
		search = s.engineSearchCheck(ctx, &source, engineInst,
			remainingFromDeadline(deadline, s.timeout+3*time.Second))
		if source.EnabledExplore && strings.TrimSpace(strValue(source.ExploreURL)) != "" {
			if hostUnreachableKinds[search.Status] {
				tagSkipped = true
			} else {
				tagCtx, cancelTag := context.WithTimeout(ctx, remainingFromDeadline(deadline, sourceTagTimeout))
				rows, err := s.checkCategories(tagCtx, &source, engineInst,
					minDuration(s.timeout, 10*time.Second), checkTagCategories, true)
				tagExpired := tagCtx.Err() != nil
				cancelTag()
				if err != nil {
					if errors.Is(err, context.DeadlineExceeded) || tagExpired {
						tagTimeout = true
					} else {
						s.applyProbeSearch(result, probe, probeReady, probeCh, search)
						return stageErrorResult(&source, err)
					}
				} else {
					tagRows = rows
				}
			}
		}
		candidates := append([]*models.BookItem{}, search.Books...)
		for _, row := range tagRows {
			if books, ok := row["books"].([]*models.BookItem); ok {
				candidates = append(candidates, books...)
			}
			delete(row, "books")
			if pages, ok := row["page_results"].([]map[string]interface{}); ok {
				for _, page := range pages {
					delete(page, "books")
				}
			}
		}
		if listDepth {
			// 快速深度到这一段就收：阅读链是唯一会翻倍的段。
			applyChain(result, chainStub(CheckDepthList))
		} else {
			applyChain(result, s.readingChainCheck(ctx, &source, engineInst, candidates,
				remainingFromDeadline(deadline, readingChainBudget)))
		}
		if !probeReady {
			probe = awaitProbe(probeCh, probeJoinBudget)
			probeReady = true
		}
	}

	result.Status = probe.Status
	result.StatusCode = probe.StatusCode
	result.ResponseTimeMs = probe.ResponseTimeMs
	result.Error = probe.Error
	result.SearchStatus = search.Status
	result.BookCount = search.Count
	result.SearchKeyword = search.Keyword
	result.SearchMethod = "rule-engine"
	result.SearchError = search.Error
	result.CategoryResults = tagRows
	result.TagCheckTimeout = tagTimeout
	result.TagCheckSkipped = tagSkipped
	result.UnsupportedJavaPackages = []string{}
	result.TagTotal = len(tagRows)
	result.TagValid = 0
	for _, row := range tagRows {
		if valid, ok := row["valid"].(bool); ok && valid {
			result.TagValid++
		}
	}
	result.TagInvalid = result.TagTotal - result.TagValid

	result.CheckState = classifyCheckResult(result)
	result.Usable = checkUsableStates[result.CheckState]
	if s.autoDisable && result.CheckState == "unusable" && source.Enabled {
		source.Enabled = false
		result.AutoDisabled = true
		reason := "搜索、发现与阅读链路均明确不可用"
		if listDepth {
			reason = "搜索与发现链路均明确不可用"
		}
		result.DisableReason = &reason
		if s.sourceSvc != nil {
			if err := s.sourceSvc.Save(&source); err != nil {
				result.AutoDisabled = false
			}
		}
	}
	return result
}

func (s *SourceCheckService) applyProbeSearch(result *CheckResult, probe probeOutcome,
	probeReady bool, probeCh <-chan probeOutcome, search searchOutcome) {
	if !probeReady {
		probe = awaitProbe(probeCh, probeJoinBudget)
	}
	result.Status = probe.Status
	result.StatusCode = probe.StatusCode
	result.ResponseTimeMs = probe.ResponseTimeMs
	result.Error = probe.Error
	result.SearchStatus = search.Status
	result.BookCount = search.Count
	result.SearchKeyword = search.Keyword
	result.SearchMethod = "rule-engine"
	result.SearchError = search.Error
	result.CheckState = "unknown"
	result.ReadingStatus = "skip"
	result.ReadingStage = "none"
	skipped := "stage_error"
	result.ReadingCheckSkipped = &skipped
}

var checkUsableStates = map[string]bool{
	"fully_usable": true, "search_only": true, "explore_only": true, "list_usable": true,
}

// stageErrorResult 流水线自身的意外错误（Python worker 的 except 分支）。
func stageErrorResult(source *models.BookSource, err error) *CheckResult {
	text := checkErrorText(err)
	return &CheckResult{
		Name: source.BookSourceName, RawURL: source.BookSourceURL,
		Status: "err", SearchStatus: "rule_error", Error: &text,
		CategoryResults:         []map[string]interface{}{},
		CheckState:              "unknown",
		ReadingStatus:           "skip",
		ReadingStage:            "none",
		ReadingCheckSkipped:     strPtr("stage_error"),
		UnsupportedJavaPackages: []string{},
	}
}

// classifyCheckResult 九态分类（port 自 check_service._classify_result）。
func classifyCheckResult(result *CheckResult) string {
	searchOK := result.SearchStatus == "ok" || result.SearchStatus == "no_results"
	exploreOK := result.TagValid > 0
	reading := result.ReadingStatus
	if reading == "needs_browser" {
		// 正文链路要渲染后才拿得到，本次没开浏览器 = 没验证过这一段。
		reading = "skip"
	}
	if reading == "ok" {
		return "fully_usable"
	}
	if uncertainReadingKinds[reading] {
		return "unknown"
	}
	// 拦截页会被当成"搜索无结果"，必须排在 search_ok 判定之前。
	if result.Status == "blocked" || result.SearchStatus == "blocked" || result.ReadingStatus == "blocked" {
		return "blocked"
	}
	// 首页被拒绝访问而规则又没解析出任何东西时，站点是拦了我们而不是书源失效。
	if result.StatusCode != nil &&
		(*result.StatusCode == 401 || *result.StatusCode == 403 || *result.StatusCode == 429) &&
		result.SearchStatus != "ok" && !exploreOK {
		return "blocked"
	}
	if searchOK || exploreOK {
		if reading == "skip" || reading == "" {
			if searchOK && !exploreOK {
				return "search_only"
			}
			if exploreOK && !searchOK {
				return "explore_only"
			}
			return "list_usable"
		}
		return "content_unavailable"
	}
	if uncertainReadingKinds[result.SearchStatus] || result.TagCheckTimeout || result.Status == "err" {
		return "unknown"
	}
	return "unusable"
}

// ---------------------------------------------------------------------------
// 阶段实现
// ---------------------------------------------------------------------------

func (s *SourceCheckService) probeSource(ctx context.Context, source *models.BookSource,
	timeout time.Duration, client *httpclient.ClientSession) probeOutcome {
	rawURL := strings.TrimSpace(source.BookSourceURL)
	if !strings.HasPrefix(rawURL, "http://") && !strings.HasPrefix(rawURL, "https://") {
		return probeOutcome{Status: "skip"}
	}
	if client == nil {
		text := "HTTP 会话未就绪"
		return probeOutcome{Status: "err", Error: &text}
	}
	started := time.Now()
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	resp, err := client.Get(requestCtx, rawURL, nil)
	elapsed := float64(time.Since(started).Nanoseconds()) / 1e6
	if err != nil {
		text := checkErrorText(err)
		return probeOutcome{Status: "err", ResponseTimeMs: &elapsed, Error: &text}
	}
	code := resp.StatusCode
	head := resp.Body
	if len(head) > checkProbeHeadBytes {
		head = head[:checkProbeHeadBytes]
	}
	if marker := blockReason(head, code, true); marker != "" {
		return probeOutcome{Status: "blocked", StatusCode: &code,
			ResponseTimeMs: &elapsed, Error: &marker}
	}
	// 首页被拒绝访问不证明主机可用；只有 2xx/3xx 才算站点应答了我们。
	if code < 400 {
		return probeOutcome{Status: "ok", StatusCode: &code, ResponseTimeMs: &elapsed}
	}
	text := fmt.Sprintf("HTTP %d", code)
	return probeOutcome{Status: "err", StatusCode: &code,
		ResponseTimeMs: &elapsed, Error: &text}
}

func (s *SourceCheckService) engineSearchCheck(ctx context.Context, source *models.BookSource,
	engineInst CheckEngine, timeout time.Duration) searchOutcome {
	keyword := "我的"
	if source.RuleSearch != nil && source.RuleSearch.CheckKeyWord != nil &&
		*source.RuleSearch.CheckKeyWord != "" {
		keyword = *source.RuleSearch.CheckKeyWord
	}
	if strings.TrimSpace(strValue(source.SearchURL)) == "" {
		return searchOutcome{Status: "skip", Keyword: keyword}
	}
	if engineInst == nil {
		text := "规则引擎未就绪"
		return searchOutcome{Status: "rule_error", Keyword: keyword, Error: &text}
	}
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	books, err := engineInst.Search(callCtx, source, keyword, 1)
	if err != nil {
		kind := checkErrorKind(err)
		text := checkErrorText(err)
		return searchOutcome{Status: kind, Keyword: keyword, Error: &text}
	}
	count := len(books)
	status := "ok"
	if count == 0 {
		status = "no_results"
	}
	return searchOutcome{Status: status, Books: books, Count: &count, Keyword: keyword}
}

// checkCategories 发现页标签校验（port 自 check_service.check_categories）。
// stop_after_valid：第一个解析成功的分类就收（连通性已有结论，剩下的样本只添延迟）。
func (s *SourceCheckService) checkCategories(ctx context.Context, source *models.BookSource,
	engineInst CheckEngine, timeout time.Duration, maxCategories int,
	stopAfterValid bool) ([]map[string]interface{}, error) {
	categories, err := s.resolveExploreCategories(source)
	if err != nil {
		return nil, err
	}
	if maxCategories > 0 && len(categories) > maxCategories {
		categories = categories[:maxCategories]
	}
	output := make([]map[string]interface{}, 0, len(categories))
	for _, category := range categories {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		pages := []int{1}
		if strings.Contains(category.URL, "{{page") || explorePagePairRe.MatchString(category.URL) {
			pages = []int{1, 2}
		}
		pageResults := make([]map[string]interface{}, 0, len(pages))
		for _, page := range pages {
			pageResults = append(pageResults,
				s.checkCategoryPage(ctx, source, engineInst, category, page, timeout))
		}
		first := pageResults[0]
		row := map[string]interface{}{
			"title": category.Title, "url": category.URL,
			"valid": first["valid"], "status_code": first["status_code"],
			"item_count": first["item_count"], "error": first["error"],
			"pagination_valid": nil, "page_results": pageResults,
			"books": first["books"],
		}
		if len(pageResults) > 1 {
			row["pagination_valid"] = pageResults[1]["valid"]
		}
		output = append(output, row)
		if stopAfterValid {
			if valid, ok := first["valid"].(bool); ok && valid {
				break
			}
		}
	}
	return output, nil
}

var explorePagePairRe = regexp.MustCompile(`<[^<>]*,[^<>]*>`)

func (s *SourceCheckService) checkCategoryPage(ctx context.Context, source *models.BookSource,
	engineInst CheckEngine, category exploreCategory, page int, timeout time.Duration) map[string]interface{} {
	row := map[string]interface{}{
		"page": page, "valid": false, "status_code": nil, "item_count": 0,
		"content_type": "rule-engine", "error": nil, "attempts": 1,
		"persistent_rate_limit": false, "books": []*models.BookItem{},
	}
	pageURL, err := resolveExploreURL(category.URL, source.BookSourceURL, page)
	if err != nil {
		text := checkErrorText(err)
		row["error"] = text
		return row
	}
	if engineInst == nil {
		row["error"] = "规则引擎未就绪"
		return row
	}
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	books, err := engineInst.Explore(callCtx, source, pageURL, category.URL)
	if err != nil {
		kind := checkErrorKind(err)
		row["error"] = kind + ": " + checkErrorText(err)
		row["persistent_rate_limit"] = kind == "rate_limited"
		return row
	}
	row["valid"] = len(books) > 0
	row["item_count"] = len(books)
	row["books"] = books
	if len(books) == 0 {
		row["error"] = "规则引擎未解析到书籍"
	}
	return row
}

// readingChainCheck 阅读链抽检（port 自 check_service._reading_chain_check）。
func (s *SourceCheckService) readingChainCheck(ctx context.Context, source *models.BookSource,
	engineInst CheckEngine, candidates []*models.BookItem, budget time.Duration) *chainOutcome {
	out := &chainOutcome{Status: "skip", Stage: "none"}
	books := make([]*models.BookItem, 0, len(candidates))
	for _, item := range candidates {
		if item != nil && item.URL != "" {
			books = append(books, item)
		}
	}
	if len(books) == 0 {
		// 没候选不代表正文链路坏了，只代表前面的段没给出可点的书。
		out.Skipped = strPtr("no_candidates")
		return out
	}
	if engineInst == nil {
		out.Skipped = strPtr("no_candidates")
		return out
	}
	browserStages := browserOnlyStages(source)
	if len(books) > checkTopBooksPerChain {
		books = books[:checkTopBooksPerChain]
	}
	sampleBook := books[0].Name
	out.SampleBook = &sampleBook
	chainDeadline := time.Now().Add(maxDuration(time.Second,
		minDuration(budget, readingChainTimeout*time.Duration(len(books)))))
	for _, book := range books {
		remaining := time.Until(chainDeadline)
		if remaining <= 0 {
			out.Status = "timeout"
			out.Error = strPtr("阅读链路抽检总耗时超限")
			break
		}
		out.Tried++
		stageTimeout := minDuration(readingChainTimeout, remaining)
		err := s.chainOneBook(ctx, source, engineInst, book, stageTimeout, out)
		if err == nil {
			return out
		}
		kind := checkErrorKind(err)
		// 这一阶段在无浏览器环境下按 HTTP 结果判断，正文由脚本渲染时必然为空。
		if kind == "rule_error" && browserStages[out.Stage] {
			out.Status = "needs_browser"
			out.Error = strPtr("该阶段需要内置浏览器渲染，本次检测未启用浏览器")
			break
		}
		out.Status = kind
		out.Error = strPtr(checkErrorText(err))
		lastSegment := ""
		if text := out.Error; text != nil {
			if index := strings.LastIndex(*text, "："); index >= 0 {
				lastSegment = strings.TrimSpace((*text)[index+len("："):])
			} else {
				lastSegment = strings.TrimSpace(*text)
			}
		}
		if kind == "rule_error" && !retryableChainErrors[lastSegment] {
			break
		}
	}
	return out
}

func (s *SourceCheckService) chainOneBook(ctx context.Context, source *models.BookSource,
	engineInst CheckEngine, book *models.BookItem, stageTimeout time.Duration,
	out *chainOutcome) error {
	out.Stage = "detail"
	callCtx, cancel := context.WithTimeout(ctx, stageTimeout)
	info, err := engineInst.BookInfo(callCtx, source, book.URL)
	cancel()
	if err != nil {
		return err
	}
	if info != nil && info.Name != "" {
		name := info.Name
		out.SampleBook = &name
	}
	if info != nil && info.TocURL != "" {
		book.TocURL = info.TocURL
	}
	out.Stage = "catalog"
	tocURL := book.TocURL
	if tocURL == "" {
		tocURL = book.URL
	}
	callCtx, cancel = context.WithTimeout(ctx, stageTimeout)
	chapters, err := engineInst.Chapters(callCtx, source, tocURL)
	cancel()
	if err != nil {
		return err
	}
	readable := make([]*models.Chapter, 0, len(chapters))
	for _, item := range chapters {
		if item != nil && item.URL != "" {
			readable = append(readable, item)
		}
	}
	// 目录已经在手上，顺手把正文样本页地址留给规则自愈用。
	urls := make([]string, 0, 3)
	for _, item := range readable {
		if len(urls) >= 3 {
			break
		}
		urls = append(urls, item.URL)
	}
	out.SampleChapterURLs = urls
	count := len(readable)
	out.ChapterCount = &count
	if len(readable) == 0 {
		return errors.New("目录中没有可读取章节")
	}
	chapter := readable[0]
	name := chapter.Title
	out.SampleChapter = &name
	out.Stage = "content"
	callCtx, cancel = context.WithTimeout(ctx, stageTimeout)
	document, err := engineInst.Content(callCtx, source, chapter)
	cancel()
	if err != nil {
		return err
	}
	readableContent := false
	if document != nil {
		readableContent = strings.TrimSpace(document.Content) != "" ||
			(document.SubContent != nil && strings.TrimSpace(*document.SubContent) != "")
	}
	if !readableContent {
		return errors.New("正文解析结果为空")
	}
	out.Status = "ok"
	out.Stage = "complete"
	out.Error = nil
	return nil
}

// browserOnlyStages 阅读链里哪些阶段只有内置浏览器渲染得出（键面判据，同 Python）。
func browserOnlyStages(source *models.BookSource) map[string]bool {
	stages := map[string]bool{}
	if ruleNeedsBrowser(source.RuleToc) {
		stages["catalog"] = true
	}
	if ruleNeedsBrowser(source.RuleContent) {
		stages["content"] = true
	}
	return stages
}

var browserViewRe = regexp.MustCompile(`(?i)"\s*web_?view\s*"\s*:\s*true`)
var browserJSRe = regexp.MustCompile(`(?i)"\s*webJs\s*"\s*:\s*"[^"]`)

func ruleNeedsBrowser(rule interface{}) bool {
	if rule == nil {
		return false
	}
	raw, err := json.Marshal(rule)
	if err != nil {
		return false
	}
	text := strings.ReplaceAll(string(raw), `\"`, `"`)
	return browserViewRe.MatchString(text) || browserJSRe.MatchString(text)
}

func chainStub(reason string) *chainOutcome {
	return &chainOutcome{Status: "skip", Stage: "none",
		SampleChapterURLs: []string{}, Skipped: &reason}
}

func applyChain(result *CheckResult, chain *chainOutcome) {
	result.ReadingStatus = chain.Status
	result.ReadingStage = chain.Stage
	result.ReadingError = chain.Error
	result.ChapterCount = chain.ChapterCount
	result.SampleBook = chain.SampleBook
	result.SampleChapter = chain.SampleChapter
	result.SampleChapterURLs = chain.SampleChapterURLs
	result.ReadingTried = chain.Tried
	result.ReadingCheckSkipped = chain.Skipped
}

// ---------------------------------------------------------------------------
// 发现分类解析（静态 + @js:）
// ---------------------------------------------------------------------------

func (s *SourceCheckService) resolveExploreCategories(source *models.BookSource) ([]exploreCategory, error) {
	raw := strings.TrimSpace(strValue(source.ExploreURL))
	if raw == "" {
		return nil, nil
	}
	if !looksLikeJS(raw) {
		return parseExploreCategories(raw), nil
	}
	if s.hub == nil || s.hub.JSRuntime() == nil {
		return nil, errors.New("分类配置需要 JavaScript 引擎")
	}
	sourceMap := map[string]interface{}{}
	if payload, err := json.Marshal(source); err == nil {
		_ = json.Unmarshal(payload, &sourceMap)
	}
	script := strings.TrimSpace(raw)
	if lower := strings.ToLower(script); strings.HasPrefix(lower, "@js:") {
		script = script[len("@js:"):]
	} else if strings.HasPrefix(lower, "@webjs:") {
		script = script[len("@webjs:"):]
	}
	value, err := s.hub.JSRuntime().Execute(script, &jsruntime.Context{
		BaseURL: source.BookSourceURL, Source: sourceMap,
	})
	if err != nil {
		return nil, err
	}
	return parseExploreCategoriesValue(value), nil
}

func looksLikeJS(raw string) bool {
	lower := strings.ToLower(strings.TrimSpace(raw))
	return strings.HasPrefix(lower, "@js:") || strings.HasPrefix(lower, "@webjs:") ||
		strings.HasPrefix(lower, "<js") || strings.Contains(lower, "java.")
}

func parseExploreCategoriesValue(value interface{}) []exploreCategory {
	switch typed := value.(type) {
	case []interface{}:
		entries := make([]exploreCategory, 0, len(typed))
		for _, item := range typed {
			if entry, ok := item.(map[string]interface{}); ok {
				title := strings.TrimSpace(fmt.Sprintf("%v", entry["title"]))
				entryURL := strings.TrimSpace(fmt.Sprintf("%v", entry["url"]))
				if title != "" && title != "<nil>" && entryURL != "" && entryURL != "<nil>" {
					entries = append(entries, exploreCategory{Title: title, URL: entryURL})
				}
			}
		}
		return entries
	case map[string]interface{}:
		return parseExploreCategoriesValue([]interface{}{typed})
	case string:
		return parseExploreCategories(typed)
	default:
		return nil
	}
}

// parseExploreCategories 静态分类配置：JSON 数组或逐行 `标题::地址`
// （url 为空的条目通常只是视觉分组标题，不是可点击分类）。
func parseExploreCategories(raw string) []exploreCategory {
	trimmed := strings.TrimSpace(raw)
	if strings.HasPrefix(trimmed, "[") {
		var payload []map[string]interface{}
		if err := json.Unmarshal([]byte(trimmed), &payload); err != nil {
			return nil
		}
		entries := make([]exploreCategory, 0, len(payload))
		for _, item := range payload {
			title := strings.TrimSpace(fmt.Sprintf("%v", item["title"]))
			entryURL := strings.TrimSpace(fmt.Sprintf("%v", item["url"]))
			if title != "" && title != "<nil>" && entryURL != "" && entryURL != "<nil>" {
				entries = append(entries, exploreCategory{Title: title, URL: entryURL})
			}
		}
		return entries
	}
	entries := make([]exploreCategory, 0)
	for _, line := range strings.Split(trimmed, "\n") {
		if index := strings.Index(line, "::"); index >= 0 {
			title := strings.TrimSpace(line[:index])
			entryURL := strings.TrimSpace(line[index+2:])
			if title != "" && entryURL != "" {
				entries = append(entries, exploreCategory{Title: title, URL: entryURL})
			}
		}
	}
	return entries
}

var explorePagePairReplaceRe = regexp.MustCompile(`<([^<>]*,[^<>]*)>`)

// resolveExploreURL 分类地址的静态求值（port 自 rule_engine._resolve_explore_url）。
func resolveExploreURL(raw, baseURL string, page int) (string, error) {
	if looksLikeJS(raw) || strings.Contains(raw, "{{cookie.") || strings.Contains(raw, "{{java.") {
		return "", errors.New("分类地址需要 JavaScript、Cookie 或动态签名")
	}
	expanded := expandPageTemplate(raw, page)
	expanded = explorePagePairReplaceRe.ReplaceAllStringFunc(expanded, func(match string) string {
		inner := match[1 : len(match)-1]
		first, later, found := strings.Cut(inner, ",")
		if !found {
			return match
		}
		if page == 1 {
			return first
		}
		return later
	})
	if strings.Contains(expanded, "{{") {
		return "", errors.New("分类地址包含暂不支持的动态模板")
	}
	joined, err := joinBaseURL(baseURL, expanded)
	if err != nil {
		return "", err
	}
	return encodeNonASCIIURL(joined), nil
}

func expandPageTemplate(value string, page int) string {
	value = strings.ReplaceAll(value, "{{page}}", fmt.Sprintf("%d", page))
	value = strings.ReplaceAll(value, "{{page-1}}", fmt.Sprintf("%d", max(page-1, 0)))
	value = strings.ReplaceAll(value, "{{page+1}}", fmt.Sprintf("%d", page+1))
	return value
}

func joinBaseURL(baseURL, reference string) (string, error) {
	base, err := url.Parse(strings.TrimRight(baseURL, "/") + "/")
	if err != nil {
		return "", err
	}
	ref, err := url.Parse(reference)
	if err != nil {
		return "", err
	}
	return base.ResolveReference(ref).String(), nil
}

func encodeNonASCIIURL(raw string) string {
	var builder strings.Builder
	builder.Grow(len(raw))
	for index := 0; index < len(raw); index++ {
		ch := raw[index]
		if ch >= 0x80 {
			fmt.Fprintf(&builder, "%%%02X", ch)
		} else {
			builder.WriteByte(ch)
		}
	}
	return builder.String()
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

func (s *SourceCheckService) collectSources(groups []string) []models.BookSource {
	sources := []models.BookSource{}
	if s.sourceSvc != nil {
		sources = s.sourceSvc.List()
	}
	if len(groups) == 0 {
		return sources
	}
	wanted := make(map[string]bool, len(groups))
	for _, group := range groups {
		wanted[group] = true
	}
	filtered := make([]models.BookSource, 0, len(sources))
	for _, source := range sources {
		if wanted[strValue(source.BookSourceGroup)] {
			filtered = append(filtered, source)
		}
	}
	return filtered
}

func (s *SourceCheckService) applySettings() {
	if s.settings == nil {
		return
	}
	if seconds := s.settings.GetInt("check.timeout", 0); seconds > 0 {
		s.timeout = time.Duration(seconds) * time.Second
	}
	if value := s.settings.GetInt("check.concurrency", 0); value > 0 {
		s.concurrency = value
	}
	s.autoDisable = s.settings.GetString("check.auto_disable") == "true"
}

func (s *SourceCheckService) emptyTiering() map[string]interface{} {
	return map[string]interface{}{
		"plan_total": 0, "shallow": 0, "deep": 0,
		"slice_limit": DeepPerRoundMin, "carried": 0, "deferred": 0,
		"queued": 0, "by_reason": map[string]interface{}{},
	}
}

func (s *SourceCheckService) isRunning() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.run != nil && s.run.running
}

func (s *SourceCheckService) beginRun(total int, depth string, cancel context.CancelFunc) *checkRun {
	run := &checkRun{total: total, running: true, depth: depth,
		results: []map[string]interface{}{}}
	done := make(chan struct{})
	s.mu.Lock()
	s.run = run
	s.runDone = done
	s.cancelRun = cancel
	s.mu.Unlock()
	return run
}

func (s *SourceCheckService) endRun(run *checkRun) {
	s.mu.Lock()
	run.running = false
	done := s.runDone
	s.runDone = nil
	s.cancelRun = nil
	s.mu.Unlock()
	if done != nil {
		close(done)
	}
}

func (s *SourceCheckService) markCancelled(run *checkRun) {
	s.mu.Lock()
	run.cancelled = true
	s.mu.Unlock()
}

func (s *SourceCheckService) awaitRunFinished() {
	s.mu.Lock()
	done := s.runDone
	s.mu.Unlock()
	if done != nil {
		<-done
	}
}

func awaitBlockedProbe(ch <-chan probeOutcome, budget time.Duration) (probeOutcome, bool) {
	timer := time.NewTimer(budget)
	defer timer.Stop()
	select {
	case outcome := <-ch:
		return outcome, true
	case <-timer.C:
		return probeOutcome{}, false
	}
}

func awaitProbe(ch <-chan probeOutcome, budget time.Duration) probeOutcome {
	timer := time.NewTimer(budget)
	defer timer.Stop()
	select {
	case outcome := <-ch:
		return outcome
	case <-timer.C:
		text := fmt.Sprintf("站点首页 %.0f 秒内没有响应", budget.Seconds())
		return probeOutcome{Status: "unknown", Error: &text}
	}
}

func remainingFromDeadline(deadline time.Time, cap time.Duration) time.Duration {
	remaining := time.Until(deadline)
	value := cap
	if remaining < value {
		value = remaining
	}
	if value < time.Second {
		value = time.Second
	}
	return value
}

func checkSourceFromJSON(payload map[string]interface{}) (*models.BookSource, error) {
	if payload == nil {
		return nil, errors.New("check_source 需要 dict 或 BookSource，收到空")
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	source := &models.BookSource{}
	if err := json.Unmarshal(raw, source); err != nil {
		return nil, err
	}
	return source, nil
}

func checkErrorText(err error) string {
	if err == nil {
		return ""
	}
	text := strings.TrimSpace(err.Error())
	if text == "" {
		text = "error"
	}
	runes := []rune(text)
	if len(runes) > checkErrorTextLimit {
		text = string(runes[:checkErrorTextLimit])
	}
	return text
}

// checkErrorKind 错误分类（port 自 check_service._error_kind；Go 错误类型面
// 与 Python 异常类不完全对应，按消息+errors.Is 归口，只求判据落点一致）。
func checkErrorKind(err error) string {
	if err == nil {
		return "rule_error"
	}
	message := strings.ToLower(checkErrorText(err))
	if errors.Is(err, context.DeadlineExceeded) ||
		strings.Contains(message, "timeout") || strings.Contains(message, "超时") {
		return "timeout"
	}
	if strings.Contains(message, "unsupported") || strings.Contains(message, "接口未模拟") ||
		strings.Contains(message, "接口未实现") || strings.Contains(message, "需要 javascript") ||
		strings.Contains(message, "缺少搜索规则") || strings.Contains(message, "缺少发现规则") ||
		strings.Contains(message, "缺少目录规则") || strings.Contains(message, "缺少正文规则") ||
		strings.Contains(message, "未配置搜索") || strings.Contains(message, "未配置发现") ||
		strings.Contains(message, "暂不支持的动态模板") {
		return "unsupported"
	}
	if strings.Contains(message, "jsruntime") || strings.Contains(message, "js 执行") ||
		strings.Contains(message, "javascript 执行") {
		return "js_error"
	}
	if strings.Contains(message, "429") || strings.Contains(message, "rate limit") {
		return "rate_limited"
	}
	var netErr net.Error
	var urlErr *url.Error
	if errors.As(err, &netErr) || errors.As(err, &urlErr) ||
		errors.Is(err, context.Canceled) ||
		strings.Contains(message, "connection refused") ||
		strings.Contains(message, "no such host") ||
		strings.Contains(message, "connection reset") {
		return "network_error"
	}
	return "rule_error"
}

func strPtr(value string) *string { return &value }

func strValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func intValue(value *int) interface{} {
	if value == nil {
		return nil
	}
	return *value
}

func floatValue(value *float64) interface{} {
	if value == nil {
		return nil
	}
	return *value
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}
