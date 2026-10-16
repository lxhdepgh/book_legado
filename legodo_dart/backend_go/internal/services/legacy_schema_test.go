package services

// 旧库（Python 后端那一份）核心表形状 × Go 写路径的钉子。
//
// 这一族的问题不是"迁移代码没写"，而是它从来没被跑过：所有既有测试的库都是
// `core.NewStorageService` 现场建的，`CREATE TABLE IF NOT EXISTS` 走的是"新建"那一条，
// 于是 Go 自造的 `settings(key,value,updated_at)` / `book_sources(... url UNIQUE ...)`
// 形状永远成立。开发者机器上的真库是 Python 时代建的，那两张表形状不同，而
// `IF NOT EXISTS` 撞上已存在的表就原样留着 —— 写侧每一条语句都红，读侧一个列都不碰，
// 所以应用看着是活的，只有"存东西"是坏的（t114 探针在真库副本上量到 4/14 条写动作
// 只在 legacy 臂坏：settings 写、书源写、书源导入追加、字体选择）。
//
// 所以这里的种子表形状照抄实测的那一份（下面两条 DDL 来自真库 sqlite_master，
// 2026-10-16 以 mode=ro 读回），并且每颗钉子都先跑一条"迁之前必红"的底价：
// 种子如果长成了新形状，底价就会红，测试不至于悄悄退化成"打开了一个新库"。
//
// 这颗钉子当场又挖出第二条旧库形状分歧：旧库的 `"group"` 列是 NOT NULL DEFAULT ''，
// 而 Go 把可空的 bookSourceGroup 原样当参数递进去，驱动写的是显式 NULL（DEFAULT 只在
// 省略列时生效），于是每条没带分组的书源写入都撞 `NOT NULL constraint failed`。
// 它在旧库上一直被前面那条 `ON CONFLICT clause does not match` 挡着 —— 编译期错误先于
// 运行期错误 —— 补了唯一索引才露出来。写侧现在落 ''（见 sourceGroupArg）。

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

const legacySettingsDDL = `CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT)`

const legacyBookSourcesDDL = `CREATE TABLE book_sources (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL DEFAULT '',
  url          TEXT NOT NULL DEFAULT '',
  "group"      TEXT NOT NULL DEFAULT '',
  enabled      INTEGER NOT NULL DEFAULT 1,
  custom_order INTEGER NOT NULL DEFAULT 0,
  check_state  TEXT NOT NULL DEFAULT '',
  respond_time INTEGER NOT NULL DEFAULT 0,
  json         TEXT NOT NULL DEFAULT '{}')`

const legacyBookSourcesGroupIndex = "CREATE INDEX idx_book_sources_group ON book_sources(`group`, enabled)"

// legacySettingKey 是旧库里已经躺着的一行设置。它既当"别把行弄丢"的对照，
// 也因为旧表只有两列而 Go 写侧发三列 —— 它就是那一条错误本身。
const legacySettingKey = "app.ignore.update.version"

// seedLegacyCoreDB 按实测形状建库、塞数据，然后把句柄关掉，
// 让下一步的 `core.NewStorageService` 从"打开一个旧库"这一步开始跑。
func seedLegacyCoreDB(t *testing.T, dir string, urls []string, idOf func(url string, i int) string) {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(dir, "legado.db"))
	if err != nil {
		t.Fatalf("打开旧库失败: %v", err)
	}
	defer func() { _ = db.Close() }()
	if _, err := db.Exec(legacySettingsDDL + ";" + legacyBookSourcesDDL + ";" + legacyBookSourcesGroupIndex); err != nil {
		t.Fatalf("按旧形状建表失败: %v", err)
	}
	if _, err := db.Exec("INSERT INTO settings (key, value) VALUES (?, ?)",
		legacySettingKey, `"9.9.9"`); err != nil {
		t.Fatalf("种旧设置行失败: %v", err)
	}
	for i, u := range urls {
		if _, err := db.Exec(`INSERT INTO book_sources (id, name, url, "group", enabled, json)
			VALUES (?, ?, ?, ?, 1, '{}')`,
			idOf(u, i), "旧名 "+u, u, "旧分组"); err != nil {
			t.Fatalf("种旧书源行失败: %v", err)
		}
	}
}

func openLegacyDB(t *testing.T, dir string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(dir, "legado.db"))
	if err != nil {
		t.Fatalf("打开旧库失败: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func countRows(t *testing.T, q interface {
	QueryRow(query string, args ...interface{}) *sql.Row
}, table string) int {
	t.Helper()
	var n int
	if err := q.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n); err != nil {
		t.Fatalf("数 %s 行失败: %v", table, err)
	}
	return n
}

// 底价：这三段必须在"还没迁"的旧库上红，红了才说明种子是真旧形状、
// 而后面的绿是迁移带来的，不是"随便开了个库"。
func assertLegacyWritesFailBeforeMigration(t *testing.T, dir string, url string) {
	t.Helper()
	db := openLegacyDB(t, dir)

	_, err := db.Exec(`INSERT OR REPLACE INTO settings (key, value, updated_at)
		VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)`, "底价探针", `"1"`)
	if err == nil || !strings.Contains(err.Error(), "no column named updated_at") {
		t.Fatalf("底价不成立：旧 settings 上的三列写入没有报缺列，得到 %v "+
			"—— 种子不是实测那两列的形状，后面的绿说明不了任何事", err)
	}

	_, err = db.Exec(`INSERT INTO book_sources (name, url, "group", enabled, custom_order,
		check_state, respond_time, json)
		VALUES (?, ?, ?, 1, 0, 0, 0, '{}')
		ON CONFLICT(url) DO UPDATE SET name = excluded.name`, "底价探针", url)
	if err == nil || !strings.Contains(err.Error(), "ON CONFLICT clause does not match") {
		t.Fatalf("底价不成立：旧 book_sources 上的 ON CONFLICT(url) 没有报不匹配，得到 %v "+
			"—— 种子不是实测那一份（url 上没有唯一约束），后面的绿说明不了任何事", err)
	}
}

func newLegacyStack(t *testing.T, dir string) (*core.SettingsService, *SourceService, *core.StorageService) {
	t.Helper()
	storage, err := core.NewStorageService(dir)
	if err != nil {
		t.Fatalf("带着旧形状库打开引擎失败（迁移本身把启动打断了）: %v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })
	settings, err := core.NewSettingsService(storage, core.NewEventBus())
	if err != nil {
		t.Fatalf("建设置服务失败: %v", err)
	}
	if err := settings.Start(); err != nil {
		t.Fatalf("设置服务启动失败: %v", err)
	}
	bus := core.NewEventBus()
	sources := NewSourceService(storage, settings, nil, bus)
	if err := sources.Start(); err != nil {
		t.Fatalf("SourceService 启动失败: %v", err)
	}
	t.Cleanup(func() { _ = sources.Stop() })
	return settings, sources, storage
}

// 旧库迁完之后，Go 的三条真写路径（设置、书源存、书源批量导入）必须真的落库，
// 而且旧行一条都不能少。
func TestLegacyCoreSchemaWritesLandAfterMigration(t *testing.T) {
	dir := t.TempDir()
	legacyURLs := []string{"https://legacy-a.test/legado.js", "https://legacy-b.test/legado.js"}
	seedLegacyCoreDB(t, dir, legacyURLs, func(u string, i int) string { return "legacy-id-" + u })
	assertLegacyWritesFailBeforeMigration(t, dir, "https://legacy-a.test/legado.js")

	settings, sources, storage := newLegacyStack(t, dir)

	if n := countRows(t, storage, "settings"); n != 1 {
		t.Errorf("迁完之后 settings 应还是那 1 旧行，得到 %d", n)
	}
	if n := countRows(t, storage, "book_sources"); n != 2 {
		t.Errorf("迁完之后 book_sources 应还是那 2 旧行，得到 %d", n)
	}

	// 1) 设置写入：t114 里 `settings_write` 就在这一条上红。
	if err := settings.Set("theme.dark.enabled", true); err != nil {
		t.Fatalf("迁移后写设置仍然失败: %v", err)
	}
	if got := settings.Get("theme.dark.enabled", ""); got != "true" {
		t.Errorf("写进去的设置读不回来: %q", got)
	}
	if n := countRows(t, storage, "settings"); n != 2 {
		t.Errorf("写一条新设置后应有 1 旧 + 1 新 = 2 行，得到 %d", n)
	}
	var legacyValue string
	if err := storage.QueryRow("SELECT value FROM settings WHERE key = ?", legacySettingKey).
		Scan(&legacyValue); err != nil {
		t.Fatalf("旧设置行读不回来了: %v", err)
	}
	if legacyValue != `"9.9.9"` {
		t.Errorf("旧设置行被改写了: %q（迁移只许加列，不许动值）", legacyValue)
	}

	// 2) 书源写入：更新旧库里躺着的那一条，就地改，不多出一行。
	//    旧表的主键是 id（TEXT），Go 从不读它，所以按 url 覆盖正是这里的语义。
	if err := sources.Save(&models.BookSource{
		BookSourceURL: legacyURLs[0], BookSourceName: "改名后的旧源", Enabled: true,
	}); err != nil {
		t.Fatalf("迁移后存旧书源仍然失败: %v", err)
	}
	if n := countRows(t, storage, "book_sources"); n != 2 {
		t.Errorf("按 url 覆盖不该多出行，现在有 %d 行", n)
	}
	var storedName string
	if err := storage.QueryRow("SELECT name FROM book_sources WHERE url = ?", legacyURLs[0]).
		Scan(&storedName); err != nil {
		t.Fatalf("读回那条旧书源失败: %v", err)
	}
	if storedName != "改名后的旧源" {
		t.Errorf("旧书源没被就地更新: name=%q", storedName)
	}
	if cached, ok := sources.Get(legacyURLs[0]); !ok || cached.BookSourceName != "改名后的旧源" {
		t.Errorf("缓存没跟着库里改: %+v ok=%v", cached, ok)
	}

	// 2b) 带分组的源：nil 那条走的是"落 ''"这一支，非 nil 必须把值传进去，
	//     否则这个补丁就成了"永远写空分组"。
	group := "玄幻"
	withGroup := legacyURLs[1] + "?group=1"
	if err := sources.Save(&models.BookSource{
		BookSourceURL: withGroup, BookSourceName: "带分组的源", BookSourceGroup: &group, Enabled: true,
	}); err != nil {
		t.Fatalf("迁移后存带分组的书源失败: %v", err)
	}
	var storedGroup string
	if err := storage.QueryRow(`SELECT "group" FROM book_sources WHERE url = ?`, withGroup).
		Scan(&storedGroup); err != nil {
		t.Fatalf("读回分组列失败: %v", err)
	}
	if storedGroup != group {
		t.Errorf("分组没落库: %q（应为 %q）", storedGroup, group)
	}

	// 3) 批量导入：这是 t114 里最刺眼的一条 —— 界面回"成功 0 条"，库里 917 行一行没动。
	//    判据不看回包好不好看，看库里到底多了几行。
	text := `[{"bookSourceUrl":"https://new-1.test/legado.js","bookSourceName":"新源一"},` +
		`{"bookSourceUrl":"https://new-2.test/legado.js","bookSourceName":"新源二"}]`
	imported, errs := sources.ImportSourcesJSON(text, ImportModeAppend)
	if len(errs) != 0 {
		t.Fatalf("追加导入报错: %v", errs)
	}
	if imported != 2 {
		t.Errorf("导入回包说落了 %d 条（应为 2）", imported)
	}
	if n := countRows(t, storage, "book_sources"); n != 5 {
		t.Errorf("库里应是 2 旧 + 1 带分组的新存 + 2 导入 = 5 行，实际 %d 行 —— 正是「成功 N 条而一行没落」那个形状", n)
	}
	if n := countRows(t, storage, "settings"); n != 2 {
		t.Errorf("导入不该动 settings，现在有 %d 行", n)
	}

	// 4) 补出来的那根索引必须挂在 url 上，且旧的那根 group 索引不能被弄丢。
	var idxNames []string
	rows, err := storage.Query("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='book_sources' ORDER BY name")
	if err != nil {
		t.Fatalf("查 book_sources 索引失败: %v", err)
	}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			rows.Close()
			t.Fatalf("扫索引名失败: %v", err)
		}
		idxNames = append(idxNames, n)
	}
	rows.Close()
	joined := strings.Join(idxNames, ",")
	if !strings.Contains(joined, "idx_book_sources_url") {
		t.Errorf("没建 idx_book_sources_url，现有索引: %s", joined)
	}
	if !strings.Contains(joined, "idx_book_sources_group") {
		t.Errorf("旧库原有的 idx_book_sources_group 没了，现有索引: %s", joined)
	}
}

// 有重复 url 的旧库：迁移必须停手，把情况喊出来，既不建索引也不删行；
// 写路径继续响亮地红（0 号原则 —— 自动删行是在替用户决定丢哪一条）。
func TestDuplicateURLLegacyLibraryIsNotAutoDeleted(t *testing.T) {
	dir := t.TempDir()
	dupURL := "https://dup.test/legado.js"
	seedLegacyCoreDB(t, dir, []string{dupURL, dupURL, "https://other.test/legado.js"},
		func(u string, i int) string { return fmt.Sprintf("dup-id-%d", i) })

	_, sources, storage := newLegacyStack(t, dir)

	if n := countRows(t, storage, "book_sources"); n != 3 {
		t.Errorf("重复 url 的旧库被删行了：应有 3 行，得到 %d", n)
	}
	var made int
	if err := storage.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_book_sources_url'",
	).Scan(&made); err != nil {
		t.Fatalf("查索引失败: %v", err)
	}
	if made != 0 {
		t.Error("重复 url 上照样建了唯一索引 —— 建的时候 SQLite 会直接报错，连带整个引擎起不来")
	}

	err := sources.Save(&models.BookSource{
		BookSourceURL: "https://third.test/legado.js", BookSourceName: "第三条", Enabled: true,
	})
	if err == nil {
		t.Fatal("重复 url 没被处理时，书源写入应该红在 ON CONFLICT 上；它回成功了 —— 0 号原则被破")
	}
	if !strings.Contains(err.Error(), "ON CONFLICT") {
		t.Errorf("写失败的错误没保住原话: %v", err)
	}
}

// 新库（Go 自己建的形状）必须完全走不到这两条迁移里：
// 不 ALTER、不建多余索引，重开一次也不会长出第二根东西。
func TestFreshGoSchemaTakesNoMigrationPath(t *testing.T) {
	dir := t.TempDir()
	storage, err := core.NewStorageService(dir)
	if err != nil {
		t.Fatalf("建全新库失败: %v", err)
	}
	var urlIdx, made int
	if err := storage.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_book_sources_url'",
	).Scan(&made); err != nil {
		t.Fatalf("查索引失败: %v", err)
	}
	if made != 0 {
		t.Error("新库形状（列上内联 UNIQUE）本就不该再长出一根 idx_book_sources_url")
	}
	if err := storage.QueryRow(
		"SELECT COUNT(*) FROM pragma_table_info('settings') WHERE name='updated_at'",
	).Scan(&urlIdx); err != nil {
		t.Fatalf("查 settings 列失败: %v", err)
	}
	if urlIdx != 1 {
		t.Errorf("新库 settings 应自带 updated_at，得到 %d", urlIdx)
	}
	if err := storage.Close(); err != nil {
		t.Fatalf("关库失败: %v", err)
	}

	// 重开：幂等 —— 迁移认得出"已经是新形状"，一句 ALTER/CREATE 都不发。
	settings, sources, storage2 := newLegacyStack(t, dir)
	if err := settings.Set("engine.reopen.probe", 1); err != nil {
		t.Fatalf("重开后写设置失败: %v", err)
	}
	if err := sources.Save(&models.BookSource{
		BookSourceURL: "https://fresh.test/legado.js", BookSourceName: "新库源", Enabled: true,
	}); err != nil {
		t.Fatalf("新库上按 url 覆盖失败: %v", err)
	}
	if err := sources.Save(&models.BookSource{
		BookSourceURL: "https://fresh.test/legado.js", BookSourceName: "第二次改名", Enabled: true,
	}); err != nil {
		t.Fatalf("新库上第二次按 url 覆盖失败: %v", err)
	}
	if n := countRows(t, storage2, "book_sources"); n != 1 {
		t.Errorf("同 url 存两次应就地更新成 1 行，得到 %d 行", n)
	}
	var extra int
	if err := storage2.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_book_sources_url'",
	).Scan(&extra); err != nil {
		t.Fatalf("查索引失败: %v", err)
	}
	if extra != 0 {
		t.Error("重开一次就补建了 idx_book_sources_url —— hasUniqueIndexOn 没认出列上的内联 UNIQUE")
	}
}
