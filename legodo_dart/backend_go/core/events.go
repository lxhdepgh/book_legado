package core

import "time"

// ============================================================
// All 22+ AppEvent subtypes aligned with Python core/events.py
// Frozen (immutable) dataclass equivalents: use struct{} with no pointers.
// ============================================================

// ServiceStateChangedEvent - published on every service state transition.
type ServiceStateChangedEvent struct {
	ServiceID string
	State     string
	Error     string
	Timestamp float64
}

func (e *ServiceStateChangedEvent) EventType() string { return "service_state_changed" }

// SettingsChangedEvent - published when a setting key/value changes.
type SettingsChangedEvent struct {
	Key       string
	Value     interface{}
	OldValue  interface{}
	Timestamp float64
}

func (e *SettingsChangedEvent) EventType() string { return "settings_changed" }

// ProgressSavedEvent - reader progress saved.
type ProgressSavedEvent struct {
	BookURL      string
	ChapterIndex int
	Position     float64 // seconds or pixel offset
}

func (e *ProgressSavedEvent) EventType() string { return "progress_saved" }

// BookmarkChangedEvent - bookmark added/removed/moved.
type BookmarkChangedEvent struct {
	BookURL      string
	Op           string // "add", "remove", "move"
	ChapterIndex int
	Offset       float64
}

func (e *BookmarkChangedEvent) EventType() string { return "bookmark_changed" }

// DownloadProgressEvent - download task progress.
type DownloadProgressEvent struct {
	TaskID    string
	Kind      string // "full_cache", "manga_prefetch", etc.
	Progress  float64
	Speed     string // e.g., "1.2 MB/s"
}

func (e *DownloadProgressEvent) EventType() string { return "download_progress" }

// SearchFinishedEvent - search across sources completed.
type SearchFinishedEvent struct {
	SearchID  string
	Items     int
	Finished  bool
}

func (e *SearchFinishedEvent) EventType() string { return "search_finished" }

// ReadingSessionChangedEvent - active reading session changed.
type ReadingSessionChangedEvent struct {
	BookURL      string
	ChapterIndex int
}

func (e *ReadingSessionChangedEvent) EventType() string { return "reading_session_changed" }

// SourceSwitchProgressEvent - switch source sweep progress.
type SourceSwitchProgressEvent struct {
	SwitchID  string
	Done      int
	Total     int
	Failures  int
	Starved   int
}

func (e *SourceSwitchProgressEvent) EventType() string { return "source_switch_progress" }

// SourceSwitchFinishedEvent - switch source sweep finished.
type SourceSwitchFinishedEvent struct {
	SwitchID  string
	Candidates int
	Cancelled  bool
	Error      string
}

func (e *SourceSwitchFinishedEvent) EventType() string { return "source_switch_finished" }

// SourceChangedEvent - a book source was modified.
type SourceChangedEvent struct {
	ChangeType string // "import", "save", "delete", "enable", "group", etc.
	SourceURL  string
}

func (e *SourceChangedEvent) EventType() string { return "source_changed" }

// CheckFinishedEvent - source check pipeline completed.
type CheckFinishedEvent struct {
	SourceID  string
	StateClass string // "available", "unavailable", etc.
	Done      int
	Total     int
}

func (e *CheckFinishedEvent) EventType() string { return "check_finished" }

// RepairReportReadyEvent - rule repair report generated.
type RepairReportReadyEvent struct {
	ReportID string
	SourceURL string
}

func (e *RepairReportReadyEvent) EventType() string { return "repair_report_ready" }

// RepairProgressEvent - rule repair stage progress.
type RepairProgressEvent struct {
	SourceURL string
	Stage     string
}

func (e *RepairProgressEvent) EventType() string { return "repair_progress" }

// ImportFinishedEvent - bulk import completed.
type ImportFinishedEvent struct {
	Kind    string
	Count   int
	Errors  []string
}

func (e *ImportFinishedEvent) EventType() string { return "import_finished" }

// ImportProgressEvent - bulk import progress.
type ImportProgressEvent struct {
	Kind    string
	Phase   string // "url_fetch", "parsing", "importing", "done"
	Done    int
	Total   int
}

func (e *ImportProgressEvent) EventType() string { return "import_progress" }

// ReadAloudStateEvent - TTS read-aloud state change.
type ReadAloudStateEvent struct {
	State    string // "speaking", "paused", "idle"
	BookURL  string
	Chapter  string
	Position float64 // always 0 for system TTS (no position reporting)
}

func (e *ReadAloudStateEvent) EventType() string { return "read_aloud_state" }

// MediaPlayStateEvent - video playback state change.
type MediaPlayStateEvent struct {
	State    string // "playing", "paused", "ended", "error"
	BookURL  string
	Chapter  string
}

func (e *MediaPlayStateEvent) EventType() string { return "media_play_state" }

// RssRefreshedEvent - RSS feed refresh completed.
type RssRefreshedEvent struct {
	SourceID  string
	NewCount  int
}

func (e *RssRefreshedEvent) EventType() string { return "rss_refreshed" }

// RssRefreshProgressEvent - RSS batch refresh progress.
type RssRefreshProgressEvent struct {
	Done     int
	Total    int
	Current  string
	Failures int
	RefreshID string
}

func (e *RssRefreshProgressEvent) EventType() string { return "rss_refresh_progress" }

// ReplaceRulesChangedEvent - replace rules were modified.
type ReplaceRulesChangedEvent struct {
	IDs []int
}

func (e *ReplaceRulesChangedEvent) EventType() string { return "replace_rules_changed" }

// BackupFinishedEvent - WebDAV backup/restore completed.
type BackupFinishedEvent struct {
	Kind   string
	OK     bool
	Detail string
}

func (e *BackupFinishedEvent) EventType() string { return "backup_finished" }

// BackupProgressEvent - WebDAV backup progress.
type BackupProgressEvent struct {
	Kind   string
	Stage  string // "export", "upload", "verify", "restore"
	Done   int
	Total  int
}

func (e *BackupProgressEvent) EventType() string { return "backup_progress" }

// ExportProgressEvent - EPUB/TXT export progress.
type ExportProgressEvent struct {
	Kind   string
	Stage  string // "chapter_load", "writing", "done"
	Done   int
	Total  int
}

func (e *ExportProgressEvent) EventType() string { return "export_progress" }

// UpdateAvailableEvent - app update found.
type UpdateAvailableEvent struct {
	Version string
	Notes   string
	URL     string
}

func (e *UpdateAvailableEvent) EventType() string { return "update_available" }

// --- Helpers to make event construction convenient ---

func NewServiceStateChanged(serviceID string, state string, err error) *ServiceStateChangedEvent {
	e := &ServiceStateChangedEvent{
		ServiceID: serviceID,
		State:     state,
		Timestamp: float64(time.Now().UnixMilli()) / 1000.0,
	}
	if err != nil {
		e.Error = err.Error()
	}
	return e
}
