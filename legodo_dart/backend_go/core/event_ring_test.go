package core

import (
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestEventRingBasicCursor(t *testing.T) {
	ring := NewEventRing(8)
	ring.Record(&SearchFinishedEvent{SearchID: "1"})
	ring.Record(&SearchFinishedEvent{SearchID: "2"})
	ring.Record(&SourceChangedEvent{ChangeType: "save", SourceID: "u"})

	entries, last, lost := ring.Since(0)
	if lost || last != 3 || len(entries) != 3 {
		t.Fatalf("Since(0) = %d entries last=%d lost=%v, want 3 entries last=3 lost=false",
			len(entries), last, lost)
	}
	for i, e := range entries {
		if e.Seq != int64(i+1) {
			t.Fatalf("entry %d has seq %d, want %d (seq must be 1-based monotonic)", i, e.Seq, i+1)
		}
	}

	entries, last, lost = ring.Since(2)
	if lost || last != 3 || len(entries) != 1 || entries[0].Seq != 3 {
		t.Fatalf("Since(2) = %d entries last=%d lost=%v, want 1 entry seq=3", len(entries), last, lost)
	}

	entries, _, _ = ring.Since(3)
	if len(entries) != 0 {
		t.Fatalf("Since(3) should be empty, got %d entries", len(entries))
	}
}

func TestEventRingOverflowMarksLost(t *testing.T) {
	ring := NewEventRing(2)
	for i := 0; i < 4; i++ {
		ring.Record(&SearchFinishedEvent{SearchID: "s"})
	}
	entries, last, lost := ring.Since(0)
	if !lost {
		t.Fatalf("cursor 0 fell out of a capacity-2 ring, want lost=true")
	}
	if last != 4 || len(entries) != 2 || entries[0].Seq != 3 || entries[1].Seq != 4 {
		t.Fatalf("want entries seq 3,4 last=4, got %d entries last=%d", len(entries), last)
	}

	// 游标刚好在"最老的前一格"时不算丢：环里从 oldest 起都拿得到。
	entries, _, lost = ring.Since(2)
	if lost || len(entries) != 2 {
		t.Fatalf("Since(2) on [3,4] should be lossless, got %d entries lost=%v", len(entries), lost)
	}
}

func TestEventRingWireContract(t *testing.T) {
	// 事件名必须是 Python 契约里的**类名**，payload 键必须是 snake_case——
	// Dart 的 EngineEventNames 与各屏 fromJson 都钉在这两条上。
	ring := NewEventRing(8)
	ring.Record(&SearchFinishedEvent{SearchID: "77", Items: []interface{}{}, Finished: true})
	entries, _, _ := ring.Since(0)
	if len(entries) != 1 {
		t.Fatalf("want 1 entry, got %d", len(entries))
	}
	if entries[0].Name != "SearchFinishedEvent" {
		t.Fatalf("wire name = %q, want the PascalCase class name", entries[0].Name)
	}
	raw, err := json.Marshal(entries[0])
	if err != nil {
		t.Fatalf("marshal entry: %v", err)
	}
	var decoded map[string]interface{}
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal entry: %v", err)
	}
	payload, _ := decoded["payload"].(map[string]interface{})
	for _, key := range []string{"search_id", "items", "finished"} {
		if _, ok := payload[key]; !ok {
			t.Fatalf("payload missing snake_case key %q (keys: %v)", key, keysOf(payload))
		}
	}
	if !strings.Contains(string(raw), `"name":"SearchFinishedEvent"`) {
		t.Fatalf("entry JSON shape drifted: %s", raw)
	}
}

func keysOf(m map[string]interface{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func TestEventNamesAreContractClassNames(t *testing.T) {
	// 24 条事件名逐一钉死：event_names.dart 那 7 条是**子集**，多一格少一格都在这红。
	want := map[AppEvent]string{
		&ServiceStateChangedEvent{}:   "ServiceStateChangedEvent",
		&SettingsChangedEvent{}:       "SettingsChangedEvent",
		&ProgressSavedEvent{}:         "ProgressSavedEvent",
		&BookmarkChangedEvent{}:       "BookmarkChangedEvent",
		&DownloadProgressEvent{}:      "DownloadProgressEvent",
		&SearchFinishedEvent{}:        "SearchFinishedEvent",
		&ReadingSessionChangedEvent{}: "ReadingSessionChangedEvent",
		&SourceSwitchProgressEvent{}:  "SourceSwitchProgressEvent",
		&SourceSwitchFinishedEvent{}:  "SourceSwitchFinishedEvent",
		&SourceChangedEvent{}:         "SourceChangedEvent",
		&CheckFinishedEvent{}:         "CheckFinishedEvent",
		&RepairReportReadyEvent{}:     "RepairReportReadyEvent",
		&RepairProgressEvent{}:        "RepairProgressEvent",
		&ImportFinishedEvent{}:        "ImportFinishedEvent",
		&ImportProgressEvent{}:        "ImportProgressEvent",
		&ReadAloudStateEvent{}:        "ReadAloudStateEvent",
		&MediaPlayStateEvent{}:        "MediaPlayStateEvent",
		&RssRefreshedEvent{}:          "RssRefreshedEvent",
		&RssRefreshProgressEvent{}:    "RssRefreshProgressEvent",
		&ReplaceRulesChangedEvent{}:   "ReplaceRulesChangedEvent",
		&BackupFinishedEvent{}:        "BackupFinishedEvent",
		&BackupProgressEvent{}:        "BackupProgressEvent",
		&ExportProgressEvent{}:        "ExportProgressEvent",
		&UpdateAvailableEvent{}:       "UpdateAvailableEvent",
	}
	for event, name := range want {
		if got := event.EventType(); got != name {
			t.Errorf("%T.EventType() = %q, want %q", event, got, name)
		}
	}
	if len(want) != 24 {
		t.Fatalf("contract table drifted: %d entries, Python has 24 event classes", len(want))
	}
}

func TestSubscribeAllRunsSynchronouslyInPublishOrder(t *testing.T) {
	bus := NewEventBus()
	var mu sync.Mutex
	var seen []string
	bus.SubscribeAll(func(event AppEvent) {
		mu.Lock()
		seen = append(seen, event.EventType())
		mu.Unlock()
	})

	bus.Publish(&SearchFinishedEvent{SearchID: "a"})
	bus.Publish(&SourceChangedEvent{ChangeType: "save"})
	bus.Publish(&CheckFinishedEvent{SourceID: "x"})

	// Publish 返回时 catch-all 必已跑完（ring 依赖这一条），不需要等待。
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 3 || seen[0] != "SearchFinishedEvent" ||
		seen[1] != "SourceChangedEvent" || seen[2] != "CheckFinishedEvent" {
		t.Fatalf("catch-all order = %v, want publish order", seen)
	}
}

func TestTypedHandlersStillDispatch(t *testing.T) {
	bus := NewEventBus()
	got := make(chan string, 1)
	bus.Subscribe("SearchFinishedEvent", func(event AppEvent) {
		if e, ok := event.(*SearchFinishedEvent); ok {
			got <- e.SearchID
		}
	})
	bus.Publish(&SearchFinishedEvent{SearchID: "42"})
	select {
	case id := <-got:
		if id != "42" {
			t.Fatalf("typed handler got %q, want 42", id)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("typed handler not dispatched")
	}
}
