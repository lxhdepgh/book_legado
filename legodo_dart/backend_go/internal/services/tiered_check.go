package services

import (
	"math"
	"net"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/publicsuffix"

	"github.com/legodo-booksource/legodo_go/internal/models"
)

// 自动分级检测的进深检判据 + 待深检队列（port 自 app/services/sources/tiered_check.py）。
//
// 一轮批量检测跑浅遍扫全书（checkDepthList），补验正文的完整档（checkDepthFull）
// 只吃这一轮开跑前就欠着的那一片（PerRoundDeepLimit），浅遍当场排进队列的源留给
// 后续轮次——一条源一轮只被派发一次。判据细节与实测结论见 Python 原文。
//
// 语义适配说明：Python 用 OkHttp 的公共后缀表（resources/publicsuffixes.gz），这里
// 用 golang.org/x/net/publicsuffix（同一份 PSL）。两边都是"按可注册域名比同站"，
// 表版本差异不影响判据形状。

const (
	// ReasonCrossHost 判据①：搜索主机与主站主机不是同一家站（按可注册域名比）。
	ReasonCrossHost = "cross_host"
	// ReasonUndecidableHost 判据②：搜索地址或主站地址静态判不出主机。
	ReasonUndecidableHost = "undecidable_host"
	// ReasonListUsable 判据③a：浅遍证明列表链路可用，正文段还没验过。
	ReasonListUsable = "list_usable"
	// ReasonUnknownShallow 判据③b：浅遍自己没结论（网络/超时/规则错）。
	ReasonUnknownShallow = "unknown_shallow"

	// DeepMaxAttempts 一条源最多被深遍重试几次（只对 unknown 类生效）。
	DeepMaxAttempts = 3
	// DeepEntryTTLSeconds 队列条目存活时间（秒）。
	DeepEntryTTLSeconds = 7 * 24 * 3600.0
	// DeepRetryCooldownSeconds 试满配额之后隔多久重新给一次机会（秒）。
	DeepRetryCooldownSeconds = 24 * 3600.0

	// DeepPerRoundRatio 一轮最多吃掉的补验条数比例。
	DeepPerRoundRatio = 0.08
	// DeepPerRoundMin 比例下限（小库一轮就能挪完）。
	DeepPerRoundMin = 12

	// ShallowDepthMark 只有 reading_check_skipped 等于这个标记的结果才是浅遍跑出来的。
	ShallowDepthMark = "list"
)

// DeepReasons 所有会让一条源进深检队列的原因（界面上按这个顺序归组）。
var DeepReasons = []string{ReasonListUsable, ReasonUnknownShallow,
	ReasonCrossHost, ReasonUndecidableHost}

var deepReasonSet = map[string]bool{
	ReasonListUsable: true, ReasonUnknownShallow: true,
	ReasonCrossHost: true, ReasonUndecidableHost: true,
}

// ShallowUsableStates 浅遍结论里"列表链路已经证明可用"的那几个九态状态。
var ShallowUsableStates = map[string]bool{
	"search_only": true, "explore_only": true, "list_usable": true,
}

// ShallowUnknownStates 浅遍结论里"没结论"的那一个。
var ShallowUnknownStates = map[string]bool{"unknown": true}

// PerRoundDeepLimit 这一轮最多补验多少条（按本轮计划源数取）。
func PerRoundDeepLimit(planTotal int) int {
	plan := planTotal
	if plan < 0 {
		plan = 0
	}
	return max(DeepPerRoundMin, int(math.Round(float64(plan)*DeepPerRoundRatio)))
}

func firstToken(raw string) string {
	fields := strings.Fields(strings.TrimSpace(raw))
	if len(fields) == 0 {
		return ""
	}
	return fields[0]
}

func normalizeHost(host string) string {
	return strings.ToLower(strings.Trim(strings.TrimSpace(host), "."))
}

// homeHost 取 bookSourceUrl 的主机名；不是 http(s) 或抠不出主机时回空串。
func homeHost(source *models.BookSource) string {
	token := firstToken(source.BookSourceURL)
	if !strings.HasPrefix(strings.ToLower(token), "http://") &&
		!strings.HasPrefix(strings.ToLower(token), "https://") {
		return ""
	}
	parsed, err := url.Parse(token)
	if err != nil {
		return ""
	}
	return normalizeHost(parsed.Host)
}

// execMarkers 出现在"主机那一段"里就说明这段是模板/JS 算出来的。
const execMarkers = "{}()=$<>;,\"`"

// hostLike 这一段文本像不像一台主机：已知顶级域结尾或 IP 字面量
// （不按点数判——Legado 的相对搜索地址大量长这样：search.php?key={{key}}）。
func hostLike(head string) bool {
	if head == "" || strings.HasPrefix(head, ".") {
		return false
	}
	hostPart := head
	if idx := strings.Index(head, ":"); idx >= 0 {
		hostPart = head[:idx]
	}
	if net.ParseIP(hostPart) != nil {
		return true
	}
	labels := strings.Split(head, ".")
	if len(labels) < 2 {
		return false
	}
	last := strings.ToLower(labels[len(labels)-1])
	ps, icann := publicsuffix.PublicSuffix(last)
	return icann && ps == last
}

// searchHost 取 searchUrl 的形状与主机：返回 (kind, host)。
//
// kind 取四个值：empty / dynamic / same_origin / host（同 Python search_host）。
func searchHost(source *models.BookSource) (string, string) {
	token := ""
	if source.SearchURL != nil {
		token = firstToken(*source.SearchURL)
	}
	if token == "" {
		return "empty", ""
	}
	if strings.HasPrefix(token, "@") {
		return "dynamic", ""
	}
	lowered := strings.ToLower(token)
	if strings.HasPrefix(lowered, "http://") || strings.HasPrefix(lowered, "https://") {
		parsed, err := url.Parse(token)
		if err != nil {
			return "host", ""
		}
		return "host", normalizeHost(parsed.Host)
	}
	if strings.HasPrefix(token, "//") {
		parsed, err := url.Parse("http:" + token)
		if err != nil {
			return "host", ""
		}
		return "host", normalizeHost(parsed.Host)
	}
	if strings.HasPrefix(token, "/") || strings.HasPrefix(token, "?") {
		return "same_origin", ""
	}
	// 没写 scheme：剥掉 Legado 的选项段，再看主机那一段是模板/像主机/相对路径。
	head := token
	if idx := strings.Index(head, "/"); idx >= 0 {
		head = head[:idx]
	}
	if idx := strings.Index(head, "?"); idx >= 0 {
		head = head[:idx]
	}
	if idx := strings.Index(head, ","); idx >= 0 {
		head = head[:idx]
	}
	if strings.ContainsAny(head, execMarkers) {
		return "dynamic", ""
	}
	if hostLike(head) {
		return "host", normalizeHost(head)
	}
	return "same_origin", ""
}

// sameSite 两台主机是不是同一家站（可注册域名相同）。算不出时按"不同家"处理。
func sameSite(hostA, hostB string) bool {
	if hostA == "" || hostB == "" {
		return false
	}
	if hostA == hostB {
		return true
	}
	a, errA := publicsuffix.EffectiveTLDPlusOne(hostA)
	b, errB := publicsuffix.EffectiveTLDPlusOne(hostB)
	if errA != nil || errB != nil {
		return false
	}
	return a != "" && a == b
}

// StaticDeepReason 判据①②：不看检测结果，只看这条源的地址形状。返回原因或空串。
func StaticDeepReason(source *models.BookSource) string {
	home := homeHost(source)
	kind, search := searchHost(source)
	if home == "" || kind == "dynamic" || (kind == "host" && search == "") {
		return ReasonUndecidableHost
	}
	if kind == "empty" || kind == "same_origin" {
		return ""
	}
	if sameSite(home, search) {
		return ""
	}
	return ReasonCrossHost
}

func positiveCounter(value interface{}) bool {
	switch typed := value.(type) {
	case int:
		return typed > 0
	case int64:
		return typed > 0
	case float64:
		return typed > 0
	default:
		return false
	}
}

// HasProseCandidates 浅遍有没有真交出可以拿去试正文的书。
func HasProseCandidates(result *CheckResult) bool {
	if result == nil {
		return false
	}
	if result.BookCount != nil && *result.BookCount > 0 {
		return true
	}
	return result.TagValid > 0
}

// ShallowDeepReason 判据③：浅遍跑完之后，这条结论要不要进深检队列。返回原因或空串。
func ShallowDeepReason(result *CheckResult) string {
	if result == nil || result.ReadingCheckSkipped == nil ||
		*result.ReadingCheckSkipped != ShallowDepthMark {
		return ""
	}
	if ShallowUsableStates[result.CheckState] {
		if HasProseCandidates(result) {
			return ReasonListUsable
		}
		return ""
	}
	if ShallowUnknownStates[result.CheckState] {
		return ReasonUnknownShallow
	}
	return ""
}

type deepEntry struct {
	Reason   string
	Attempts int
	TS       float64
	QueuedAt float64
}

// DeepQueue 待深检队列（服务侧，不落库、不进用户分组）。
type DeepQueue struct {
	mu          sync.Mutex
	entries     map[string]*deepEntry
	maxAttempts int
	ttl         float64
	cooldown    float64
	clock       func() float64
}

// NewDeepQueue 建一条默认队列；clock 为 nil 时用墙钟秒。
func NewDeepQueue() *DeepQueue {
	return &DeepQueue{
		entries:     make(map[string]*deepEntry),
		maxAttempts: DeepMaxAttempts,
		ttl:         DeepEntryTTLSeconds,
		cooldown:    DeepRetryCooldownSeconds,
		clock:       func() float64 { return float64(time.Now().UnixNano()) / 1e9 },
	}
}

// Len 队列总条数（含试满的）。
func (q *DeepQueue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.entries)
}

// Add 排进队列。判据换了就是新信息：重试预算重置、队也重新排；同一个原因反复
// 排进来只刷新"结论还新不新"的时间戳，不许洗白次数、也不许把队推回队尾。
func (q *DeepQueue) Add(rawURL, reason string) {
	if rawURL == "" || !deepReasonSet[reason] {
		return
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	now := q.clock()
	entry, ok := q.entries[rawURL]
	if !ok {
		q.entries[rawURL] = &deepEntry{Reason: reason, Attempts: 0, TS: now, QueuedAt: now}
		return
	}
	entry.TS = now
	if entry.Reason != reason {
		entry.Reason = reason
		entry.Attempts = 0
		entry.QueuedAt = now
	}
}

// Exhausted 这一条是不是已经把重试预算花完了。
func (q *DeepQueue) Exhausted(rawURL string) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	entry, ok := q.entries[rawURL]
	return ok && entry.Attempts >= q.maxAttempts
}

// NoteAttempt 深遍跑过它一回（跑完还不行就计数，满次数歇 cooldown）。
func (q *DeepQueue) NoteAttempt(rawURL string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if entry, ok := q.entries[rawURL]; ok {
		entry.Attempts++
	}
}

// Drop 从队列里摘掉。
func (q *DeepQueue) Drop(rawURL string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	delete(q.entries, rawURL)
}

// ReasonOf 这条源当前排队的原因。
func (q *DeepQueue) ReasonOf(rawURL string) string {
	q.mu.Lock()
	defer q.mu.Unlock()
	if entry, ok := q.entries[rawURL]; ok {
		return entry.Reason
	}
	return ""
}

// Prune 丢掉过期与"歇够 cooldown 的试满条目"，返回被丢掉的 raw_url。
func (q *DeepQueue) Prune() []string {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.pruneLocked()
}

func (q *DeepQueue) pruneLocked() []string {
	now := q.clock()
	gone := make([]string, 0)
	for rawURL, entry := range q.entries {
		age := now - entry.TS
		spent := entry.Attempts >= q.maxAttempts
		if age > q.ttl || (spent && age > q.cooldown) {
			delete(q.entries, rawURL)
			gone = append(gone, rawURL)
		}
	}
	return gone
}

// Pending 当前该排深检的条目：{raw_url: reason}（试满配额的不在这里）。
func (q *DeepQueue) Pending() map[string]string {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.pruneLocked()
	out := make(map[string]string, len(q.entries))
	for rawURL, entry := range q.entries {
		if entry.Attempts < q.maxAttempts {
			out[rawURL] = entry.Reason
		}
	}
	return out
}

// Select 从本轮要检的源里挑出队列中那些（顺序跟源列表，不跟队列）。
//
// 给了 limit 就是"这一轮只吃得起这么多"：挑的是还没被补验过的先、试得少的先、
// 欠得久的先（attempts, queued_at, 列表序）。limit < 0 表示不限额。
func (q *DeepQueue) Select(sources []models.BookSource, limit int) []models.BookSource {
	wanted := q.Pending()
	type picked struct {
		index    int
		attempts int
		queued   float64
		source   models.BookSource
	}
	items := make([]picked, 0)
	for index, source := range sources {
		if _, ok := wanted[source.BookSourceURL]; !ok {
			continue
		}
		q.mu.Lock()
		entry := q.entries[source.BookSourceURL]
		q.mu.Unlock()
		item := picked{index: index, source: source}
		if entry != nil {
			item.attempts = entry.Attempts
			item.queued = entry.QueuedAt
		}
		items = append(items, item)
	}
	if limit >= 0 && limit < len(items) {
		sort.SliceStable(items, func(i, j int) bool {
			if items[i].attempts != items[j].attempts {
				return items[i].attempts < items[j].attempts
			}
			if items[i].queued != items[j].queued {
				return items[i].queued < items[j].queued
			}
			return items[i].index < items[j].index
		})
		items = items[:limit]
	}
	out := make([]models.BookSource, len(items))
	for index, item := range items {
		out[index] = item.source
	}
	return out
}

// Snapshot 给回放快照用的明细（按原因分组稳定排序），含已试满的那些。
func (q *DeepQueue) Snapshot() []map[string]interface{} {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.pruneLocked()
	order := make(map[string]int, len(DeepReasons))
	for index, reason := range DeepReasons {
		order[reason] = index
	}
	rows := make([]map[string]interface{}, 0, len(q.entries))
	for rawURL, entry := range q.entries {
		rows = append(rows, map[string]interface{}{
			"raw_url":   rawURL,
			"reason":    entry.Reason,
			"attempts":  entry.Attempts,
			"exhausted": entry.Attempts >= q.maxAttempts,
		})
	}
	sort.SliceStable(rows, func(i, j int) bool {
		ri, oki := order[rows[i]["reason"].(string)]
		rj, okj := order[rows[j]["reason"].(string)]
		if !oki {
			ri = len(order)
		}
		if !okj {
			rj = len(order)
		}
		if ri != rj {
			return ri < rj
		}
		return rows[i]["raw_url"].(string) < rows[j]["raw_url"].(string)
	})
	return rows
}
