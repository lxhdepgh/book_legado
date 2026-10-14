package gateway

import "time"

// Action represents a registered RPC action.
type Action struct {
	Name     string        // e.g., "engine.status"
	Timeout  time.Duration // server budget
	Mode     ActionMode    // call, job, sync
	Summary  string
}

// ActionMode defines how an action is dispatched.
type ActionMode int

const (
	ActionModeCall ActionMode = iota // await result with timeout
	ActionModeJob                    // fire-and-forget
	ActionModeSync                   // direct synchronous call
)

// MethodRegistry provides action lookup by name.
type MethodRegistry map[string]Action

// NewMethodRegistry creates the standard action registry.
// This is the minimal set; full implementation adds all ~60 actions.
func NewMethodRegistry() MethodRegistry {
	return MethodRegistry{
		// Engine actions
		"engine.hello":       {Name: "engine.hello", Timeout: 5 * time.Second, Mode: ActionModeCall, Summary: "Engine hello handshake"},
		"engine.status":      {Name: "engine.status", Timeout: 5 * time.Second, Mode: ActionModeCall, Summary: "Engine status query"},
		"engine.shutdown":    {Name: "engine.shutdown", Timeout: 10 * time.Second, Mode: ActionModeJob, Summary: "Graceful shutdown"},
		"engine.webContentReady": {Name: "engine.webContentReady", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "WebView reverse content ready callback"},

		// Bookshelf actions
		"bookshelf.list":     {Name: "bookshelf.list", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List shelf books"},
		"bookshelf.add":      {Name: "bookshelf.add", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Add book to shelf"},

		// Source actions
		"source.list":        {Name: "source.list", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List all book sources"},
		"source.get":         {Name: "source.get", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get single source by URL"},
		"source.save":        {Name: "source.save", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Save/replace a book source"},
		"source.delete":      {Name: "source.delete", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Delete a book source"},
		"source.enable":      {Name: "source.enable", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Enable/disable a source"},
		"source.batch_enable": {Name: "source.batch_enable", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Batch enable/disable sources"},
		"source.batch_delete": {Name: "source.batch_delete", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Batch delete sources"},
		"source.group":       {Name: "source.group", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Set source group"},
		"source.export":      {Name: "source.export", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Export all sources as JSON"},
		"source.audit":       {Name: "source.audit", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "Static audit on a source"},

		// Import actions
		"import.text":        {Name: "import.text", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "Import from text"},
		"import.url":         {Name: "import.url", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "Import from URL"},

		// Reader actions
		"reader.open":        {Name: "reader.open", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Open a book for reading"},
		"reader.close":       {Name: "reader.close", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Close reading session"},
		"reader.toc":         {Name: "reader.toc", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Get table of contents"},
		"reader.content":     {Name: "reader.content", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "Get chapter content"},
		"reader.position_get": {Name: "reader.position_get", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get reading position"},
		"reader.position_save": {Name: "reader.position_save", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Save reading position"},
		"reader.bookmarks":   {Name: "reader.bookmarks", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List bookmarks"},
		"reader.search_content": {Name: "reader.search_content", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Search within book content"},
		"reader.switch.seeds": {Name: "reader.switch.seeds", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get switch seeds"},
		"reader.switch.scan": {Name: "reader.switch.scan", Timeout: 120 * time.Second, Mode: ActionModeJob, Summary: "Scan for alternatives"},
		"reader.switch.snapshot": {Name: "reader.switch.snapshot", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get scan snapshot"},
		"reader.switch.cancel": {Name: "reader.switch.cancel", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Cancel scan"},
		"reader.switch.apply": {Name: "reader.switch.apply", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Apply switch result"},

		// RSS actions
		"rss.sources":        {Name: "rss.sources", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List RSS sources"},
		"rss.articles":       {Name: "rss.articles", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get articles for source"},
		"rss.article":        {Name: "rss.article", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get single article"},
		"rss.add_source":     {Name: "rss.add_source", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Add RSS source"},
		"rss.remove_source":  {Name: "rss.remove_source", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Remove RSS source"},
		"rss.refresh":        {Name: "rss.refresh", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "Refresh one source"},
		"rss.refresh_all":    {Name: "rss.refresh_all", Timeout: 300 * time.Second, Mode: ActionModeJob, Summary: "Refresh all sources"},
		"rss.mark_read":      {Name: "rss.mark_read", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Mark articles read"},
		"rss.star":           {Name: "rss.star", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Star/unstar article"},

		// Check actions
		"check.start":        {Name: "check.start", Timeout: 600 * time.Second, Mode: ActionModeJob, Summary: "Start source check pipeline"},
		"check.snapshot":     {Name: "check.snapshot", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get check snapshot"},
		"check.cancel":       {Name: "check.cancel", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Cancel running check"},
		"check.source":       {Name: "check.source", Timeout: 120 * time.Second, Mode: ActionModeCall, Summary: "Check single source"},

		// Settings actions
		"settings.read":      {Name: "settings.read", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Read settings"},
		"settings.write":     {Name: "settings.write", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Write a setting"},

		// Theme actions
		"theme.preferences":  {Name: "theme.preferences", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get theme preferences"},
		"theme.save":         {Name: "theme.save", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Save theme preferences"},

		// ReadAloud actions
		"readaloud.speak":    {Name: "readaloud.speak", Timeout: 60 * time.Second, Mode: ActionModeCall, Summary: "TTS speak text"},
		"readaloud.stop":     {Name: "readaloud.stop", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Stop TTS playback"},
		"readaloud.profiles": {Name: "readaloud.profiles", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List TTS profiles"},
		"readaloud.add_profile": {Name: "readaloud.add_profile", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Add TTS profile"},
		"readaloud.remove_profile": {Name: "readaloud.remove_profile", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Remove TTS profile"},

		// Update actions
		"update.check":       {Name: "update.check", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Check for updates"},
		"update.ignore":      {Name: "update.ignore", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Ignore update version"},

		// Browser actions
		"browser.open_page":  {Name: "browser.open_page", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Open browser page"},
		"browser.navigate":   {Name: "browser.navigate", Timeout: 30 * time.Second, Mode: ActionModeCall, Summary: "Navigate browser page"},
		"browser.close":      {Name: "browser.close", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Close browser page"},

		// Replace rule actions
		"replace.list":       {Name: "replace.list", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List replace rules"},
		"replace.add":        {Name: "replace.add", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Add replace rule"},
		"replace.update":     {Name: "replace.update", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Update replace rule"},
		"replace.delete":     {Name: "replace.delete", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Delete replace rule"},
		"replace.toggle_global": {Name: "replace.toggle_global", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Toggle global replace"},

		// Font actions
		"font.scan_system":   {Name: "font.scan_system", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Scan system fonts"},
		"font.add_user":      {Name: "font.add_user", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Add user font"},
		"font.remove":        {Name: "font.remove", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Remove user font"},
		"font.select":        {Name: "font.select", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Select active font"},

		// QR actions
		"qr.generate":        {Name: "qr.generate", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Generate QR code"},
		"qr.decode":          {Name: "qr.decode", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Decode QR from image"},

		// Export actions
		"export.epub":        {Name: "export.epub", Timeout: 300 * time.Second, Mode: ActionModeJob, Summary: "Export book as EPUB"},
		"export.txt":         {Name: "export.txt", Timeout: 300 * time.Second, Mode: ActionModeJob, Summary: "Export book as TXT"},

		// WebDAV actions
		"webdav.backup":      {Name: "webdav.backup", Timeout: 300 * time.Second, Mode: ActionModeJob, Summary: "Backup to WebDAV"},
		"webdav.restore":     {Name: "webdav.restore", Timeout: 300 * time.Second, Mode: ActionModeCall, Summary: "Restore from WebDAV"},
		"webdav.list_backups": {Name: "webdav.list_backups", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "List WebDAV backups"},

		// WebBridge actions
		"webbridge.start":    {Name: "webbridge.start", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Start web bridge server"},
		"webbridge.stop":     {Name: "webbridge.stop", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Stop web bridge server"},
		"webbridge.status":   {Name: "webbridge.status", Timeout: 10 * time.Second, Mode: ActionModeCall, Summary: "Get web bridge status"},
	}
}

// GetAction looks up an action by its dotted name.
func (r MethodRegistry) GetAction(name string) (*Action, bool) {
	a, ok := r[name]
	return &a, ok
}
