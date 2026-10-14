package core

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	_ "modernc.org/sqlite"
)

// StorageService manages the SQLite main database and file directories.
// Aligns with Python core/storage.py StorageService.
type StorageService struct {
	db       *sql.DB
	mu       sync.Mutex // per-statement lock (aligns with Python's LockedConnection)
	stateDir string
	cacheDir string
}

// NewStorageService creates a storage service with its own data/cache dirs.
func NewStorageService(stateDir string) (*StorageService, error) {
	if err := os.MkdirAll(stateDir, 0755); err != nil {
		return nil, fmt.Errorf("create state dir: %w", err)
	}

	cacheDir := filepath.Join(stateDir, "cache")
	if err := os.MkdirAll(cacheDir, 0755); err != nil {
		return nil, fmt.Errorf("create cache dir: %w", err)
	}

	dbPath := filepath.Join(stateDir, "legado.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}

	// Single-writer pattern like Python's check_same_thread=False + RLock
	db.SetMaxOpenConns(1)

	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("enable WAL: %w", err)
	}

	svc := &StorageService{
		db:       db,
		stateDir: stateDir,
		cacheDir: cacheDir,
	}

	if err := svc.ensureSchema(); err != nil {
		db.Close()
		return nil, fmt.Errorf("init schema: %w", err)
	}

	return svc, nil
}

// DB returns the underlying database handle. Callers must not share it.
func (s *StorageService) DB() *sql.DB { return s.db }

// CacheDir returns the cache directory path.
func (s *StorageService) CacheDir() string { return s.cacheDir }

// DataDir returns the state/data directory path.
func (s *StorageService) DataDir() string { return s.stateDir }

// DownloadDir returns the downloads subdirectory.
func (s *StorageService) DownloadDir() string {
	d := filepath.Join(s.cacheDir, "downloads")
	os.MkdirAll(d, 0755)
	return d
}

// ReaderCacheDir returns the reader-cache subdirectory.
func (s *StorageService) ReaderCacheDir() string {
	d := filepath.Join(s.cacheDir, "reader-cache")
	os.MkdirAll(d, 0755)
	return d
}

// ImageCacheDir returns the image-cache subdirectory.
func (s *StorageService) ImageCacheDir() string {
	d := filepath.Join(s.cacheDir, "image-cache")
	os.MkdirAll(d, 0755)
	return d
}

// BackgroundsDir returns the backgrounds subdirectory.
func (s *StorageService) BackgroundsDir() string {
	d := filepath.Join(s.cacheDir, "backgrounds")
	os.MkdirAll(d, 0755)
	return d
}

// Close shuts down the database connection.
func (s *StorageService) Close() error {
	return s.db.Close()
}

// Execute runs a single SQL statement with locking.
func (s *StorageService) Execute(query string, args ...interface{}) (sql.Result, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.db.Exec(query, args...)
}

// QueryRow executes a query that returns a single row.
func (s *StorageService) QueryRow(query string, args ...interface{}) *sql.Row {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.db.QueryRow(query, args...)
}

// Query executes a query that returns rows.
func (s *StorageService) Query(query string, args ...interface{}) (*sql.Rows, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.db.Query(query, args...)
}

// Transaction runs fn within an explicit transaction (BEGIN IMMEDIATE).
// On panic or error, the transaction is rolled back.
func (s *StorageService) Transaction(fn func(*sql.Tx) error) error {
	s.mu.Lock()
	tx, err := s.db.Begin()
	s.mu.Unlock()
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			tx.Rollback() // nolint:errcheck
		}
	}()
	if err := fn(tx); err != nil {
		return err
	}
	committed = true
	return tx.Commit()
}

// ensureSchema creates all tables for the latest schema version (v7).
// Aligns with Python core/storage.py ensure_schema().
func (s *StorageService) ensureSchema() error {
	schema := `
-- v1: settings (key-value), cookies (JS cookie jar persistence)
CREATE TABLE IF NOT EXISTS settings (
	key TEXT PRIMARY KEY,
	value TEXT NOT NULL,
	updated_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TABLE IF NOT EXISTS cookies (
	name TEXT NOT NULL,
	host TEXT NOT NULL,
	path TEXT NOT NULL DEFAULT '/',
	value TEXT NOT NULL,
	expires INTEGER,
	secure INTEGER DEFAULT 0,
	UNIQUE(name, host, path)
);

-- v2: books (book metadata + continuous scroll progress), bookmarks
CREATE TABLE IF NOT EXISTS books (
	book_url TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	author TEXT,
	kind TEXT,
	cover_url TEXT,
	source_url TEXT,
	in_bookshelf INTEGER DEFAULT 0,
	reading TEXT DEFAULT '{}',
	vars TEXT DEFAULT '{}',
	payload TEXT DEFAULT '{}',
	updated_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
	play_position REAL DEFAULT 0,
	play_chapter_index INTEGER DEFAULT -1
);

CREATE TABLE IF NOT EXISTS bookmarks (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	book_url TEXT NOT NULL,
	chapter_index INTEGER NOT NULL,
	offset REAL DEFAULT 0,
	summary TEXT,
	created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
	UNIQUE(book_url, chapter_index, offset)
);

-- v3: audit_log (static source audit results)
CREATE TABLE IF NOT EXISTS audit_log (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	source_url TEXT NOT NULL,
	audit_data TEXT NOT NULL,
	created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

-- v4: tts_profiles, rss_sources, rss_articles, books.play_position
CREATE TABLE IF NOT EXISTS tts_profiles (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL,
	url TEXT,
	headers TEXT,
	engine_type TEXT DEFAULT 'builtin'
);

CREATE TABLE IF NOT EXISTS rss_sources (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL,
	url TEXT NOT NULL UNIQUE,
	"group" TEXT,
	enabled INTEGER DEFAULT 1,
	rss_type INTEGER DEFAULT 0,
	last_update_time INTEGER DEFAULT 0,
	custom_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rss_articles (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	source_id INTEGER NOT NULL,
	title TEXT NOT NULL,
	url TEXT,
	author TEXT,
	pub_date TEXT,
	image TEXT,
	read INTEGER DEFAULT 0,
	starred INTEGER DEFAULT 0,
	text TEXT,
	created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
	UNIQUE(source_id, url)
);

-- v5: replace_rules, dict_rules, fonts, download_tasks
CREATE TABLE IF NOT EXISTS replace_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL,
	pattern TEXT NOT NULL,
	replacement TEXT NOT NULL,
	enabled INTEGER DEFAULT 1,
	global_enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dict_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL,
	searchUrl TEXT NOT NULL,
	resultJsonRoot TEXT,
	titleRule TEXT,
	contentRule TEXT,
	nextPageUrlRule TEXT,
	enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fonts (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	family TEXT NOT NULL,
	path TEXT NOT NULL,
	is_system INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS download_tasks (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	book_url TEXT NOT NULL,
	chapter_index INTEGER NOT NULL,
	status INTEGER DEFAULT 0,
	progress REAL DEFAULT 0,
	created_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

-- v6: book_sources, bookshelf, search_books (migration from JSON files)
CREATE TABLE IF NOT EXISTS book_sources (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL,
	url TEXT NOT NULL UNIQUE,
	"group" TEXT,
	enabled INTEGER DEFAULT 1,
	custom_order INTEGER DEFAULT 0,
	check_state INTEGER DEFAULT 0,
	respond_time INTEGER DEFAULT 180000,
	json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookshelf (
	book_url TEXT PRIMARY KEY,
	source_url TEXT,
	name TEXT NOT NULL,
	author TEXT,
	kind TEXT,
	cover_url TEXT,
	in_bookshelf INTEGER DEFAULT 1,
	reading TEXT DEFAULT '{}',
	vars TEXT DEFAULT '{}',
	payload TEXT DEFAULT '{}',
	updated_at INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TABLE IF NOT EXISTS search_books (
	book_url TEXT NOT NULL,
	name TEXT NOT NULL,
	author TEXT,
	kind TEXT,
	origin TEXT,
	origin_group TEXT,
	origin_order INTEGER DEFAULT 0,
	saved_at INTEGER,
	payload TEXT,
	updated_at INTEGER,
	PRIMARY KEY(book_url, origin, saved_at)
);

-- v7: books.play_chapter_index (separate from reading progress)
-- Already included in v2 schema above as play_chapter_index

-- Runtime FTS5 virtual table for content search (Created dynamically by ContentSearchService)
`;

	_, err := s.db.Exec(schema)
	return err
}

// SchemaVersion returns the current database schema version.
func (s *StorageService) SchemaVersion() (int, error) {
	var version int
	err := s.db.QueryRow("PRAGMA user_version").Scan(&version)
	return version, err
}

// SetSchemaVersion sets the database schema version.
func (s *StorageService) SetSchemaVersion(v int) error {
	_, err := s.db.Exec(fmt.Sprintf("PRAGMA user_version = %d", v))
	return err
}
