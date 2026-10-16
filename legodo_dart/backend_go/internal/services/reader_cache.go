package services

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/legodo-booksource/legodo_go/internal/models"
)

// ReadingCache 书目录/正文的磁盘缓存（对齐 Python reading_cache.ReadingCache）。
//
// 键与磁盘名字是**同一条公式**：目录名 = sha256("\0".join(书源 url, 书 url))，
// 章文件名 = sha256(章 url)——与 Python 侧逐字节一致，flet 时代落盘的缓存
// 直接就能读（这正是这次要接上的：Dart 版此前完全看不见那批目录）。
type ReadingCache struct {
	root string
}

// readingCacheVersion 与 Python ReadingCache.VERSION 对齐：新写一律 version 2。
// 读方向兼容 1 和 2（目录文件的两版形状没差、正文的字节块本轮只用文本块）。
const readingCacheVersion = 2

// NewReadingCache 建一个挂在 <cacheRoot>/reader-cache 下的缓存（cacheRoot 即
// StorageService.CacheDir()——reader-cache 这一层由本类型自己补）。
func NewReadingCache(cacheRoot string) *ReadingCache {
	return &ReadingCache{root: filepath.Join(cacheRoot, "reader-cache")}
}

func cacheDigest(values ...string) string {
	joined := strings.Join(values, "\x00")
	sum := sha256.Sum256([]byte(joined))
	return hex.EncodeToString(sum[:])
}

func (c *ReadingCache) bookDir(sourceURL, bookURL string) string {
	return filepath.Join(c.root, cacheDigest(sourceURL, bookURL))
}

// cacheCatalog 盘上 catalog.json 的形状（键名对齐 Python asdict(ChapterItem)
// 的 snake_case；缺的键按零值——Python 侧 dataclass 有默认值，双方互读都成立）。
type cacheCatalog struct {
	Version  int            `json:"version"`
	SavedAt  int64          `json:"saved_at"`
	Chapters []cacheChapter `json:"chapters"`
}

type cacheChapter struct {
	Name      string `json:"name"`
	URL       string `json:"url"`
	Index     int    `json:"index"`
	IsVolume  bool   `json:"is_volume"`
	IsVip     bool   `json:"is_vip"`
	IsPay     bool   `json:"is_pay"`
	Tag       string `json:"tag"`
	WordCount string `json:"word_count"`
}

// cacheContent 盘上 content/<digest>.json 的形状（只出文本块；字节块的 .bin
// 兄弟文件不过协议面，跨进程渲染走 media 那条路——本轮还没接）。
type cacheContent struct {
	Version    int                 `json:"version"`
	Title      string              `json:"title"`
	ImageStyle string              `json:"image_style"`
	RawContent string              `json:"raw_content"`
	Blocks     []cacheContentBlock `json:"blocks"`
}

type cacheContentBlock struct {
	Kind  string `json:"kind"`
	Value string `json:"value"`
	Bytes bool   `json:"bytes"`
}

func readJSONFile(path string, target interface{}) bool {
	raw, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return json.Unmarshal(raw, target) == nil
}

func writeJSONAtomic(path string, data interface{}) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	temporary := fmt.Sprintf("%s.%d.tmp", path, time.Now().UnixNano())
	if err := os.WriteFile(temporary, payload, 0644); err != nil {
		return err
	}
	return os.Rename(temporary, path)
}

// LoadCatalog 读盘上目录。ok=false 覆盖三种"当没有"：文件不在、解析不了、
// version 不是 1/2（对齐 Python load_catalog 返回 None 的那几支）。
func (c *ReadingCache) LoadCatalog(sourceURL, bookURL string) ([]*models.Chapter, bool) {
	var data cacheCatalog
	if !readJSONFile(filepath.Join(c.bookDir(sourceURL, bookURL), "catalog.json"), &data) {
		return nil, false
	}
	if data.Version != 1 && data.Version != 2 {
		return nil, false
	}
	chapters := make([]*models.Chapter, 0, len(data.Chapters))
	for _, item := range data.Chapters {
		chapters = append(chapters, &models.Chapter{
			BookURL:       bookURL,
			Index:         item.Index,
			Title:         item.Name,
			URL:           item.URL,
			IsVolume:      item.IsVolume,
			IsVip:         item.IsVip,
			IsPay:         item.IsPay,
			Tag:           item.Tag,
			WordCountText: item.WordCount,
		})
	}
	return chapters, true
}

// SaveCatalog 目录落盘（version 2，原子写）。取目录成功后调用；失败静默——
// 磁盘写不进去不该把"目录拿到了"这一发弄坏（对齐 Python：只有读方向的容错）。
func (c *ReadingCache) SaveCatalog(sourceURL, bookURL string, chapters []*models.Chapter) {
	items := make([]cacheChapter, 0, len(chapters))
	for _, chapter := range chapters {
		if chapter == nil {
			continue
		}
		items = append(items, cacheChapter{
			Name:      chapter.Title,
			URL:       chapter.URL,
			Index:     chapter.Index,
			IsVolume:  chapter.IsVolume,
			IsVip:     chapter.IsVip,
			IsPay:     chapter.IsPay,
			Tag:       chapter.Tag,
			WordCount: chapter.WordCountText,
		})
	}
	payload := cacheCatalog{
		Version:  readingCacheVersion,
		SavedAt:  time.Now().Unix(),
		Chapters: items,
	}
	_ = writeJSONAtomic(filepath.Join(c.bookDir(sourceURL, bookURL), "catalog.json"), payload)
}

// LoadContent 读盘上一章正文（只取文本块，按序拼回段间 "\n"——Python 存盘时
// 文本块值本身就是带换行的正文，拼回来过 ParagraphLines 就是同一串段）。
func (c *ReadingCache) LoadContent(sourceURL, bookURL, chapterURL string) (*models.ChapterContent, bool) {
	path := filepath.Join(c.bookDir(sourceURL, bookURL), "content",
		cacheDigest(chapterURL)+".json")
	var data cacheContent
	if !readJSONFile(path, &data) {
		return nil, false
	}
	if data.Version != 1 && data.Version != 2 {
		return nil, false
	}
	texts := make([]string, 0, len(data.Blocks))
	for _, block := range data.Blocks {
		if block.Kind != "text" || block.Bytes || block.Value == "" {
			continue
		}
		texts = append(texts, block.Value)
	}
	if len(texts) == 0 {
		return nil, false
	}
	return &models.ChapterContent{
		ChapterURL: chapterURL,
		Title:      data.Title,
		Content:    strings.Join(texts, "\n"),
	}, true
}
