package services

// 书源导入链的服务面测试：`ImportService`（按 kind 分发的门面）与
// `SourceService.ImportSourcesJSON`（唯一有落库职责的那一层）。
//
// 这一族原来的形状是：门面把 JSON 解析成三个字段的小结构体（`bookSourceUrl`/
// `bookSourceName`/`bookSourceGroup`，规则整段丢掉），塞进一个自己造的内存 map，
// 然后回一句 `imported: N`——**库里一行都没写**。真正会写库的那个方法
// （旧 `ImportMultiple`）没人调用。所以这里的判据全部落在"库里到底多了几行"上，
// 而不是"回包里的数好不好看"。
//
// 判据口径取自 Python 侧那两份现读的实现：
// `app/services/sources/source_service.py:418 import_sources_json` 与
// `app/services/sources/online_import_service.py`（kind 路由表、占位说明文案）。

import (
	"database/sql"
	"encoding/json"
	"strings"
	"sync"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

type importTestStack struct {
	sources  *SourceService
	importer *ImportService
	storage  *core.StorageService
	bus      *core.EventBus

	mu     sync.Mutex
	events []core.AppEvent
}

func newImportTestStack(t *testing.T) *importTestStack {
	t.Helper()
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储失败：%v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })
	settings, err := core.NewSettingsService(storage, core.NewEventBus())
	if err != nil {
		t.Fatalf("建设置服务失败：%v", err)
	}
	bus := core.NewEventBus()
	sources := NewSourceService(storage, settings, nil, bus)
	if err := sources.Start(); err != nil {
		t.Fatalf("SourceService 启动失败：%v", err)
	}
	stack := &importTestStack{sources: sources, storage: storage, bus: bus}
	stack.importer = NewImportService(sources, nil)
	bus.SubscribeAll(func(e core.AppEvent) {
		stack.mu.Lock()
		defer stack.mu.Unlock()
		stack.events = append(stack.events, e)
	})
	t.Cleanup(func() { _ = sources.Stop() })
	return stack
}

// changeEvents 只数 SourceChangedEvent：导入收尾必须**恰好一发**，
// 而 Dart 的书源屏听见这条就整表重取——一发变 N 发就是 N 次全量重载。
func (s *importTestStack) changeEvents() []*core.SourceChangedEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*core.SourceChangedEvent
	for _, event := range s.events {
		if changed, ok := event.(*core.SourceChangedEvent); ok {
			out = append(out, changed)
		}
	}
	return out
}

// rows 直接查库，不看缓存：缓存是服务自己的说法，表里的行才是用户下次打开应用看到的。
func (s *importTestStack) rows(t *testing.T) map[string]string {
	t.Helper()
	rows, err := s.storage.Query("SELECT url, enabled FROM book_sources")
	if err != nil {
		t.Fatalf("查库失败：%v", err)
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var url string
		var enabled int
		if err := rows.Scan(&url, &enabled); err != nil {
			t.Fatalf("读行失败：%v", err)
		}
		out[url] = map[bool]string{true: "启用", false: "停用"}[enabled == 1]
	}
	return out
}

// sourceJSONOf 取库里那格原文，用于验"规则整段还在"。
func (s *importTestStack) sourceJSONOf(t *testing.T, url string) map[string]interface{} {
	t.Helper()
	var raw string
	err := s.storage.QueryRow(`SELECT json FROM book_sources WHERE url=?`, url).Scan(&raw)
	if err == sql.ErrNoRows {
		t.Fatalf("库里没有 %s", url)
	}
	if err != nil {
		t.Fatalf("读 %s 失败：%v", url, err)
	}
	var out map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		t.Fatalf("库里那格不是 JSON 对象：%q", raw)
	}
	return out
}

// seedOne 手工落一条源（不经导入，用来造"库里已经有 X"的现场），并清掉它发出的事件，
// 让后面的"导入收尾只发一发事件"数的是导入自己那几发。
func seedOne(t *testing.T, stack *importTestStack, url, name string, enabled bool) {
	t.Helper()
	src := &models.BookSource{BookSourceURL: url, BookSourceName: name, Enabled: enabled}
	if err := stack.sources.Save(src); err != nil {
		t.Fatalf("预置书源失败：%v", err)
	}
	stack.mu.Lock()
	stack.events = nil
	stack.mu.Unlock()
}

func TestImportWritesRowsAndPublishesOneChange(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://a.test","bookSourceName":"源甲",` +
		`"bookSourceGroup":"组一","ruleSearch":{"bookList":".book"}},` +
		`{"bookSourceUrl":"http://b.test","bookSourceName":"源乙"}]`

	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 2 {
		t.Fatalf("导入条数不是 2：%d（errors=%v）", count, errs)
	}
	if len(errs) != 0 {
		t.Fatalf("干净导入不该带错误：%v", errs)
	}
	rows := stack.rows(t)
	if len(rows) != 2 || rows["http://a.test"] == "" {
		t.Fatalf("库里没多出两行：%v", rows)
	}
	// 规则整段必须落库：旧门面只留 name/url/group，导进来的源从此没有规则可用。
	stored := stack.sourceJSONOf(t, "http://a.test")
	ruleSearch, ok := stored["ruleSearch"].(map[string]interface{})
	if !ok || ruleSearch["bookList"] != ".book" {
		t.Fatalf("ruleSearch 没落库：%v", stored["ruleSearch"])
	}
	if got, _ := stored["bookSourceGroup"].(string); got != "组一" {
		t.Fatalf("分组没落库：%v", stored["bookSourceGroup"])
	}

	changes := stack.changeEvents()
	if len(changes) != 1 {
		t.Fatalf("导入收尾该恰好发一发 SourceChangedEvent，实发 %d：%v", len(changes), changes)
	}
	if changes[0].ChangeType != "imported" || changes[0].SourceID != "" {
		t.Fatalf("收尾事件形状不对：%+v", changes[0])
	}
}

// TestImportAbsentEnabledMeansOn 钉 Python 数据类那条默认值：`enabled` 缺键是 True
// （`book_source.py:224`）。Go 的 json.Unmarshal 缺键留零值 false，于是"导入成功 N 条"
// 之后整批源在库里是停用状态——书源页看着有源、搜不出书。
func TestImportAbsentEnabledMeansOn(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://no-enabled.test","bookSourceName":"没带 enabled 的源"}]`

	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 1 {
		t.Fatalf("该导入 1 条：%d（errors=%v）", count, errs)
	}
	if got := stack.rows(t)["http://no-enabled.test"]; got != "启用" {
		t.Fatalf("缺 enabled 的源落成了停用：%q", got)
	}
	src, ok := stack.sources.Get("http://no-enabled.test")
	if !ok || !src.Enabled {
		t.Fatalf("缓存里那条源是停用：%+v", src)
	}
}

func TestImportRejectsEntriesWithoutURL(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://ok.test","bookSourceName":"能导"},` +
		`{"bookSourceName":"没有地址"},` +
		`{"bookSourceUrl":"   ","bookSourceName":"地址只有空格"}]`

	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 1 {
		t.Fatalf("只有第一条能导：%d（errors=%v）", count, errs)
	}
	if len(errs) != 2 {
		t.Fatalf("两条坏条目各该说一句：%v", errs)
	}
	// 序号是给人去文件里找那条的，必须在；形状（第 N 项）取自 Python 的文案。
	if !strings.Contains(errs[0], "第 2 项") || !strings.Contains(errs[0], "bookSourceUrl") {
		t.Fatalf("第一条缺地址的读数不对：%q", errs[0])
	}
	if !strings.Contains(errs[1], "第 3 项") {
		t.Fatalf("空白地址该按原序号报：%q", errs[1])
	}
	if rows := stack.rows(t); len(rows) != 1 {
		t.Fatalf("坏条目不能落库：%v", rows)
	}
}

// TestImportAppendKeepsExistingRow 钉 append 的"已有那条赢"（Python 在门口 skip）：
// 导一份同名源的文件不该把用户手工停用/改过的那条覆盖回启用。
func TestImportAppendKeepsExistingRow(t *testing.T) {
	stack := newImportTestStack(t)
	seedOne(t, stack, "http://dup.test", "手工改过名的源", false)

	text := `[{"bookSourceUrl":"http://dup.test","bookSourceName":"文件里的名字"}]`
	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 0 {
		t.Fatalf("append 撞 url 该跳过，却报了 %d 条（errors=%v）", count, errs)
	}
	if len(errs) != 0 {
		t.Fatalf("跳过重复不是错误，不该说话：%v", errs)
	}
	if got := stack.rows(t)["http://dup.test"]; got != "停用" {
		t.Fatalf("重复导入把用户停用那条改回了启用：%q", got)
	}
	src, _ := stack.sources.Get("http://dup.test")
	if src.BookSourceName != "手工改过名的源" {
		t.Fatalf("库里那条被覆盖成了：%q", src.BookSourceName)
	}
	if changes := stack.changeEvents(); len(changes) != 0 {
		t.Fatalf("一行没动却广播了书源变更：%+v", changes)
	}
}

// TestImportReplaceSemantics 两条都要钉：坏文件不能清空表，好文件才整表换。
func TestImportReplaceSemantics(t *testing.T) {
	stack := newImportTestStack(t)
	seedOne(t, stack, "http://old.test", "旧源", true)

	// 一：一条有效源都没解析出来 → 不动表。
	count, errs := stack.sources.ImportSourcesJSON(
		`[{"bookSourceName":"没有地址"}]`, ImportModeReplace)
	if count != 0 || len(errs) == 0 {
		t.Fatalf("坏文件该报 0 条并说话：%d / %v", count, errs)
	}
	if rows := stack.rows(t); len(rows) != 1 || rows["http://old.test"] == "" {
		t.Fatalf("一个坏文件把整张表清空了：%v", rows)
	}

	// 二：解析出至少一条 → 旧行退场，新行留下（库里恰好只剩新那批）。
	count, errs = stack.sources.ImportSourcesJSON(
		`[{"bookSourceUrl":"http://new.test","bookSourceName":"新源"}]`, ImportModeReplace)
	if count != 1 {
		t.Fatalf("替换该导 1 条：%d（errors=%v）", count, errs)
	}
	rows := stack.rows(t)
	if len(rows) != 1 || rows["http://new.test"] == "" {
		t.Fatalf("替换后库里该只剩新源：%v", rows)
	}
	if _, ok := stack.sources.Get("http://old.test"); ok {
		t.Fatalf("缓存里旧源还在：替换只写了库没换缓存")
	}
	src, _ := stack.sources.Get("http://new.test")
	if src == nil || src.BookSourceName != "新源" {
		t.Fatalf("新源没进缓存：%+v", src)
	}
}

// TestImportAcceptsRuleEncodedAsString 钉 Legado 的另一种形状：规则格是一段被序列化过
// 的 JSON 字符串（`book_source.py:232` 注释：旧版与分享源常用）。按对象解会把整条判死。
func TestImportAcceptsRuleEncodedAsString(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://str-rule.test","bookSourceName":"字符串规则源",` +
		`"ruleSearch":"{\"bookList\":\".book\",\"name\":\".name//text()\"}"}]`

	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 1 {
		t.Fatalf("字符串形态的规则格该被接受：%d（errors=%v）", count, errs)
	}
	src, ok := stack.sources.Get("http://str-rule.test")
	if !ok || src.RuleSearch == nil || src.RuleSearch.BookList == nil ||
		*src.RuleSearch.BookList != ".book" {
		t.Fatalf("规则解出来了却是空的：%+v", src)
	}
	stored := stack.sourceJSONOf(t, "http://str-rule.test")
	if _, isObject := stored["ruleSearch"].(map[string]interface{}); !isObject {
		t.Fatalf("库里那格还是字符串：%v", stored["ruleSearch"])
	}
}

func TestImportBrokenRuleStringIsReported(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://bad-rule.test","ruleSearch":"不是 JSON"}]`

	count, errs := stack.sources.ImportSourcesJSON(text, ImportModeAppend)
	if count != 0 || len(errs) != 1 {
		t.Fatalf("坏规则串该整条拒掉并报一句：%d / %v", count, errs)
	}
	if !strings.Contains(errs[0], "ruleSearch") {
		t.Fatalf("错误里没说是哪一格坏了：%q", errs[0])
	}
}

func TestImportUnknownModeAndBadShapes(t *testing.T) {
	stack := newImportTestStack(t)

	count, errs := stack.sources.ImportSourcesJSON(`[{"bookSourceUrl":"http://m.test"}]`, "merge")
	if count != 0 || len(errs) != 1 || !strings.Contains(errs[0], "未知导入模式") {
		t.Fatalf("表外模式该被拒：%d / %v", count, errs)
	}
	if rows := stack.rows(t); len(rows) != 0 {
		t.Fatalf("被拒的导入却写了库：%v", rows)
	}

	if _, errs := stack.sources.ImportSourcesJSON(`{这不是 JSON`, ImportModeAppend); len(errs) == 0 ||
		!strings.Contains(errs[0], "JSON 解析失败") {
		t.Fatalf("语法错没报成语法错：%v", errs)
	}
	if _, errs := stack.sources.ImportSourcesJSON(`"只是一句字符串"`, ImportModeAppend); len(errs) == 0 ||
		!strings.Contains(errs[0], "必须是对象或对象数组") {
		t.Fatalf("顶层形状不对没说话：%v", errs)
	}
}

// TestImportFailedTransactionKeepsCache 写库失败（表被拿掉）时：报一句"保存失败"，
// 缓存还是动手前那张表——不许出现"库里没动、界面上已经多了两条"的假象。
func TestImportFailedTransactionKeepsCache(t *testing.T) {
	stack := newImportTestStack(t)
	seedOne(t, stack, "http://keep.test", "原来的源", true)

	if _, err := stack.storage.Execute(`DROP TABLE book_sources`); err != nil {
		t.Fatalf("拆表失败（前置没铺好）：%v", err)
	}

	count, errs := stack.sources.ImportSourcesJSON(
		`[{"bookSourceUrl":"http://gone.test","bookSourceName":"写不进的源"}]`, ImportModeAppend)
	if count != 0 {
		t.Fatalf("写库失败却报成功：%d", count)
	}
	if len(errs) == 0 || !strings.Contains(errs[len(errs)-1], "保存失败") {
		t.Fatalf("最后一句该是保存失败：%v", errs)
	}
	src, ok := stack.sources.Get("http://keep.test")
	if !ok || src.BookSourceName != "原来的源" {
		t.Fatalf("缓存没停在动手前那张表：%+v", src)
	}
	if _, ok := stack.sources.Get("http://gone.test"); ok {
		t.Fatalf("写失败的条目进了缓存：界面上它会显示成一条不存在的源")
	}
	if changes := stack.changeEvents(); len(changes) != 0 {
		t.Fatalf("库里没变动却广播了书源变更：%+v", changes)
	}
}

// TestImportTopLevelSingleObject 顶层单对象也收（Python：dict 当一条）。
func TestImportTopLevelSingleObject(t *testing.T) {
	stack := newImportTestStack(t)
	count, errs := stack.sources.ImportSourcesJSON(
		`{"bookSourceUrl":"http://one.test","bookSourceName":"单对象"}`, ImportModeAppend)
	if count != 1 {
		t.Fatalf("顶层单对象该按一条导：%d（errors=%v）", count, errs)
	}
	if rows := stack.rows(t); len(rows) != 1 || rows["http://one.test"] == "" {
		t.Fatalf("单对象没落库：%v", rows)
	}
}

// --- 门面（按 kind 分发）---

func TestImportFacadeWritesThroughToLibrary(t *testing.T) {
	stack := newImportTestStack(t)
	text := `[{"bookSourceUrl":"http://facade.test","bookSourceName":"门面导的源"}]`

	result := stack.importer.ImportText(text, ImportKindBookSource, ImportModeAppend)
	if result.Count != 1 || result.Placeholder || len(result.Errors) != 0 {
		t.Fatalf("门面回包不对：%+v", result)
	}
	if result.Kind != ImportKindBookSource {
		t.Fatalf("kind 该原样带回：%q", result.Kind)
	}
	if rows := stack.rows(t); rows["http://facade.test"] == "" {
		t.Fatalf("门面报了 1 条，库里却没有：%v", rows)
	}
}

// TestImportFacadePendingKindsArePlaceholder 钉那六个"今天没有落点"的类型：
// count=0 + placeholder=true + 一句去哪一页加的说明。界面把 placeholder 丢了就会报成
// "导入了 0 条"，用户以为文件坏了，其实是这类东西本来就没有存储落点。
func TestImportFacadePendingKindsArePlaceholder(t *testing.T) {
	stack := newImportTestStack(t)
	wantTexts := map[string]string{
		"txt_rules":     "TXT 目录规则目前没有独立的存储落点，暂不支持导入",
		"replace_rules": "替换规则请在「配置 → 替换规则」页里添加，导入通道未接",
		"dict_rules":    "词典规则请在「配置 → 词典」页里添加，导入通道未接",
		"rss":           "RSS 源请在「配置 → 更多工具」的 RSS 页里添加，导入通道未接",
		"tts":           "TTS 朗读在阅读器内设置，导入通道未接",
		"theme":         "主题在「配置 → 外观」页里选择，导入通道未接",
	}
	if got := len(wantTexts); got != len(pendingKindText) {
		t.Fatalf("待接类型表与文案表条数不一致：%d vs %d（新增一格要同时补说明）",
			got, len(pendingKindText))
	}
	for kind, want := range wantTexts {
		result := stack.importer.ImportText(`[{"bookSourceUrl":"http://x.test"}]`, kind, ImportModeAppend)
		if !result.Placeholder || result.Count != 0 || len(result.Errors) != 0 {
			t.Fatalf("%s 该是占位：%+v", kind, result)
		}
		if len(result.Warnings) != 1 || result.Warnings[0] != want {
			t.Fatalf("%s 的说明文案漂移了：%q（该是 %q）", kind, result.Warnings, want)
		}
	}
	if rows := stack.rows(t); len(rows) != 0 {
		t.Fatalf("占位类型不该写库：%v", rows)
	}
}

// TestImportFacadeUnknownKindIsError 表里查不到的 kind 不是"占位"而是拼错了：
// 报 error，且 placeholder=false——否则界面会把它显示成"这类东西没有落点"，
// 而真相是"你说的这一类我听不懂"。
func TestImportFacadeUnknownKindIsError(t *testing.T) {
	stack := newImportTestStack(t)
	result := stack.importer.ImportText(`[]`, "book_sources", ImportModeAppend)
	if result.Placeholder {
		t.Fatalf("未知类型被当成了占位：%+v", result)
	}
	if len(result.Errors) != 1 || !strings.Contains(result.Errors[0], "未知资源类型") {
		t.Fatalf("未知类型该报一句：%v", result.Errors)
	}
}

func TestImportFacadeParseErrorsComeFromFacadeWording(t *testing.T) {
	stack := newImportTestStack(t)
	cases := []struct {
		text  string
		want  string
		label string
	}{
		{`{这不是 JSON`, "JSON 解析失败", "语法错"},
		{`"字符串"`, "书源 JSON 顶层应为数组或对象", "顶层形状"},
		{`[1,2,3]`, "书源 JSON 中没有有效对象", "数组里没有对象"},
	}
	for _, c := range cases {
		result := stack.importer.ImportText(c.text, ImportKindBookSource, ImportModeAppend)
		if result.Count != 0 || len(result.Errors) != 1 {
			t.Fatalf("%s：%+v", c.label, result)
		}
		if !strings.Contains(result.Errors[0], c.want) {
			t.Fatalf("%s 的读数不对：%q（该含 %q）", c.label, result.Errors[0], c.want)
		}
	}
}

// TestImportTextStripsBOM Windows 上存的 JSON 常带 UTF-8 BOM：不剥就是"这个文件不能
// 导入：JSON 解析失败"，而文件其实完全正常。
func TestImportTextStripsBOM(t *testing.T) {
	stack := newImportTestStack(t)
	text := "\xEF\xBB\xBF" + `[{"bookSourceUrl":"http://bom.test","bookSourceName":"带 BOM 的源"}]`

	result := stack.importer.ImportText(text, ImportKindBookSource, ImportModeAppend)
	if result.Count != 1 || len(result.Errors) != 0 {
		t.Fatalf("带 BOM 的文件没导进去：%+v", result)
	}
	if rows := stack.rows(t); rows["http://bom.test"] == "" {
		t.Fatalf("报了 1 条但库里没有：%v", rows)
	}
}

// TestImportFromURLRecordsFetchFailure 拉取失败记成一条 error 后**不再分发**：
// 不许把"没拿到东西"报成"导入完成 0 条"，更不许顺手把空串当 JSON 去解析。
func TestImportFromURLRecordsFetchFailure(t *testing.T) {
	stack := newImportTestStack(t)
	stack.importer.fetchText = func(url string) (string, error) {
		return "", &urlFetchError{url: url}
	}
	result := stack.importer.ImportFromURL("http://127.0.0.1:1/nope.json", ImportKindBookSource, ImportModeAppend)
	if len(result.Errors) != 1 || !strings.HasPrefix(result.Errors[0], "拉取失败：") {
		t.Fatalf("拉取失败没按那句报：%v", result.Errors)
	}
	if result.Count != 0 || !strings.Contains(result.Errors[0], "127.0.0.1") {
		t.Fatalf("错误里该带上是哪个地址： %+v", result)
	}
	if rows := stack.rows(t); len(rows) != 0 {
		t.Fatalf("没拉到东西却写了库：%v", rows)
	}
}

// TestImportFromURLDispatchesFetchedText 拉到的文本走的是同一条落库路（不是第二份实现）。
func TestImportFromURLDispatchesFetchedText(t *testing.T) {
	stack := newImportTestStack(t)
	stack.importer.fetchText = func(url string) (string, error) {
		return `[{"bookSourceUrl":"http://from-url.test","bookSourceName":"在线导的源"}]`, nil
	}
	result := stack.importer.ImportFromURL("http://ok.test/a.json", ImportKindBookSource, ImportModeAppend)
	if result.Count != 1 || len(result.Errors) != 0 {
		t.Fatalf("在线导入没落库：%+v", result)
	}
	if rows := stack.rows(t); rows["http://from-url.test"] != "启用" {
		t.Fatalf("在线导的源在库里形状不对：%v", rows)
	}
	if result.Kind != ImportKindBookSource {
		t.Fatalf("kind 丢了：%q", result.Kind)
	}
}

type urlFetchError struct{ url string }

func (e *urlFetchError) Error() string { return "dial " + e.url + ": connection refused" }

// TestImportFromURLUsesRealFetcherOnLoopback 默认拉取器（没注入替身）也要能跑：
// 指向一个关着的回环端口，要听到"拉取失败"，不是"导入 0 条"。
func TestImportFromURLUsesRealFetcherOnLoopback(t *testing.T) {
	stack := newImportTestStack(t)
	result := stack.importer.ImportFromURL("http://127.0.0.1:1/x.json", ImportKindBookSource, ImportModeAppend)
	if len(result.Errors) != 1 || !strings.HasPrefix(result.Errors[0], "拉取失败：") {
		t.Fatalf("默认拉取器没报错：%+v", result)
	}
}
