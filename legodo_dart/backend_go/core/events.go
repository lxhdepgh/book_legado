package core

import "time"

// ============================================================
// All AppEvent subtypes aligned with Python core/events.py.
//
// 两条不可改的约定（都对齐 Python 侧 wire 契约，app/services/gateway/service.py
// 的 `_make_forwarder`：name = cls.__name__，payload = 字段字典）：
//
//  1. EventType() 返回的字符串就是 Dart 的 `EngineEventNames` 常量
//     （`SearchFinishedEvent` 这一族**类名**，不是蛇形）。event_names.dart
//     注释点名了后果：名字打错不是红屏，是那一屏永远不出结果。
//  2. 每个字段的 json tag 必须与 Python dataclass 的字段名逐字一致
//     （Dart 屏按 snake_case 键取值）。
// ============================================================

// ServiceStateChangedEvent - published on every service state transition.
type ServiceStateChangedEvent struct {
	ServiceID string  `json:"service_id"`
	State     string  `json:"state"`
	Error     string  `json:"error,omitempty"`
	Timestamp float64 `json:"timestamp"`
}

func (e *ServiceStateChangedEvent) EventType() string { return "ServiceStateChangedEvent" }

// SettingsChangedEvent - published when a setting key/value changes.
type SettingsChangedEvent struct {
	Key       string      `json:"key"`
	Value     interface{} `json:"value,omitempty"`
	OldValue  interface{} `json:"old_value,omitempty"`
	Timestamp float64     `json:"timestamp"`
}

func (e *SettingsChangedEvent) EventType() string { return "SettingsChangedEvent" }

// ProgressSavedEvent - reader progress saved.
type ProgressSavedEvent struct {
	BookURL      string  `json:"book_url"`
	ChapterIndex int     `json:"chapter_index"`
	Position     float64 `json:"position,omitempty"`
	Timestamp    float64 `json:"timestamp"`
}

func (e *ProgressSavedEvent) EventType() string { return "ProgressSavedEvent" }

// BookmarkChangedEvent - bookmark added/removed ("add" / "remove").
type BookmarkChangedEvent struct {
	BookURL   string  `json:"book_url"`
	Op        string  `json:"op"`
	Timestamp float64 `json:"timestamp"`
}

func (e *BookmarkChangedEvent) EventType() string { return "BookmarkChangedEvent" }

// DownloadProgressEvent - download task progress; progress is 0-1.
type DownloadProgressEvent struct {
	TaskID    string  `json:"task_id"`
	Kind      string  `json:"kind"`
	Progress  float64 `json:"progress"`
	Speed     float64 `json:"speed"`
	Timestamp float64 `json:"timestamp"`
}

func (e *DownloadProgressEvent) EventType() string { return "DownloadProgressEvent" }

// SearchFinishedEvent - search across sources; items is a list of book row
// snapshots (same shape as the searchInScope reply). finished=false is a
// per-source increment, finished=true closes the round.
type SearchFinishedEvent struct {
	SearchID  string      `json:"search_id"`
	Items     interface{} `json:"items"`
	Timestamp float64     `json:"timestamp"`
	Finished  bool        `json:"finished"`
}

func (e *SearchFinishedEvent) EventType() string { return "SearchFinishedEvent" }

// ReadingSessionChangedEvent - active reading session changed.
type ReadingSessionChangedEvent struct {
	BookURL      string  `json:"book_url"`
	ChapterIndex int     `json:"chapter_index"`
	Timestamp    float64 `json:"timestamp"`
}

func (e *ReadingSessionChangedEvent) EventType() string { return "ReadingSessionChangedEvent" }

// SourceSwitchProgressEvent - switch source sweep per-source progress.
// failures and starved stay separate numbers: starved is "this machine is
// busy", not the source's fault.
type SourceSwitchProgressEvent struct {
	SwitchID  string  `json:"switch_id"`
	Done      int     `json:"done"`
	Total     int     `json:"total"`
	Failures  int     `json:"failures"`
	Starved   int     `json:"starved"`
	Timestamp float64 `json:"timestamp"`
}

func (e *SourceSwitchProgressEvent) EventType() string { return "SourceSwitchProgressEvent" }

// SourceSwitchFinishedEvent - one switch sweep closed (done / cancelled /
// errored: all three endings send this frame). candidates is the ranked list
// of {source_url, source_name, book} rows.
type SourceSwitchFinishedEvent struct {
	SwitchID   string      `json:"switch_id"`
	Candidates interface{} `json:"candidates"`
	Cancelled  bool        `json:"cancelled"`
	Error      string      `json:"error"`
	Timestamp  float64     `json:"timestamp"`
}

func (e *SourceSwitchFinishedEvent) EventType() string { return "SourceSwitchFinishedEvent" }

// SourceChangedEvent - a book source was modified; source_id is the source
// URL (empty string = whole-library change, receivers refresh everything).
type SourceChangedEvent struct {
	ChangeType string  `json:"change_type"`
	SourceID   string  `json:"source_id"`
	Timestamp  float64 `json:"timestamp"`
}

func (e *SourceChangedEvent) EventType() string { return "SourceChangedEvent" }

// CheckFinishedEvent - one source finished checking. done/total are the
// round's planned scale (the progress denominator).
type CheckFinishedEvent struct {
	SourceID   string  `json:"source_id"`
	StateClass string  `json:"state_class"`
	Summary    string  `json:"summary"`
	Done       int     `json:"done"`
	Total      int     `json:"total"`
	Timestamp  float64 `json:"timestamp"`
}

func (e *CheckFinishedEvent) EventType() string { return "CheckFinishedEvent" }

// RepairReportReadyEvent - rule repair report generated.
type RepairReportReadyEvent struct {
	ReportID  string  `json:"report_id"`
	SourceID  string  `json:"source_id"`
	Timestamp float64 `json:"timestamp"`
}

func (e *RepairReportReadyEvent) EventType() string { return "RepairReportReadyEvent" }

// RepairProgressEvent - rule repair stage progress; stage is a human string.
type RepairProgressEvent struct {
	SourceID  string  `json:"source_id"`
	Stage     string  `json:"stage"`
	Timestamp float64 `json:"timestamp"`
}

func (e *RepairProgressEvent) EventType() string { return "RepairProgressEvent" }

// ImportFinishedEvent - bulk import completed.
type ImportFinishedEvent struct {
	Kind      string   `json:"kind"`
	Count     int      `json:"count"`
	Errors    []string `json:"errors"`
	Timestamp float64  `json:"timestamp"`
}

func (e *ImportFinishedEvent) EventType() string { return "ImportFinishedEvent" }

// ImportProgressEvent - bulk import progress; total=0 means unknown size.
type ImportProgressEvent struct {
	Kind      string  `json:"kind"`
	Phase     string  `json:"phase"`
	Done      int     `json:"done"`
	Total     int     `json:"total"`
	Timestamp float64 `json:"timestamp"`
}

func (e *ImportProgressEvent) EventType() string { return "ImportProgressEvent" }

// ReadAloudStateEvent - TTS read-aloud state change.
type ReadAloudStateEvent struct {
	State     string  `json:"state"` // "idle", "speaking", "paused", "error"
	BookURL   string  `json:"book_url"`
	Chapter   int     `json:"chapter"`
	Position  float64 `json:"position"`
	Timestamp float64 `json:"timestamp"`
}

func (e *ReadAloudStateEvent) EventType() string { return "ReadAloudStateEvent" }

// MediaPlayStateEvent - video playback state change.
type MediaPlayStateEvent struct {
	State     string  `json:"state"` // "idle", "playing", "paused", "ended", "error"
	BookURL   string  `json:"book_url"`
	Chapter   int     `json:"chapter"`
	Timestamp float64 `json:"timestamp"`
}

func (e *MediaPlayStateEvent) EventType() string { return "MediaPlayStateEvent" }

// RssRefreshedEvent - one RSS source finished refreshing; new_count is the
// number of newly added articles (deduplicated by (source_id, url)).
type RssRefreshedEvent struct {
	SourceID  string  `json:"source_id"`
	NewCount  int     `json:"new_count"`
	Timestamp float64 `json:"timestamp"`
}

func (e *RssRefreshedEvent) EventType() string { return "RssRefreshedEvent" }

// RssRefreshProgressEvent - RSS batch refresh progress; refresh_id is
// client-generated and echoed back on every frame.
type RssRefreshProgressEvent struct {
	Done      int     `json:"done"`
	Total     int     `json:"total"`
	Current   string  `json:"current"`
	Failures  int     `json:"failures"`
	RefreshID int64   `json:"refresh_id"`
	Timestamp float64 `json:"timestamp"`
}

func (e *RssRefreshProgressEvent) EventType() string { return "RssRefreshProgressEvent" }

// ReplaceRulesChangedEvent - global replace rules modified; empty ids means
// a whole-library change (receivers re-wash everything).
type ReplaceRulesChangedEvent struct {
	IDs       []int   `json:"ids"`
	Timestamp float64 `json:"timestamp"`
}

func (e *ReplaceRulesChangedEvent) EventType() string { return "ReplaceRulesChangedEvent" }

// BackupFinishedEvent - WebDAV backup/restore completed (sent either way).
type BackupFinishedEvent struct {
	Kind      string  `json:"kind"` // "backup" / "restore"
	OK        bool    `json:"ok"`
	Detail    string  `json:"detail"`
	Timestamp float64 `json:"timestamp"`
}

func (e *BackupFinishedEvent) EventType() string { return "BackupFinishedEvent" }

// BackupProgressEvent - WebDAV backup/restore stage progress.
type BackupProgressEvent struct {
	Kind      string  `json:"kind"`
	Stage     string  `json:"stage"`
	Done      int     `json:"done"`
	Total     int     `json:"total"`
	Timestamp float64 `json:"timestamp"`
}

func (e *BackupProgressEvent) EventType() string { return "BackupProgressEvent" }

// ExportProgressEvent - EPUB/TXT export progress (done/total = chapters).
type ExportProgressEvent struct {
	Kind      string  `json:"kind"` // "epub" / "txt"
	Stage     string  `json:"stage"`
	Done      int     `json:"done"`
	Total     int     `json:"total"`
	Timestamp float64 `json:"timestamp"`
}

func (e *ExportProgressEvent) EventType() string { return "ExportProgressEvent" }

// UpdateAvailableEvent - a newer version was found (no auto-install).
type UpdateAvailableEvent struct {
	Version   string  `json:"version"`
	Notes     string  `json:"notes"`
	URL       string  `json:"url"`
	Timestamp float64 `json:"timestamp"`
}

func (e *UpdateAvailableEvent) EventType() string { return "UpdateAvailableEvent" }

// --- Helpers to make event construction convenient ---

// NowSeconds matches Python's time.time() float seconds used by all events.
func NowSeconds() float64 { return float64(time.Now().UnixMilli()) / 1000.0 }

func NewServiceStateChanged(serviceID string, state string, err error) *ServiceStateChangedEvent {
	e := &ServiceStateChangedEvent{
		ServiceID: serviceID,
		State:     state,
		Timestamp: NowSeconds(),
	}
	if err != nil {
		e.Error = err.Error()
	}
	return e
}
