package services

import (
	"context"
	"database/sql"
	"fmt"
	"html"
	"log"
	"strings"
	"time"

	"github.com/antchfx/xmlquery"
	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
)

// RSS 服务（port 自 app/services/content/rss_service.py，契约 §3）。
//
// 两条不可动的语义（wire 面由 golden 钉死）：
//
//  1. **源 id 就是 feed 地址**（TEXT 主键，add/remove/refresh 都吃它）；文章 id 是
//     自增整数。golden `rss.sources.json` 的 id 就是 url，Dart `RssSourceRow.id`
//     按字符串收。
//  2. **库里存 HTML 原文，`rss.article` 才给去标签的 `text`**：`plainText` 是
//     全库唯一一把去标签的尺（对齐 Python `plain_text` / bs4.get_text("\n")），
//     列表那一发（9 键）一根正文都不带。
//
// 事件面：单源刷新完成发 RssRefreshedEvent{source_id, new_count}（new_count = 按
// (source_id,url) 去重后的**真新增**数）；全量刷新逐源发进度帧，收尾那一帧是唯一
// 带 failures 的（中途每帧的失败数"截至此刻"画出来就是锯齿，见 Python 原文）。
type RSSService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
	httpC   *httpclient.ClientSession
}

// NewRSSService creates a new RSS service.
func NewRSSService(storage *core.StorageService, bus *core.EventBus, hc *httpclient.ClientSession) *RSSService {
	return &RSSService{
		base:    core.NewBaseService("RSSService", "StorageService"),
		storage: storage,
		bus:     bus,
		httpC:   hc,
	}
}

func (s *RSSService) Start() error { return s.base.Start() }
func (s *RSSService) Stop() error  { return s.base.Stop() }

func (s *RSSService) publish(event core.AppEvent) {
	if s.bus != nil {
		s.bus.Publish(event)
	}
}

func (s *RSSService) publishRefreshProgress(done, total int, current string, failures int, refreshID int64) {
	s.publish(&core.RssRefreshProgressEvent{
		Done: done, Total: total, Current: current,
		Failures: failures, RefreshID: refreshID, Timestamp: core.NowSeconds(),
	})
}

// ---------------------------------------------------------------------------
// 读（源行 / 文章行 / 正文）
// ---------------------------------------------------------------------------

// SourceRows 订阅列表那一屏的行：源字段 + 篇数/未读两格角标（服务端一条 GROUP BY
// join 好，客户端不自己逐源数）。对齐 Python `source_rows`。
func (s *RSSService) SourceRows() []map[string]interface{} {
	rows := make([]map[string]interface{}, 0)
	if s.storage == nil {
		return rows
	}
	counts := map[string][2]int{}
	countRows, err := s.storage.Query(
		`SELECT source_id, COUNT(*), SUM(CASE WHEN read_flag=0 THEN 1 ELSE 0 END)
		 FROM rss_articles GROUP BY source_id`)
	if err == nil && countRows != nil {
		for countRows.Next() {
			var sourceID string
			var total, unread int
			if scanErr := countRows.Scan(&sourceID, &total, &unread); scanErr == nil {
				counts[sourceID] = [2]int{total, unread}
			}
		}
		countRows.Close()
	}

	srcRows, err := s.storage.Query(
		`SELECT id, name, url, enabled, source_group, custom_order, type, last_update_time
		 FROM rss_sources ORDER BY custom_order ASC, name ASC`)
	if err != nil || srcRows == nil {
		return rows
	}
	defer srcRows.Close()
	for srcRows.Next() {
		var id, name, url, group string
		var enabled, order, typeVal int
		var lastUpdate float64
		if scanErr := srcRows.Scan(&id, &name, &url, &enabled, &group, &order, &typeVal, &lastUpdate); scanErr != nil {
			continue
		}
		stat := counts[id]
		rows = append(rows, map[string]interface{}{
			"id": id, "name": name, "url": url,
			"enabled": enabled == 1, "group": group,
			"custom_order": order, "type": typeVal,
			"last_update_time": lastUpdate,
			"total":            stat[0], "unread": stat[1],
		})
	}
	return rows
}

// ArticleRows 某源的文章行（9 键，**不带正文**）。[unreadOnly] 折的就是 read_flag。
func (s *RSSService) ArticleRows(sourceID string, unreadOnly bool) []map[string]interface{} {
	rows := make([]map[string]interface{}, 0)
	if s.storage == nil || sourceID == "" {
		return rows
	}
	sqlText := `SELECT id, source_id, title, url, author, pub_date, image, read_flag, star_flag
		FROM rss_articles WHERE source_id = ?`
	if unreadOnly {
		sqlText += ` AND read_flag = 0`
	}
	sqlText += ` ORDER BY pub_date DESC, id DESC`
	result, err := s.storage.Query(sqlText, sourceID)
	if err != nil || result == nil {
		return rows
	}
	defer result.Close()
	for result.Next() {
		var id int64
		var srcID, title, url, author, pubDate, image string
		var read, starred int
		if scanErr := result.Scan(&id, &srcID, &title, &url, &author, &pubDate, &image, &read, &starred); scanErr != nil {
			continue
		}
		rows = append(rows, map[string]interface{}{
			"id": id, "source_id": srcID, "title": title, "url": url,
			"author": author, "pub_date": pubDate, "image": image,
			"read": read == 1, "starred": starred == 1,
		})
	}
	return rows
}

// ArticleDetail 打开一篇文章：行 + **已去标签**的正文（text）；没这篇回 nil
// （wire 上 data=null，Dart `openRssArticle` 把 null 与空正文分开读）。
func (s *RSSService) ArticleDetail(articleID int64) map[string]interface{} {
	if s.storage == nil {
		return nil
	}
	var id int64
	var srcID, title, url, author, pubDate, content, image string
	var read, starred int
	err := s.storage.QueryRow(
		`SELECT id, source_id, title, url, author, pub_date, content, image, read_flag, star_flag
		 FROM rss_articles WHERE id = ?`, articleID).
		Scan(&id, &srcID, &title, &url, &author, &pubDate, &content, &image, &read, &starred)
	if err == sql.ErrNoRows {
		return nil
	}
	if err != nil {
		return nil
	}
	return map[string]interface{}{
		"id": id, "source_id": srcID, "title": title, "url": url,
		"author": author, "pub_date": pubDate, "text": plainText(content),
		"image": image, "read": read == 1, "starred": starred == 1,
	}
}

// ---------------------------------------------------------------------------
// 写（源 CRUD）
// ---------------------------------------------------------------------------

// AddSource 新增订阅源，返回源 id（= 规范化后的 feed 地址）。同 url 重复添加是
// 更新（ON CONFLICT），不产生重复行。
func (s *RSSService) AddSource(name, url, sourceGroup string) (string, error) {
	url = strings.TrimSpace(url)
	if url == "" {
		return "", fmt.Errorf("RSS 源 url 不能为空")
	}
	name = strings.TrimSpace(name)
	if name == "" {
		name = url
	}
	if s.storage == nil {
		return url, nil
	}
	_, err := s.storage.Execute(
		`INSERT INTO rss_sources(id, name, url, source_group) VALUES(?,?,?,?)
		 ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_group=excluded.source_group`,
		url, name, url, strings.TrimSpace(sourceGroup))
	if err != nil {
		return "", err
	}
	return url, nil
}

// RemoveSource 删除订阅源及其全部文章（含已读/收藏标记），回是否真删到行。
func (s *RSSService) RemoveSource(sourceID string) (bool, error) {
	if s.storage == nil || sourceID == "" {
		return false, nil
	}
	if _, err := s.storage.Execute(`DELETE FROM rss_articles WHERE source_id = ?`, sourceID); err != nil {
		return false, err
	}
	result, err := s.storage.Execute(`DELETE FROM rss_sources WHERE id = ?`, sourceID)
	if err != nil {
		return false, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return false, nil
	}
	return changed > 0, nil
}

// ---------------------------------------------------------------------------
// 刷新
// ---------------------------------------------------------------------------

// Refresh 抓取并刷新单个源，返回新增文章数；完成发 RssRefreshedEvent。
//
// 源不存在回 (0, nil)——列表刚被另一侧删掉时不该炸界面；抓取失败回**错误**（这一发
// 对齐 schema：错误信封里带异常名，「0 新增」与「没抓着」是两件事）。
func (s *RSSService) Refresh(ctx context.Context, sourceID string) (int, error) {
	sourceID = strings.TrimSpace(sourceID)
	if sourceID == "" || s.storage == nil || s.httpC == nil {
		return 0, nil
	}
	var url string
	err := s.storage.QueryRow(`SELECT url FROM rss_sources WHERE id = ?`, sourceID).Scan(&url)
	if err == sql.ErrNoRows {
		log.Printf("[RSS] refresh：源不存在 %s", sourceID)
		return 0, nil
	}
	if err != nil {
		return 0, err
	}

	resp, err := s.httpC.Get(ctx, url, nil)
	if err != nil {
		return 0, fmt.Errorf("feed 抓取失败: %w", err)
	}
	articles := parseFeed(resp.BodyString())

	newCount, err := s.upsertArticles(sourceID, articles)
	if err != nil {
		return 0, err
	}
	_, _ = s.storage.Execute(`UPDATE rss_sources SET last_update_time = ? WHERE id = ?`,
		float64(time.Now().UnixNano())/1e9, sourceID)
	s.publish(&core.RssRefreshedEvent{
		SourceID: sourceID, NewCount: newCount, Timestamp: core.NowSeconds(),
	})
	return newCount, nil
}

// RefreshAll 刷新全部启用源，返回新增文章总数；逐源发进度帧与 RssRefreshedEvent。
//
// 串行逐源抓（schema 明写："串行逐源抓 feed，所以一轮的长短取决于源数与网络"）：
// 进度帧的 `current` 按 Python 的口径是"**接下来**要刷的那一条"，`done` 是"已经
// 动手的条数"——中途帧只在开抓之前发得出来这个形状（并发完成帧会把 current 说成
// 刚刷完的那条，界面状态句 '正在刷新 done+1/total：X' 就指错行）。`refresh_id`
// 由客户端生成、每帧原样回带。
func (s *RSSService) RefreshAll(ctx context.Context, refreshID int64) int {
	type srcRow struct{ id, name string }
	sources := make([]srcRow, 0)
	if s.storage != nil {
		rows, err := s.storage.Query(
			`SELECT id, name FROM rss_sources WHERE enabled = 1
			 ORDER BY custom_order ASC, name ASC`)
		if err == nil && rows != nil {
			for rows.Next() {
				var item srcRow
				if scanErr := rows.Scan(&item.id, &item.name); scanErr == nil {
					sources = append(sources, item)
				}
			}
			rows.Close()
		}
	}
	total := len(sources)
	// 分母在**开头**就定下（本轮计划源数），中途不许变。
	s.publishRefreshProgress(0, total, "", 0, refreshID)

	totalNew := 0
	failures := 0
	for index, src := range sources {
		current := src.name
		if current == "" {
			current = src.id
		}
		s.publishRefreshProgress(index, total, current, 0, refreshID)
		count, err := s.Refresh(ctx, src.id)
		if err != nil {
			// 单源失败不拖垮其余源，但**要有读数**：收尾帧带 failures。
			failures++
			log.Printf("[RSS] refresh_all：源 %s 刷新失败，继续其余: %v", src.id, err)
			continue
		}
		totalNew += count
	}
	// 收尾那一帧是唯一带 failures 的。
	s.publishRefreshProgress(total, total, "", failures, refreshID)
	return totalNew
}

// ---------------------------------------------------------------------------
// 文章写回
// ---------------------------------------------------------------------------

// MarkRead 标记文章已读，回是否真改到行。
func (s *RSSService) MarkRead(articleID int64) (bool, error) {
	if s.storage == nil {
		return false, nil
	}
	result, err := s.storage.Execute(
		`UPDATE rss_articles SET read_flag = 1, updated_at = ? WHERE id = ?`,
		float64(time.Now().UnixNano())/1e9, articleID)
	if err != nil {
		return false, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return false, nil
	}
	return changed > 0, nil
}

// Star 设置文章收藏（**设定值**不是翻转），回是否真改到行。
func (s *RSSService) Star(articleID int64, starred bool) (bool, error) {
	if s.storage == nil {
		return false, nil
	}
	flag := 0
	if starred {
		flag = 1
	}
	result, err := s.storage.Execute(
		`UPDATE rss_articles SET star_flag = ?, updated_at = ? WHERE id = ?`,
		flag, float64(time.Now().UnixNano())/1e9, articleID)
	if err != nil {
		return false, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return false, nil
	}
	return changed > 0, nil
}

// ---------------------------------------------------------------------------
// 落库（按 (source_id,url) 去重，保留已读/收藏状态）
// ---------------------------------------------------------------------------

func (s *RSSService) upsertArticles(sourceID string, articles []feedArticle) (int, error) {
	if len(articles) == 0 {
		return 0, nil
	}
	now := float64(time.Now().UnixNano()) / 1e9
	newCount := 0
	err := s.storage.Transaction(func(tx *sql.Tx) error {
		existing := map[string]bool{}
		rows, err := tx.Query(`SELECT url FROM rss_articles WHERE source_id = ?`, sourceID)
		if err != nil {
			return err
		}
		for rows.Next() {
			var u string
			if scanErr := rows.Scan(&u); scanErr != nil {
				rows.Close()
				return scanErr
			}
			existing[u] = true
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}

		for _, art := range articles {
			if existing[art.URL] {
				// 已存在：刷新标题/正文/日期/作者，但**不动** read_flag/star_flag
				// （用户已读/收藏状态不得被刷新覆盖）。
				if _, err := tx.Exec(
					`UPDATE rss_articles SET title=?, content=?, pub_date=?, author=?, updated_at=?
					 WHERE source_id=? AND url=?`,
					art.Title, art.Content, art.PubDate, art.Author, now, sourceID, art.URL); err != nil {
					return err
				}
				continue
			}
			if _, err := tx.Exec(
				`INSERT INTO rss_articles
				 (source_id, title, url, author, pub_date, content, image, updated_at)
				 VALUES (?,?,?,?,?,?,?,?)`,
				sourceID, art.Title, art.URL, art.Author, art.PubDate,
				art.Content, art.Image, now); err != nil {
				return err
			}
			existing[art.URL] = true
			newCount++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return newCount, nil
}

// ---------------------------------------------------------------------------
// 正文去标签（全库唯一一把尺，对齐 Python plain_text / bs4.get_text("\n")）
// ---------------------------------------------------------------------------

// plainText feed 正文（HTML）→ 可读纯文本：脚本/样式整段丢弃，标签与标签之间的
// 每一段文字去首尾空白、解实体，非空的用换行接起来。库里存的仍是原文。
func plainText(raw string) string {
	text := strings.TrimSpace(raw)
	if !strings.Contains(text, "<") {
		return text
	}
	var builder strings.Builder
	skipDepth := 0 // script/style 内的文字不落
	appendChunk := func(chunk string) {
		trimmed := strings.TrimSpace(html.UnescapeString(chunk))
		if trimmed == "" {
			return
		}
		if builder.Len() > 0 {
			builder.WriteByte('\n')
		}
		builder.WriteString(trimmed)
	}

	for index := 0; index < len(text); {
		lt := strings.IndexByte(text[index:], '<')
		if lt < 0 {
			if skipDepth == 0 {
				appendChunk(text[index:])
			}
			break
		}
		if skipDepth == 0 {
			appendChunk(text[index : index+lt])
		}
		index += lt
		if strings.HasPrefix(text[index:], "<!--") {
			end := strings.Index(text[index:], "-->")
			if end < 0 {
				break
			}
			index += end + 3
			continue
		}
		gt := strings.IndexByte(text[index:], '>')
		if gt < 0 {
			break
		}
		tag := strings.TrimSpace(text[index+1 : index+gt])
		index += gt + 1

		name := strings.ToLower(tag)
		closing := strings.HasPrefix(name, "/")
		name = strings.TrimPrefix(name, "/")
		if cut := strings.IndexAny(name, " \t\r\n/"); cut >= 0 {
			name = name[:cut]
		}
		if name == "script" || name == "style" {
			if closing {
				if skipDepth > 0 {
					skipDepth--
				}
			} else {
				skipDepth++
			}
		}
	}
	return builder.String()
}

// ---------------------------------------------------------------------------
// 标准 feed 解析（RSS 2.0 / Atom；对齐 feedparser 的字段口径）
// ---------------------------------------------------------------------------

type feedArticle struct {
	Title   string
	URL     string
	Author  string
	PubDate string
	Content string
	Image   string
}

// parseFeed 把 feed 原文解析为文章列表。解析失败按 0 条处理（feedparser 对坏
// feed 宽容：bozo=1、entries=[]），抓取失败才上抛——两者在 wire 上是不同的事。
func parseFeed(body string) []feedArticle {
	doc, err := xmlquery.Parse(strings.NewReader(body))
	if err != nil {
		log.Printf("[RSS] feed 解析失败（按 0 条处理）: %v", err)
		return nil
	}
	items := xmlquery.Find(doc, "//item")
	if len(items) == 0 {
		items = xmlquery.Find(doc, "//entry")
	}
	articles := make([]feedArticle, 0, len(items))
	for _, item := range items {
		link := firstLink(item)
		if link == "" {
			continue // 无链接的条目无法作为文章唯一键，跳过
		}
		title := nodeInnerText(xmlquery.FindOne(item, "title"))
		if title == "" {
			title = link
		}
		articles = append(articles, feedArticle{
			Title:   title,
			URL:     link,
			Author:  firstText(item, "author", "creator"),
			PubDate: firstText(item, "pubDate", "published", "updated"),
			Content: firstText(item, "summary", "description"),
			Image:   firstThumbnailURL(item),
		})
	}
	return articles
}

// firstLink 取条目链接：RSS 的 <link>文本</link> 与 Atom 的
// <link rel="..." href="..."/> 两种形状都认；rel 非 alternate 的（如 self）跳过。
func firstLink(item *xmlquery.Node) string {
	for node := item.FirstChild; node != nil; node = node.NextSibling {
		if node.Type != xmlquery.ElementNode || node.Data != "link" {
			continue
		}
		rel := attrValue(node, "rel")
		if rel != "" && rel != "alternate" {
			continue
		}
		if href := attrValue(node, "href"); href != "" {
			return href
		}
		if text := strings.TrimSpace(node.InnerText()); text != "" {
			return text
		}
	}
	return ""
}

func firstText(item *xmlquery.Node, names ...string) string {
	for _, name := range names {
		if text := nodeInnerText(xmlquery.FindOne(item, name)); text != "" {
			return text
		}
	}
	return ""
}

// firstThumbnailURL 取媒体封面（feedparser 的 media_thumbnail：<media:thumbnail
// url="...">）。
func firstThumbnailURL(item *xmlquery.Node) string {
	for node := item.FirstChild; node != nil; node = node.NextSibling {
		if node.Type == xmlquery.ElementNode && node.Data == "thumbnail" {
			return attrValue(node, "url")
		}
	}
	return ""
}

func nodeInnerText(node *xmlquery.Node) string {
	if node == nil {
		return ""
	}
	return strings.TrimSpace(node.InnerText())
}

func attrValue(node *xmlquery.Node, key string) string {
	for _, attr := range node.Attr {
		if attr.Name.Local == key {
			return attr.Value
		}
	}
	return ""
}
