package storage

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	_ "modernc.org/sqlite"
)

// StorageService 存储服务（对齐 Python core/storage.py）
type StorageService struct {
	db         *sql.DB
	stateDir   string
	cacheDir   string
}

// NewStorageService 创建存储服务
func NewStorageService(stateDir string) (*StorageService, error) {
	// 创建目录
	if err := os.MkdirAll(stateDir, 0755); err != nil {
		return nil, fmt.Errorf("创建状态目录失败: %w", err)
	}

	cacheDir := filepath.Join(stateDir, "cache")
	if err := os.MkdirAll(cacheDir, 0755); err != nil {
		return nil, fmt.Errorf("创建缓存目录失败: %w", err)
	}

	// 打开数据库
	dbPath := filepath.Join(stateDir, "legodo.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("打开数据库失败: %w", err)
	}

	// 配置连接池
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)

	// 启用 WAL 模式
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, fmt.Errorf("启用 WAL 模式失败: %w", err)
	}

	svc := &StorageService{
		db:       db,
		stateDir: stateDir,
		cacheDir: cacheDir,
	}

	// 初始化 schema
	if err := svc.initSchema(); err != nil {
		return nil, fmt.Errorf("初始化 schema 失败: %w", err)
	}

	return svc, nil
}

// DB 获取数据库连接
func (s *StorageService) DB() *sql.DB {
	return s.db
}

// CacheDir 获取缓存目录
func (s *StorageService) CacheDir() string {
	return s.cacheDir
}

// Close 关闭数据库
func (s *StorageService) Close() error {
	if s.db != nil {
		return s.db.Close()
	}
	return nil
}

// initSchema 初始化数据库 schema
func (s *StorageService) initSchema() error {
	schema := `
	CREATE TABLE IF NOT EXISTS book_sources (
		bookSourceUrl TEXT PRIMARY KEY,
		bookSourceName TEXT NOT NULL,
		bookSourceGroup TEXT,
		bookSourceType INTEGER DEFAULT 0,
		bookUrlPattern TEXT,
		customOrder INTEGER DEFAULT 0,
		enabled BOOLEAN DEFAULT 1,
		enabledExplore BOOLEAN DEFAULT 1,
		jsLib TEXT,
		enabledCookieJar BOOLEAN DEFAULT 1,
		concurrentRate TEXT,
		header TEXT,
		loginUrl TEXT,
		loginUi TEXT,
		loginCheckJs TEXT,
		coverDecodeJs TEXT,
		bookSourceComment TEXT,
		variableComment TEXT,
		lastUpdateTime INTEGER DEFAULT 0,
		respondTime INTEGER DEFAULT 180000,
		weight INTEGER DEFAULT 0,
		exploreUrl TEXT,
		exploreScreen TEXT,
		ruleSearch TEXT,
		ruleExplore TEXT,
		ruleBookInfo TEXT,
		ruleToc TEXT,
		ruleContent TEXT,
		ruleReview TEXT,
		eventListener BOOLEAN DEFAULT 0,
		customButton BOOLEAN DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS books (
		url TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		author TEXT,
		intro TEXT,
		coverUrl TEXT,
		tocUrl TEXT,
		origin TEXT NOT NULL,
		type INTEGER DEFAULT 0,
		totalChapterNum INTEGER DEFAULT 0,
		durChapterIndex INTEGER DEFAULT 0,
		durChapterTitle TEXT,
		lastUpdateTime INTEGER DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS chapters (
		bookUrl TEXT NOT NULL,
		chapterIndex INTEGER NOT NULL,
		title TEXT NOT NULL,
		url TEXT NOT NULL,
		isVip BOOLEAN DEFAULT 0,
		isPay BOOLEAN DEFAULT 0,
		updateTime INTEGER,
		wordCount INTEGER DEFAULT 0,
		PRIMARY KEY (bookUrl, chapterIndex)
	);

	CREATE TABLE IF NOT EXISTS settings (
		key TEXT PRIMARY KEY,
		value TEXT NOT NULL,
		updated_at INTEGER DEFAULT (strftime('%s', 'now') * 1000)
	);

	CREATE INDEX IF NOT EXISTS idx_books_origin ON books(origin);
	CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(bookUrl);
	`

	_, err := s.db.Exec(schema)
	return err
}
