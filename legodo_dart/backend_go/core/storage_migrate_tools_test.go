package core

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

// 旧形状（Go 自造列时期）的四张工具表：replace_rules.global_enabled /
// dict_rules.searchUrl / fonts.is_system / download_tasks.chapter_index。
// 真库是 Python 形状，这些列不存在——旧形状库由 migrateToolsTables 重建，
// 否则服务加载会报 "no such column"（书源/替换/字体整片打不开）。
func seedLegacyToolsDB(t *testing.T, dir string) {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(dir, "legado.db"))
	if err != nil {
		t.Fatalf("打开旧形状库失败: %v", err)
	}
	defer db.Close()

	legacy := `
CREATE TABLE replace_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL DEFAULT '',
	pattern TEXT NOT NULL DEFAULT '',
	replacement TEXT NOT NULL DEFAULT '',
	enabled INTEGER NOT NULL DEFAULT 1,
	global_enabled INTEGER NOT NULL DEFAULT 0
);
INSERT INTO replace_rules (name, pattern, replacement, enabled, global_enabled)
	VALUES ('去广告', '广告', '', 1, 1), ('替换', 'a', 'b', 0, 0);

CREATE TABLE dict_rules (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL DEFAULT '',
	searchUrl TEXT NOT NULL DEFAULT '',
	enabled INTEGER NOT NULL DEFAULT 1
);
INSERT INTO dict_rules (name, searchUrl, enabled) VALUES ('词典甲', 'http://d.test', 1);

CREATE TABLE fonts (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	family TEXT NOT NULL DEFAULT '',
	path TEXT NOT NULL DEFAULT '',
	is_system INTEGER NOT NULL DEFAULT 0
);
INSERT INTO fonts (family, path, is_system)
	VALUES ('Fira', 'C:\fonts\fira.ttf', 0), ('Fira', 'C:\fonts\fira.ttf', 0);

CREATE TABLE download_tasks (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	book_url TEXT NOT NULL DEFAULT '',
	chapter_index INTEGER NOT NULL DEFAULT 0,
	progress REAL NOT NULL DEFAULT 0,
	created_at REAL NOT NULL DEFAULT 0
);
INSERT INTO download_tasks (book_url, chapter_index, progress, created_at)
	VALUES ('http://book.test', 3, 0.5, 111);
`
	if _, err := db.Exec(legacy); err != nil {
		t.Fatalf("建旧形状表失败: %v", err)
	}
}

func tableColumns(t *testing.T, s *StorageService, table string) map[string]bool {
	t.Helper()
	rows, err := s.Query("PRAGMA table_info(" + table + ")")
	if err != nil {
		t.Fatalf("查 %s 列失败: %v", table, err)
	}
	defer rows.Close()
	cols := make(map[string]bool)
	for rows.Next() {
		var cid, notnull, pk int
		var name, ctype string
		var dflt interface{}
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			t.Fatalf("扫描 %s 列失败: %v", table, err)
		}
		cols[name] = true
	}
	return cols
}

func TestStorageMigratesLegacyToolsTables(t *testing.T) {
	dir := t.TempDir()
	seedLegacyToolsDB(t, dir)

	svc, err := NewStorageService(dir)
	if err != nil {
		t.Fatalf("打开旧形状库失败（迁移没跑通）: %v", err)
	}
	t.Cleanup(func() { _ = svc.Close() })

	// replace_rules：旧 global_enabled 列应消失，契约列补齐；行搬运到位。
	repCols := tableColumns(t, svc, "replace_rules")
	if repCols["global_enabled"] {
		t.Error("replace_rules.global_enabled 还在 —— 旧形状没被重建")
	}
	for _, col := range []string{"scope", "is_regex", "created_at", "updated_at"} {
		if !repCols[col] {
			t.Errorf("replace_rules 缺契约列 %s", col)
		}
	}
	rows, err := svc.Query("SELECT name, scope, is_regex, enabled FROM replace_rules ORDER BY id")
	if err != nil {
		t.Fatalf("读迁移后的 replace_rules 失败: %v", err)
	}
	type repRow struct {
		name    string
		scope   string
		isRegex int
		enabled int
	}
	var reps []repRow
	for rows.Next() {
		var r repRow
		if err := rows.Scan(&r.name, &r.scope, &r.isRegex, &r.enabled); err != nil {
			t.Fatalf("扫描 replace_rules 行失败: %v", err)
		}
		reps = append(reps, r)
	}
	rows.Close()
	if len(reps) != 2 {
		t.Fatalf("replace_rules 应搬来 2 行，得到 %d", len(reps))
	}
	if reps[0].name != "去广告" || reps[0].scope != "content" || reps[0].isRegex != 1 || reps[0].enabled != 1 {
		t.Errorf("第 1 条搬运不对: %+v（scope 应补 'content'、is_regex 应补 1）", reps[0])
	}
	if reps[1].enabled != 0 {
		t.Errorf("第 2 条 enabled=0 应保留，得到 %d", reps[1].enabled)
	}

	// fonts：旧 is_system 消失，family 同时补进 name，重复 path 只留一行。
	fontCols := tableColumns(t, svc, "fonts")
	if fontCols["is_system"] {
		t.Error("fonts.is_system 还在 —— 旧形状没被重建")
	}
	if !fontCols["name"] || !fontCols["family"] {
		t.Error("fonts 缺 name/family 契约列")
	}
	var fontCount int
	var name, family, path string
	if err := svc.QueryRow("SELECT COUNT(*) FROM fonts").Scan(&fontCount); err != nil {
		t.Fatalf("数 fonts 行失败: %v", err)
	}
	if fontCount != 1 {
		t.Errorf("重复 path 应只留 1 行（UNIQUE(path)），得到 %d", fontCount)
	}
	if err := svc.QueryRow("SELECT name, family, path FROM fonts").Scan(&name, &family, &path); err != nil {
		t.Fatalf("读 fonts 行失败: %v", err)
	}
	if name != "Fira" || family != "Fira" || path != `C:\fonts\fira.ttf` {
		t.Errorf("fonts 搬运不对: name=%q family=%q path=%q", name, family, path)
	}

	// dict_rules：searchUrl 消失，url/rules_json 就位。
	dictCols := tableColumns(t, svc, "dict_rules")
	if dictCols["searchUrl"] {
		t.Error("dict_rules.searchUrl 还在 —— 旧形状没被重建")
	}
	if !dictCols["url"] || !dictCols["rules_json"] {
		t.Error("dict_rules 缺 url/rules_json 契约列")
	}

	// download_tasks：chapter_index 消失，kind/status/files 就位，行搬运到位。
	taskCols := tableColumns(t, svc, "download_tasks")
	if taskCols["chapter_index"] {
		t.Error("download_tasks.chapter_index 还在 —— 旧形状没被重建")
	}
	for _, col := range []string{"kind", "status", "files"} {
		if !taskCols[col] {
			t.Errorf("download_tasks 缺契约列 %s", col)
		}
	}
	var bookURL string
	var progress float64
	if err := svc.QueryRow("SELECT book_url, progress FROM download_tasks").Scan(&bookURL, &progress); err != nil {
		t.Fatalf("读 download_tasks 行失败: %v", err)
	}
	if bookURL != "http://book.test" || progress != 0.5 {
		t.Errorf("download_tasks 搬运不对: book_url=%q progress=%v", bookURL, progress)
	}

	// 尾索引在迁移之后建：旧形状表上不可能挂得住 scope 列索引。
	var idxCount int
	if err := svc.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_replace_rules_scope'",
	).Scan(&idxCount); err != nil {
		t.Fatalf("查索引失败: %v", err)
	}
	if idxCount != 1 {
		t.Error("idx_replace_rules_scope 不存在 —— 尾索引没跑在迁移之后")
	}
}
