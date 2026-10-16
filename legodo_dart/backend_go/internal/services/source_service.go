package services

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// SourceService manages book source CRUD operations.
type SourceService struct {
	base     *core.BaseService
	storage  *core.StorageService
	settings *core.SettingsService
	engine   *engine.RuleEngine
	bus      *core.EventBus
	cache    map[string]*models.BookSource // url -> source
}

// NewSourceService creates a new SourceService.
func NewSourceService(storage *core.StorageService, settings *core.SettingsService, ruleEngine *engine.RuleEngine, bus *core.EventBus) *SourceService {
	return &SourceService{
		base:     core.NewBaseService("SourceService", "StorageService", "RuleEngineService"),
		storage:  storage,
		settings: settings,
		engine:   ruleEngine,
		bus:      bus,
		cache:    make(map[string]*models.BookSource),
	}
}

// Start loads all sources from DB into cache.
func (s *SourceService) Start() error {
	s.base.SetOnStart(func() error {
		return s.loadAllSources()
	})
	s.base.SetOnStop(func() error {
		return nil
	})
	return s.base.Start()
}

// Stop releases resources.
func (s *SourceService) Stop() error {
	return s.base.Stop()
}

// loadAllSources reads all sources from DB into the in-memory cache.
func (s *SourceService) loadAllSources() error {
	rows, err := s.storage.Query("SELECT json FROM book_sources")
	if err != nil {
		return fmt.Errorf("load sources: %w", err)
	}
	defer rows.Close()

	count := 0
	for rows.Next() {
		var jsonStr string
		if err := rows.Scan(&jsonStr); err != nil {
			continue
		}
		var src models.BookSource
		if err := json.Unmarshal([]byte(jsonStr), &src); err != nil {
			log.Printf("[SourceService] failed to parse source JSON: %v", err)
			continue
		}
		s.cache[src.BookSourceURL] = &src
		count++
	}
	log.Printf("[SourceService] loaded %d sources into cache", count)
	return rows.Err()
}

// List returns all book sources.
func (s *SourceService) List() []models.BookSource {
	result := make([]models.BookSource, 0, len(s.cache))
	for _, src := range s.cache {
		result = append(result, *src)
	}
	return result
}

// Get retrieves a single source by URL. 查无此源回 (nil, false)——缓存里的
// nil 值与缺席同义，不能拿去解引用（换源写回按 url 找源，找不到是常态）。
func (s *SourceService) Get(url string) (*models.BookSource, bool) {
	src, ok := s.cache[url]
	if !ok || src == nil {
		return nil, false
	}
	copy := *src
	return &copy, true
}

// Save persists a book source to DB and updates the cache.
// 单条动作的形状：落库 + 发一条 SourceChangedEvent。批量入口走 SaveSilent + 收尾一条。
func (s *SourceService) Save(src *models.BookSource) error {
	if err := s.SaveSilent(src); err != nil {
		return err
	}
	s.publishChanged("save", src.BookSourceURL)
	return nil
}

// SaveSilent 与 Save 同一条 UPSERT，但**不发事件**。
// 逐条发 = 界面每件事各重取一次整表（1371 条批量禁用实测折算 ≈10.2 s 的界面占用），
// 所以批量入口用它，整批收尾只发一条汇总。
func (s *SourceService) SaveSilent(src *models.BookSource) error {
	srcJSON, err := json.Marshal(src)
	if err != nil {
		return fmt.Errorf("marshal source: %w", err)
	}

	_, err = s.storage.Execute(sourceUpsertSQL,
		src.BookSourceName, src.BookSourceURL, sourceGroupArg(src),
		boolToInt(src.Enabled), src.CustomOrder, 0, src.RespondTime,
		string(srcJSON),
	)
	if err != nil {
		return fmt.Errorf("save source: %w", err)
	}

	s.cache[src.BookSourceURL] = src
	return nil
}

// publishChanged 是本服务发 SourceChangedEvent 的唯一口。
// SourceID 留空 = 整库级变更（core/events.go：接收方自己重取全部）。
func (s *SourceService) publishChanged(changeType, url string) {
	s.bus.Publish(&core.SourceChangedEvent{
		ChangeType: changeType, SourceID: url, Timestamp: core.NowSeconds(),
	})
}

// Delete removes a book source by URL.
func (s *SourceService) Delete(url string) error {
	if err := s.DeleteSilent(url); err != nil {
		return err
	}
	s.publishChanged("delete", url)
	return nil
}

// DeleteSilent 删一条但**不发事件**（批量删除用它）。
func (s *SourceService) DeleteSilent(url string) error {
	_, err := s.storage.Execute("DELETE FROM book_sources WHERE url = ?", url)
	if err != nil {
		return fmt.Errorf("delete source: %w", err)
	}
	delete(s.cache, url)
	return nil
}

// Enable sets the enabled flag on a source.
func (s *SourceService) Enable(url string, enabled bool) error {
	if err := s.EnableSilent(url, enabled); err != nil {
		return err
	}
	s.publishChanged("enable", url)
	return nil
}

// EnableSilent 改启用状态但**不发事件**（批量启停用它）。
func (s *SourceService) EnableSilent(url string, enabled bool) error {
	_, err := s.storage.Execute("UPDATE book_sources SET enabled = ? WHERE url = ?", boolToInt(enabled), url)
	if err != nil {
		return err
	}
	if src, ok := s.cache[url]; ok {
		src.Enabled = enabled
	}
	return nil
}

// EnableBatch 一次启停多条，形状照 ImportSourcesJSON：**一条事务 + 收尾一条**事件。
//
// 逐条走 EnableSilent 时每发都是一次 autocommit，实测 0.64 ms/行——全选 1371 条
// 就是 879.6 ms 同步压在界面 isolate 上（FFI 没有出口，调用方只能干等）。
// 一条都没改成就不发声；缓存只在提交成功后改，广播必须发生在库里真的变了之后。
func (s *SourceService) EnableBatch(urls []string, enabled bool) int {
	flag := boolToInt(enabled)
	var done []string
	err := s.storage.Transaction(func(tx *sql.Tx) error {
		stmt, err := tx.Prepare("UPDATE book_sources SET enabled = ? WHERE url = ?")
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, url := range urls {
			if _, err := stmt.Exec(flag, url); err != nil {
				continue
			}
			done = append(done, url)
		}
		return nil
	})
	if err != nil {
		log.Printf("[SourceService] batch enable rolled back: %v", err)
		return 0
	}
	for _, url := range done {
		if src, ok := s.cache[url]; ok {
			src.Enabled = enabled
		}
	}
	if len(done) > 0 {
		s.publishChanged("enable", "")
	}
	return len(done)
}

// DeleteBatch 同 EnableBatch 的形状：一条事务删完，收尾一条。
func (s *SourceService) DeleteBatch(urls []string) int {
	var done []string
	err := s.storage.Transaction(func(tx *sql.Tx) error {
		stmt, err := tx.Prepare("DELETE FROM book_sources WHERE url = ?")
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, url := range urls {
			if _, err := stmt.Exec(url); err != nil {
				continue
			}
			done = append(done, url)
		}
		return nil
	})
	if err != nil {
		log.Printf("[SourceService] batch delete rolled back: %v", err)
		return 0
	}
	for _, url := range done {
		delete(s.cache, url)
	}
	if len(done) > 0 {
		s.publishChanged("delete", "")
	}
	return len(done)
}

// SetGroup 改**一条**源的分组。wire 上带的是这一条的 url，
// 原先的形状是把整张表的分组一起刷掉（并且逐条发事件）。
func (s *SourceService) SetGroup(url, group string) error {
	src, ok := s.Get(url)
	if !ok {
		return fmt.Errorf("书源不存在：%s", url)
	}
	src.BookSourceGroup = &group
	return s.Save(src)
}

// ExportAll exports all sources as a JSON array string.
func (s *SourceService) ExportAll() (string, error) {
	items := make([]models.BookSource, 0, len(s.cache))
	for _, src := range s.cache {
		items = append(items, *src)
	}
	data, err := json.MarshalIndent(items, "", "  ")
	return string(data), err
}

// 导入落库的两种模式（Python SourceService.import_sources_json 的 mode 取值）。
const (
	ImportModeAppend  = "append"
	ImportModeReplace = "replace"
)

// importRuleFieldKeys：这六格在 Legado 里既可以是对象，也可以是一段被序列化过的 JSON
// 字符串（Python 模型 `from_dict` 的 RULE_FIELDS 那一圈）。论坛分享源与旧版导出常用
// 字符串形态，不认它等于把整条源判成"非法书源"跳过。
var importRuleFieldKeys = []string{
	"ruleSearch", "ruleExplore", "ruleBookInfo", "ruleToc", "ruleContent", "ruleReview",
}

// ImportSourcesJSON 是 `source_service.py:418 import_sources_json` 的对应物：
// 从 JSON 文本把书源**写进库**，返回（成功条数, 错误列表）。
//
// 三条不是风格的规矩：
//   - 缺 bookSourceUrl 的条目在门口就拒——落库后既匹配不上也删不掉，还会互相撞成
//     同一条空源（Python 同一条注释）。
//   - append 撞 url 跳过：**已有那条赢**，导入文件不会悄悄改掉用户手调过的启用状态。
//   - replace 只在解析出至少一条有效书源时动手，坏文件不能把整张表清空；
//     写库失败回滚后缓存从库里重载，界面上看到的还是动手前那张表。
func (s *SourceService) ImportSourcesJSON(text, mode string) (int, []string) {
	if mode != ImportModeAppend && mode != ImportModeReplace {
		return 0, []string{fmt.Sprintf("未知导入模式：%q", mode)}
	}
	var top interface{}
	if err := json.Unmarshal([]byte(text), &top); err != nil {
		return 0, []string{fmt.Sprintf("JSON 解析失败: %v", err)}
	}
	items, ok := normalizeSourceItems(top)
	if !ok {
		return 0, []string{"书源 JSON 必须是对象或对象数组"}
	}

	existing := make(map[string]bool, len(s.cache))
	for url := range s.cache {
		existing[url] = true
	}
	seen := make(map[string]bool, len(items))
	parsed := make([]*models.BookSource, 0, len(items))
	var errs []string
	for index, item := range items {
		src, err := decodeImportedSource(item)
		if err != nil {
			errs = append(errs, fmt.Sprintf("跳过非法书源: %v", err))
			continue
		}
		sid := strings.TrimSpace(src.BookSourceURL)
		if sid == "" {
			errs = append(errs, fmt.Sprintf("跳过缺少 bookSourceUrl 的条目（第 %d 项）", index+1))
			continue
		}
		if mode == ImportModeAppend && (existing[sid] || seen[sid]) {
			continue
		}
		seen[sid] = true
		src.BookSourceURL = sid
		parsed = append(parsed, src)
	}
	if len(parsed) == 0 {
		return 0, errs
	}

	next := make(map[string]*models.BookSource, len(parsed))
	if mode == ImportModeAppend {
		next = make(map[string]*models.BookSource, len(s.cache)+len(parsed))
		for url, src := range s.cache {
			next[url] = src
		}
	}
	for _, src := range parsed {
		next[src.BookSourceURL] = src
	}

	// 缓存只在事务提交成功后换：事务要么整批落地要么整批不落地，所以失败路径上
	// 旧缓存本来就还是库里那张表——不需要"回滚缓存"这步假动作。
	if err := s.commitImport(parsed, next, mode); err != nil {
		return 0, append(errs, fmt.Sprintf("保存失败，已恢复原有书源：%v", err))
	}

	// 整批一条、整库级：导入 1371 条也只发这一发（界面每发都要重取一次整表）。
	s.publishChanged("imported", "")
	return len(parsed), errs
}

// commitImport 一次事务写库；只有提交成功才把缓存换成新的那份。
// 事务内不碰 bus：广播"书源库变了"必须发生在库里真的变了之后。
func (s *SourceService) commitImport(parsed []*models.BookSource, next map[string]*models.BookSource, mode string) error {
	err := s.storage.Transaction(func(tx *sql.Tx) error {
		if mode == ImportModeReplace {
			if _, err := tx.Exec(`DELETE FROM book_sources`); err != nil {
				return fmt.Errorf("clear sources: %w", err)
			}
		}
		for _, src := range parsed {
			if err := upsertSourceTx(tx, src); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	s.cache = next
	return nil
}

// normalizeSourceItems：顶层对象当一条，数组按多条，其余形状不收（Python 同一条拒绝）。
func normalizeSourceItems(top interface{}) ([]interface{}, bool) {
	switch v := top.(type) {
	case map[string]interface{}:
		return []interface{}{v}, true
	case []interface{}:
		return v, true
	}
	return nil, false
}

// decodeImportedSource 把一个 JSON 项解成 models.BookSource，补上两处"缺键不等于 false"
// 的默认值（enabled / enabledExplore，Python 数据类的默认都是 True），并把规则六格的
// 字符串形态解回对象。
func decodeImportedSource(item interface{}) (*models.BookSource, error) {
	object, ok := item.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("书源条目不是对象")
	}
	normalized := make(map[string]interface{}, len(object))
	for key, value := range object {
		normalized[key] = value
	}
	for _, key := range importRuleFieldKeys {
		raw, present := normalized[key]
		if !present {
			continue
		}
		text, isString := raw.(string)
		if !isString {
			continue
		}
		var decoded interface{}
		if err := json.Unmarshal([]byte(text), &decoded); err != nil {
			return nil, fmt.Errorf("invalid serialized %s", key)
		}
		if _, isObject := decoded.(map[string]interface{}); !isObject {
			return nil, fmt.Errorf("serialized %s is not a JSON object", key)
		}
		normalized[key] = decoded
	}
	for _, key := range []string{"enabled", "enabledExplore"} {
		if _, present := normalized[key]; !present {
			normalized[key] = true
		}
	}
	data, err := json.Marshal(normalized)
	if err != nil {
		return nil, fmt.Errorf("re-encode: %w", err)
	}
	var src models.BookSource
	if err := json.Unmarshal(data, &src); err != nil {
		return nil, err
	}
	return &src, nil
}

// upsertSourceTx 与 Save 写的是同一行同一批列；分开两个入口会让"导入进来的源"和
// "手动存的源"在库里长得不一样（比如导入忘了带 json 列）。
func upsertSourceTx(tx *sql.Tx, src *models.BookSource) error {
	srcJSON, err := json.Marshal(src)
	if err != nil {
		return fmt.Errorf("marshal source %s: %w", src.BookSourceURL, err)
	}
	_, err = tx.Exec(sourceUpsertSQL,
		src.BookSourceName, src.BookSourceURL, sourceGroupArg(src),
		boolToInt(src.Enabled), src.CustomOrder, 0, src.RespondTime, string(srcJSON))
	if err != nil {
		return fmt.Errorf("save source %s: %w", src.BookSourceURL, err)
	}
	return nil
}

// sourceGroupArg 把可空的 bookSourceGroup 落成一条字符串再进 SQL。
// 直接递 *string(nil) 会让驱动写 NULL，而 Python 时代的库里 `"group"` 列是
// NOT NULL DEFAULT “”（DEFAULT 只在省略该列时生效，显式 NULL 照撞），于是每一条
// 没带分组的书源写入都红在 `NOT NULL constraint failed: book_sources.group` 上——
// Go 自己建的那张表这一列可空，所以这个错只在旧库上出现（旧库上它原先被
// `ON CONFLICT clause does not match` 挡在后面，补了唯一索引才露出来）。
// 这一列没有任何读方：缓存整份从 json 列还原（loadAllSources 只 SELECT json），
// 所以 "" 与 NULL 在界面上同形，落 "" 只是让两种库形状都能收下同一句话。
func sourceGroupArg(src *models.BookSource) string {
	if src.BookSourceGroup == nil {
		return ""
	}
	return *src.BookSourceGroup
}

const sourceUpsertSQL = `INSERT INTO book_sources (name, url, "group", enabled, custom_order, check_state, respond_time, json)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		 ON CONFLICT(url) DO UPDATE SET
		   name=excluded.name, "group"=excluded."group", enabled=excluded.enabled,
		   json=excluded.json, custom_order=excluded.custom_order, respond_time=excluded.respond_time`

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
