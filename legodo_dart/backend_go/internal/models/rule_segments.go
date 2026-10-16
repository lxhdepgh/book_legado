package models

import (
	"bytes"
	"encoding/json"
	"strconv"
)

// RuleText 宽容解码"规则文本"类字段。canReName 在书源 JSON 里历来有
// ""/"1"/"true"/布尔/数字 各种形状（Legado 自身是 String?，真库 917 行里
// 38 行就那么写、被 *bool 整体拒掉——书源列表凭空少一截）。
// 语义对齐 Python reading_engine `bool(str(v or "").strip())`：空（空串、
// false、数值零、null）记为空串，其余标量取文本。
type RuleText string

// UnmarshalJSON implements json.Unmarshaler.
func (r *RuleText) UnmarshalJSON(b []byte) error {
	t := bytes.TrimSpace(b)
	switch {
	case len(t) == 0 || bytes.Equal(t, []byte("null")) || bytes.Equal(t, []byte("false")):
		*r = ""
	case t[0] == '"':
		var s string
		if err := json.Unmarshal(t, &s); err != nil {
			return err
		}
		*r = RuleText(s)
	default:
		if f, err := strconv.ParseFloat(string(t), 64); err == nil && f == 0 {
			*r = ""
			return nil
		}
		*r = RuleText(string(t))
	}
	return nil
}


// BookListRule 书籍列表规则接口（SearchRule/ExploreRule 共用）
type BookListRule interface {
	GetBookList() *string
	GetName() *string
	GetAuthor() *string
	GetIntro() *string
	GetKind() *string
	GetLastChapter() *string
	GetUpdateTime() *string
	GetBookURL() *string
	GetCoverURL() *string
	GetWordCount() *string
}

// SearchRule 搜索规则（对齐 Legado SearchRule.kt）
type SearchRule struct {
	// 校验关键字（literal，非规则）
	CheckKeyWord *string `json:"checkKeyWord,omitempty"`
	// 书籍列表选择器
	BookList *string `json:"bookList"`
	// 书名
	Name *string `json:"name"`
	// 作者
	Author *string `json:"author"`
	// 简介
	Intro *string `json:"intro"`
	// 分类
	Kind *string `json:"kind"`
	// 最新章节
	LastChapter *string `json:"lastChapter"`
	// 更新时间
	UpdateTime *string `json:"updateTime"`
	// 书籍 URL
	BookURL *string `json:"bookUrl"`
	// 封面 URL
	CoverURL *string `json:"coverUrl"`
	// 字数
	WordCount *string `json:"wordCount"`
}

// GetBookList 实现 BookListRule 接口
func (s *SearchRule) GetBookList() *string { return s.BookList }
func (s *SearchRule) GetName() *string     { return s.Name }
func (s *SearchRule) GetAuthor() *string   { return s.Author }
func (s *SearchRule) GetIntro() *string    { return s.Intro }
func (s *SearchRule) GetKind() *string     { return s.Kind }
func (s *SearchRule) GetLastChapter() *string { return s.LastChapter }
func (s *SearchRule) GetUpdateTime() *string  { return s.UpdateTime }
func (s *SearchRule) GetBookURL() *string     { return s.BookURL }
func (s *SearchRule) GetCoverURL() *string    { return s.CoverURL }
func (s *SearchRule) GetWordCount() *string   { return s.WordCount }

// ExploreRule 发现规则（对齐 Legado ExploreRule.kt）
type ExploreRule struct {
	BookList    *string `json:"bookList"`
	Name        *string `json:"name"`
	Author      *string `json:"author"`
	Intro       *string `json:"intro"`
	Kind        *string `json:"kind"`
	LastChapter *string `json:"lastChapter"`
	UpdateTime  *string `json:"updateTime"`
	BookURL     *string `json:"bookUrl"`
	CoverURL    *string `json:"coverUrl"`
	WordCount   *string `json:"wordCount"`
}

func (e *ExploreRule) GetBookList() *string { return e.BookList }
func (e *ExploreRule) GetName() *string     { return e.Name }
func (e *ExploreRule) GetAuthor() *string   { return e.Author }
func (e *ExploreRule) GetIntro() *string    { return e.Intro }
func (e *ExploreRule) GetKind() *string     { return e.Kind }
func (e *ExploreRule) GetLastChapter() *string { return e.LastChapter }
func (e *ExploreRule) GetUpdateTime() *string  { return e.UpdateTime }
func (e *ExploreRule) GetBookURL() *string     { return e.BookURL }
func (e *ExploreRule) GetCoverURL() *string    { return e.CoverURL }
func (e *ExploreRule) GetWordCount() *string   { return e.WordCount }

// BookInfoRule 书籍信息页规则（对齐 Legado BookInfoRule.kt）
type BookInfoRule struct {
	// 初始化 JS
	Init *string `json:"init"`
	// 书名
	Name *string `json:"name"`
	// 作者
	Author *string `json:"author"`
	// 简介
	Intro *string `json:"intro"`
	// 分类
	Kind *string `json:"kind"`
	// 最新章节
	LastChapter *string `json:"lastChapter"`
	// 更新时间
	UpdateTime *string `json:"updateTime"`
	// 封面 URL
	CoverURL *string `json:"coverUrl"`
	// 目录 URL
	TocURL *string `json:"tocUrl"`
	// 字数
	WordCount *string `json:"wordCount"`
	// 是否可重命名（Legado 语义：规则文本，非空 ⇒ 允许；见 RuleText）
	CanReName RuleText `json:"canReName,omitempty"`
	// 下载链接
	DownloadURLs *string `json:"downloadUrls"`
}

// TocRule 目录页规则（对齐 Legado TocRule.kt）
type TocRule struct {
	// 预更新 JS
	PreUpdateJS *string `json:"preUpdateJs"`
	// 章节列表选择器
	ChapterList *string `json:"chapterList"`
	// 章节名
	ChapterName *string `json:"chapterName"`
	// 章节 URL
	ChapterURL *string `json:"chapterUrl"`
	// 格式化 JS
	FormatJS *string `json:"formatJs"`
	// 是否分卷
	IsVolume *string `json:"isVolume"`
	// 是否 VIP
	IsVip *string `json:"isVip"`
	// 是否付费
	IsPay *string `json:"isPay"`
	// 更新时间
	UpdateTime *string `json:"updateTime"`
	// 下一页目录 URL
	NextTocURL *string `json:"nextTocUrl"`
}

// ContentRule 正文页规则（对齐 Legado ContentRule.kt）
type ContentRule struct {
	// 正文内容
	Content *string `json:"content"`
	// 副内容（用于分页）
	SubContent *string `json:"subContent"`
	// 标题
	Title *string `json:"title"`
	// 下一页内容 URL
	NextContentURL *string `json:"nextContentUrl"`
	// Web JS
	WebJS *string `json:"webJs"`
	// 来源正则
	SourceRegex *string `json:"sourceRegex"`
	// 替换正则
	ReplaceRegex *string `json:"replaceRegex"`
	// 图片样式
	ImageStyle *string `json:"imageStyle"`
	// 图片解密
	ImageDecode *string `json:"imageDecode"`
	// 付费操作
	PayAction *string `json:"payAction"`
	// 回调 JS
	CallBackJS *string `json:"callBackJs"`
}

// ReviewRule 段评规则（对齐 Legado ReviewRule.kt）
type ReviewRule struct {
	ReviewURL      *string `json:"reviewUrl"`
	AvatarRule     *string `json:"avatarRule"`
	ContentRule    *string `json:"contentRule"`
	PostTimeRule   *string `json:"postTimeRule"`
	ReviewQuoteURL *string `json:"reviewQuoteUrl"`
	VoteUpURL      *string `json:"voteUpUrl"`
	VoteDownURL    *string `json:"voteDownUrl"`
	PostReviewURL  *string `json:"postReviewUrl"`
	PostQuoteURL   *string `json:"postQuoteUrl"`
	DeleteURL      *string `json:"deleteUrl"`
}
