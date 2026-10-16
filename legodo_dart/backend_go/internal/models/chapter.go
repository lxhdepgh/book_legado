package models

import "time"

// Chapter 章节实体（对齐 Python reading_engine.py:ChapterItem）
type Chapter struct {
	// 书籍 URL
	BookURL string `json:"bookUrl"`
	// 章节序号
	Index int `json:"index"`
	// 章节名
	Title string `json:"name"`
	// 章节 URL
	URL string `json:"url"`
	// 是否卷标题（Legado 的 isVolume：不是真章，点了没有正文）
	IsVolume bool `json:"isVolume"`
	// 是否 VIP
	IsVip bool `json:"isVip"`
	// 是否付费
	IsPay bool `json:"isPay"`
	// 章级标记（updateTime 规则的原值或字数拆分后的剩余，对齐 ChapterItem.tag）
	Tag string `json:"tag"`
	// 字数文本（有的源根本不填；字符串形态是协议面口径，对齐 str(word_count)）
	WordCountText string `json:"wordCount"`
	// 字数（数值形态，老字段）
	WordCount int `json:"-"`
	// 更新时间
	UpdateTime *time.Time `json:"updateTime,omitempty"`
}

// ChapterContent 章节内容
type ChapterContent struct {
	// 章节 URL
	ChapterURL string `json:"chapterUrl"`
	// 标题
	Title string `json:"title"`
	// 正文内容
	Content string `json:"content"`
	// 副内容（分页补充）
	SubContent *string `json:"subContent,omitempty"`
	// 下一页 URL
	NextContentURL *string `json:"nextContentUrl,omitempty"`
	// 解析时间
	ParsedAt time.Time `json:"parsedAt"`
}
