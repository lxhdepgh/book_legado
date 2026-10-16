package core

import "sync"

// RingEntry is one recorded event as it crosses the FFI boundary:
// {"seq": 42, "name": "SearchFinishedEvent", "payload": {...}}.
type RingEntry struct {
	Seq     int64       `json:"seq"`
	Name    string      `json:"name"`
	Payload interface{} `json:"payload"`
}

// EventRing keeps the most recent events for Dart-side polling (engine_poll).
//
// FFI 没有推送出口，Dart 每 250ms 以游标 `since` 来问一次。环形缓冲的两条语义：
//   - seq 单调递增，Dart 用 last_seq 推进游标；
//   - 客户端游标老过环里最老一条时带 lost=true 返回最早的部分——宁可丢旧事件
//     也不能断流（断流在那 7 条屏上表现为"结果永远不来"）。
type EventRing struct {
	mu       sync.Mutex
	entries  []RingEntry
	nextSeq  int64
	capacity int
}

// NewEventRing creates a ring holding up to capacity recent events.
func NewEventRing(capacity int) *EventRing {
	if capacity <= 0 {
		capacity = 4096
	}
	return &EventRing{
		entries:  make([]RingEntry, 0, capacity),
		capacity: capacity,
	}
}

// Record appends an event. Called synchronously inside EventBus.Publish so
// that seq order equals publish order (handlers still dispatch concurrently).
func (r *EventRing) Record(event AppEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.nextSeq++
	r.entries = append(r.entries, RingEntry{
		Seq:     r.nextSeq,
		Name:    event.EventType(),
		Payload: event,
	})
	if len(r.entries) > r.capacity {
		r.entries = r.entries[len(r.entries)-r.capacity:]
	}
}

// Since returns every entry with Seq > since, the current last seq, and
// whether the requested cursor has fallen out of the ring.
func (r *EventRing) Since(since int64) (entries []RingEntry, lastSeq int64, lost bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	lastSeq = r.nextSeq
	if len(r.entries) == 0 {
		return []RingEntry{}, lastSeq, false
	}
	oldest := r.entries[0].Seq
	if since < oldest-1 {
		lost = true
		since = oldest - 1
	}
	entries = make([]RingEntry, 0, len(r.entries))
	for _, e := range r.entries {
		if e.Seq > since {
			entries = append(entries, e)
		}
	}
	return entries, lastSeq, lost
}
