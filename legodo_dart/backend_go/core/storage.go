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
//
// LEGADO_BOOKSOURCE_CACHE_DIR 是 Python app_paths.py 的既有契约：设了就用它
// 顶掉 <data>/cache（语料与打包测试都靠这个把读写钉在临时目录）。
func NewStorageService(stateDir string) (*StorageService, error) {
	if err := os.MkdirAll(stateDir, 0755); err != nil {
		return nil, fmt.Errorf("create state dir: %w", err)
	}

	cacheDir := filepath.Join(stateDir, "cache")
	if override := os.Getenv("LEGADO_BOOKSOURCE_CACHE_DIR"); override != "" {
		cacheDir = override
	}
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
-- 结构化列对齐 Python schema（_migrate_v2/v4/v7/v8 的最终形状）：进度是
-- chapter_index/scroll_offset/scroll_ratio/chapter_pos/chapter_paragraph/font_size；
-- 早先这里自造的 reading/vars/payload 三列在真库里并不存在，对真库执行会直接报错。
CREATE TABLE IF NOT EXISTS books (
	book_url TEXT PRIMARY KEY,
	source_url TEXT NOT NULL DEFAULT '',
	name TEXT NOT NULL DEFAULT '',
	author TEXT NOT NULL DEFAULT '',
	kind TEXT NOT NULL DEFAULT '',
	cover_url TEXT NOT NULL DEFAULT '',
	chapter_index INTEGER NOT NULL DEFAULT 0,
	scroll_offset REAL NOT NULL DEFAULT 0,
	scroll_ratio REAL NOT NULL DEFAULT 0,
	chapter_pos INTEGER NOT NULL DEFAULT 0,
	font_size INTEGER NOT NULL DEFAULT 16,
	in_bookshelf INTEGER NOT NULL DEFAULT 1,
	shelf_group TEXT NOT NULL DEFAULT '',
	updated_at REAL NOT NULL DEFAULT 0,
	play_position REAL NOT NULL DEFAULT 0,
	play_chapter_index INTEGER NOT NULL DEFAULT 0,
	chapter_paragraph INTEGER
);

CREATE TABLE IF NOT EXISTS bookmarks (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	book_url TEXT NOT NULL,
	chapter_index INTEGER NOT NULL DEFAULT 0,
	offset REAL NOT NULL DEFAULT 0,
	summary TEXT NOT NULL DEFAULT '',
	created_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bookmarks_book ON bookmarks(book_url);

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

-- Python 形状（app/core/storage.py _migrate_v4）：源 id 即 feed 地址（TEXT 主键），
-- wire 上 rss.* 那一族全按这个身份（golden rss.sources 的 id 就是 url）。
CREATE TABLE IF NOT EXISTS rss_sources (
	id               TEXT PRIMARY KEY,
	name             TEXT NOT NULL DEFAULT '',
	url              TEXT NOT NULL DEFAULT '',
	enabled          INTEGER NOT NULL DEFAULT 1,
	source_group     TEXT NOT NULL DEFAULT '',
	custom_order     INTEGER NOT NULL DEFAULT 0,
	type             INTEGER NOT NULL DEFAULT 0,
	last_update_time REAL   NOT NULL DEFAULT 0,
	rule_articles    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rss_articles (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	source_id   TEXT NOT NULL,
	title       TEXT NOT NULL DEFAULT '',
	url         TEXT NOT NULL DEFAULT '',
	author      TEXT NOT NULL DEFAULT '',
	pub_date    TEXT NOT NULL DEFAULT '',
	content     TEXT NOT NULL DEFAULT '',
	image       TEXT NOT NULL DEFAULT '',
	read_flag   INTEGER NOT NULL DEFAULT 0,
	star_flag   INTEGER NOT NULL DEFAULT 0,
	updated_at  REAL   NOT NULL DEFAULT 0,
	UNIQUE(source_id, url)
);
-- 注：rss 的两个索引不放在这里——旧形状的库还没迁到 star_flag 列，
-- 建索引会先于 migrateRssTables 炸掉 ensureSchema，见 ensureSchema 尾部。

-- v5: replace_rules, dict_rules, fonts, download_tasks
-- 形状对齐 Python _migrate_v5（app/storage/schema.py）：replace_rules 的
-- scope（content/title，对应 Legado isTitle）/is_regex 是契约列，"全局叠加
-- 开关"不是列而是 settings 键 replace.global.enabled；fonts 只登记用户字体
-- （系统字体只读枚举、不落库）。早先这里自造的 global_enabled / is_system
-- 列在真库里并不存在，对真库执行会直接报错（旧形状由 migrateToolsTables
-- 在 ensureSchema 尾部重建）。
CREATE TABLE IF NOT EXISTS replace_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL DEFAULT '',
	pattern TEXT NOT NULL DEFAULT '',
	replacement TEXT NOT NULL DEFAULT '',
	scope TEXT NOT NULL DEFAULT 'content',
	is_regex INTEGER NOT NULL DEFAULT 1,
	enabled INTEGER NOT NULL DEFAULT 1,
	created_at REAL NOT NULL DEFAULT 0,
	updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dict_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL DEFAULT '',
	url TEXT NOT NULL DEFAULT '',
	rules_json TEXT NOT NULL DEFAULT '{}',
	enabled INTEGER NOT NULL DEFAULT 1,
	created_at REAL NOT NULL DEFAULT 0,
	updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fonts (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL DEFAULT '',
	family TEXT NOT NULL DEFAULT '',
	path TEXT NOT NULL DEFAULT '',
	UNIQUE(path)
);

CREATE TABLE IF NOT EXISTS download_tasks (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	book_url TEXT NOT NULL DEFAULT '',
	kind TEXT NOT NULL DEFAULT '',
	status TEXT NOT NULL DEFAULT 'pending',
	progress REAL NOT NULL DEFAULT 0,
	files TEXT NOT NULL DEFAULT '[]',
	created_at REAL NOT NULL DEFAULT 0
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

-- One row per book_url (INSERT OR REPLACE semantics), alignment with
-- Python schema v6: search_books(book_url PK, source_group, saved_at ms).
CREATE TABLE IF NOT EXISTS search_books (
	book_url TEXT PRIMARY KEY,
	name TEXT NOT NULL DEFAULT '',
	author TEXT NOT NULL DEFAULT '',
	kind TEXT NOT NULL DEFAULT '',
	origin TEXT NOT NULL DEFAULT '',
	source_group TEXT NOT NULL DEFAULT '',
	origin_order INTEGER NOT NULL DEFAULT 0,
	saved_at INTEGER NOT NULL DEFAULT 0,
	payload TEXT NOT NULL DEFAULT '{}',
	updated_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_search_books_name_author
	ON search_books(name, author, origin_order);

CREATE INDEX IF NOT EXISTS idx_search_books_origin
	ON search_books(origin);

-- v7: books.play_chapter_index (separate from reading progress)
-- Already included in v2 schema above as play_chapter_index

-- Runtime FTS5 virtual table for content search (Created dynamically by ContentSearchService)
`

	_, err := s.db.Exec(schema)
	if err != nil {
		return err
	}
	if err := s.migrateSearchBooks(); err != nil {
		return err
	}
	if err := s.migrateRssTables(); err != nil {
		return err
	}
	if err := s.migrateToolsTables(); err != nil {
		return err
	}
	// 索引等迁移之后建：旧形状的 rss_articles 还没有 star_flag 列，
	// 旧形状的 replace_rules/download_tasks 也还没有契约列。
	_, err = s.db.Exec(`CREATE INDEX IF NOT EXISTS idx_rss_articles_source
		ON rss_articles(source_id);
	CREATE INDEX IF NOT EXISTS idx_rss_articles_star
		ON rss_articles(star_flag);
	CREATE INDEX IF NOT EXISTS idx_replace_rules_scope
		ON replace_rules(scope, enabled);
	CREATE INDEX IF NOT EXISTS idx_download_tasks_book
		ON download_tasks(book_url)`)
	return err
}

// migrateSearchBooks rebuilds a pre-FFI search_books table (PK included
// origin+saved_at and carried an origin_group column) into the one-row-per-
// book_url shape. The old shape was never written by any Go code path, but
// INSERT OR REPLACE against it would silently pile up duplicate rows per
// book_url, so an existing DB gets rebuilt instead of patched.
func (s *StorageService) migrateSearchBooks() error {
	rows, err := s.db.Query(`PRAGMA table_info(search_books)`)
	if err != nil {
		return err
	}
	hasOriginGroup := false
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt interface{}
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			rows.Close()
			return err
		}
		if name == "origin_group" {
			hasOriginGroup = true
		}
	}
	rows.Close()
	if !hasOriginGroup {
		return nil
	}

	return s.Transaction(func(tx *sql.Tx) error {
		if _, err := tx.Exec(`CREATE TABLE search_books_v2 (
			book_url TEXT PRIMARY KEY,
			name TEXT NOT NULL DEFAULT '',
			author TEXT NOT NULL DEFAULT '',
			kind TEXT NOT NULL DEFAULT '',
			origin TEXT NOT NULL DEFAULT '',
			source_group TEXT NOT NULL DEFAULT '',
			origin_order INTEGER NOT NULL DEFAULT 0,
			saved_at INTEGER NOT NULL DEFAULT 0,
			payload TEXT NOT NULL DEFAULT '{}',
			updated_at REAL NOT NULL DEFAULT 0
		)`); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT OR REPLACE INTO search_books_v2
			(book_url, name, author, kind, origin, source_group, origin_order,
			 saved_at, payload, updated_at)
			SELECT book_url, name, author, COALESCE(kind,''),
			       COALESCE(origin,''), COALESCE(origin_group,''),
			       COALESCE(origin_order,0), COALESCE(saved_at,0),
			       COALESCE(payload,'{}'), COALESCE(updated_at,0)
			FROM search_books ORDER BY saved_at`); err != nil {
			return err
		}
		if _, err := tx.Exec(`DROP TABLE search_books`); err != nil {
			return err
		}
		if _, err := tx.Exec(`ALTER TABLE search_books_v2 RENAME TO search_books`); err != nil {
			return err
		}
		if _, err := tx.Exec(`CREATE INDEX IF NOT EXISTS idx_search_books_name_author
			ON search_books(name, author, origin_order)`); err != nil {
			return err
		}
		_, err := tx.Exec(`CREATE INDEX IF NOT EXISTS idx_search_books_origin
			ON search_books(origin)`)
		return err
	})
}

// migrateRssTables rebuilds a pre-FFI rss_sources / rss_articles pair into the
// Python shape: source id = feed url (TEXT), source_group / type /
// read_flag / star_flag column names, content instead of text. The old shape
// was only ever written by the stub Go rss service; the wire contract
// (golden rss.sources.json / rss.articles.json) keys off the Python shape, so
// an existing DB is rebuilt with data carried over instead of patched.
// Orphan articles (their source row was already gone) are dropped — the
// delete path is cascading, so they carry no meaning.
func (s *StorageService) migrateRssTables() error {
	rows, err := s.db.Query(`PRAGMA table_info(rss_sources)`)
	if err != nil {
		return err
	}
	oldShape := false
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt interface{}
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			rows.Close()
			return err
		}
		if name == "rss_type" {
			oldShape = true
		}
	}
	rows.Close()
	if !oldShape {
		return nil
	}

	return s.Transaction(func(tx *sql.Tx) error {
		if _, err := tx.Exec(`CREATE TABLE rss_articles_v2 (
			id          INTEGER PRIMARY KEY AUTOINCREMENT,
			source_id   TEXT NOT NULL,
			title       TEXT NOT NULL DEFAULT '',
			url         TEXT NOT NULL DEFAULT '',
			author      TEXT NOT NULL DEFAULT '',
			pub_date    TEXT NOT NULL DEFAULT '',
			content     TEXT NOT NULL DEFAULT '',
			image       TEXT NOT NULL DEFAULT '',
			read_flag   INTEGER NOT NULL DEFAULT 0,
			star_flag   INTEGER NOT NULL DEFAULT 0,
			updated_at  REAL   NOT NULL DEFAULT 0,
			UNIQUE(source_id, url)
		)`); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO rss_articles_v2
			(id, source_id, title, url, author, pub_date, content, image,
			 read_flag, star_flag, updated_at)
			SELECT a.id, src.url, COALESCE(a.title,''), COALESCE(a.url,''),
			       COALESCE(a.author,''), COALESCE(a.pub_date,''),
			       COALESCE(a.text,''), COALESCE(a.image,''),
			       COALESCE(a.read,0), COALESCE(a.starred,0),
			       COALESCE(a.created_at,0) / 1000.0
			FROM rss_articles a JOIN rss_sources src ON src.id = a.source_id`); err != nil {
			return err
		}
		if _, err := tx.Exec(`DROP TABLE rss_articles`); err != nil {
			return err
		}
		if _, err := tx.Exec(`ALTER TABLE rss_articles_v2 RENAME TO rss_articles`); err != nil {
			return err
		}
		if _, err := tx.Exec(`CREATE INDEX IF NOT EXISTS idx_rss_articles_source
			ON rss_articles(source_id)`); err != nil {
			return err
		}
		if _, err := tx.Exec(`CREATE INDEX IF NOT EXISTS idx_rss_articles_star
			ON rss_articles(star_flag)`); err != nil {
			return err
		}

		if _, err := tx.Exec(`CREATE TABLE rss_sources_v2 (
			id               TEXT PRIMARY KEY,
			name             TEXT NOT NULL DEFAULT '',
			url              TEXT NOT NULL DEFAULT '',
			enabled          INTEGER NOT NULL DEFAULT 1,
			source_group     TEXT NOT NULL DEFAULT '',
			custom_order     INTEGER NOT NULL DEFAULT 0,
			type             INTEGER NOT NULL DEFAULT 0,
			last_update_time REAL   NOT NULL DEFAULT 0,
			rule_articles    TEXT NOT NULL DEFAULT ''
		)`); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO rss_sources_v2
			(id, name, url, enabled, source_group, custom_order, type, last_update_time)
			SELECT url, COALESCE(name,''), url, COALESCE(enabled,1),
			       COALESCE("group",''), COALESCE(custom_order,0),
			       COALESCE(rss_type,0), COALESCE(last_update_time,0)
			FROM rss_sources WHERE url IS NOT NULL AND url <> ''`); err != nil {
			return err
		}
		if _, err := tx.Exec(`DROP TABLE rss_sources`); err != nil {
			return err
		}
		_, err := tx.Exec(`ALTER TABLE rss_sources_v2 RENAME TO rss_sources`)
		return err
	})
}

// migrateToolsTables rebuilds pre-FFI replace_rules / dict_rules / fonts /
// download_tasks tables (self-invented columns: global_enabled, searchUrl,
// is_system, chapter_index) into the Python shape from _migrate_v5. The Go
// tool services query the Python shape (scope/is_regex, name/family/path),
// so an existing DB built by an earlier Go build is rebuilt with the data
// carried over instead of patched. Values that have no counterpart in the
// new shape (per-rule global_enabled, integer status, …) are dropped — the
// global apply toggle lives in the settings key replace.global.enabled.
func (s *StorageService) migrateToolsTables() error {
	has := func(table, column string) (bool, error) {
		rows, err := s.db.Query(fmt.Sprintf("PRAGMA table_info(%s)", table))
		if err != nil {
			return false, err
		}
		defer rows.Close()
		for rows.Next() {
			var cid, notnull, pk int
			var name, ctype string
			var dflt interface{}
			if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
				return false, err
			}
			if name == column {
				return true, nil
			}
		}
		return false, rows.Err()
	}

	legacyReplace, err := has("replace_rules", "global_enabled")
	if err != nil {
		return err
	}
	if legacyReplace {
		if err := s.Transaction(func(tx *sql.Tx) error {
			if _, err := tx.Exec(`CREATE TABLE replace_rules_v2 (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL DEFAULT '',
				pattern TEXT NOT NULL DEFAULT '',
				replacement TEXT NOT NULL DEFAULT '',
				scope TEXT NOT NULL DEFAULT 'content',
				is_regex INTEGER NOT NULL DEFAULT 1,
				enabled INTEGER NOT NULL DEFAULT 1,
				created_at REAL NOT NULL DEFAULT 0,
				updated_at REAL NOT NULL DEFAULT 0
			)`); err != nil {
				return err
			}
			if _, err := tx.Exec(`INSERT INTO replace_rules_v2
				(id, name, pattern, replacement, scope, is_regex, enabled)
				SELECT id, COALESCE(name,''), COALESCE(pattern,''),
				       COALESCE(replacement,''), 'content', 1, COALESCE(enabled,1)
				FROM replace_rules`); err != nil {
				return err
			}
			if _, err := tx.Exec(`DROP TABLE replace_rules`); err != nil {
				return err
			}
			_, err := tx.Exec(`ALTER TABLE replace_rules_v2 RENAME TO replace_rules`)
			return err
		}); err != nil {
			return err
		}
	}

	legacyDict, err := has("dict_rules", "searchUrl")
	if err != nil {
		return err
	}
	if legacyDict {
		if err := s.Transaction(func(tx *sql.Tx) error {
			if _, err := tx.Exec(`CREATE TABLE dict_rules_v2 (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL DEFAULT '',
				url TEXT NOT NULL DEFAULT '',
				rules_json TEXT NOT NULL DEFAULT '{}',
				enabled INTEGER NOT NULL DEFAULT 1,
				created_at REAL NOT NULL DEFAULT 0,
				updated_at REAL NOT NULL DEFAULT 0
			)`); err != nil {
				return err
			}
			if _, err := tx.Exec(`INSERT INTO dict_rules_v2 (id, name, enabled)
				SELECT id, COALESCE(name,''), COALESCE(enabled,1) FROM dict_rules`); err != nil {
				return err
			}
			if _, err := tx.Exec(`DROP TABLE dict_rules`); err != nil {
				return err
			}
			_, err := tx.Exec(`ALTER TABLE dict_rules_v2 RENAME TO dict_rules`)
			return err
		}); err != nil {
			return err
		}
	}

	legacyFonts, err := has("fonts", "is_system")
	if err != nil {
		return err
	}
	if legacyFonts {
		if err := s.Transaction(func(tx *sql.Tx) error {
			if _, err := tx.Exec(`CREATE TABLE fonts_v2 (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				name TEXT NOT NULL DEFAULT '',
				family TEXT NOT NULL DEFAULT '',
				path TEXT NOT NULL DEFAULT '',
				UNIQUE(path)
			)`); err != nil {
				return err
			}
			// 旧形状没有 name，family 既是显示名也是族名；重复 path 只留一行。
			if _, err := tx.Exec(`INSERT OR IGNORE INTO fonts_v2 (id, name, family, path)
				SELECT id, COALESCE(family,''), COALESCE(family,''), COALESCE(path,'')
				FROM fonts`); err != nil {
				return err
			}
			if _, err := tx.Exec(`DROP TABLE fonts`); err != nil {
				return err
			}
			_, err := tx.Exec(`ALTER TABLE fonts_v2 RENAME TO fonts`)
			return err
		}); err != nil {
			return err
		}
	}

	legacyTasks, err := has("download_tasks", "chapter_index")
	if err != nil {
		return err
	}
	if legacyTasks {
		if err := s.Transaction(func(tx *sql.Tx) error {
			if _, err := tx.Exec(`CREATE TABLE download_tasks_v2 (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				book_url TEXT NOT NULL DEFAULT '',
				kind TEXT NOT NULL DEFAULT '',
				status TEXT NOT NULL DEFAULT 'pending',
				progress REAL NOT NULL DEFAULT 0,
				files TEXT NOT NULL DEFAULT '[]',
				created_at REAL NOT NULL DEFAULT 0
			)`); err != nil {
				return err
			}
			if _, err := tx.Exec(`INSERT INTO download_tasks_v2
				(id, book_url, progress, created_at)
				SELECT id, COALESCE(book_url,''), COALESCE(progress,0),
				       COALESCE(created_at,0)
				FROM download_tasks`); err != nil {
				return err
			}
			if _, err := tx.Exec(`DROP TABLE download_tasks`); err != nil {
				return err
			}
			_, err := tx.Exec(`ALTER TABLE download_tasks_v2 RENAME TO download_tasks`)
			return err
		}); err != nil {
			return err
		}
	}

	return nil
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
