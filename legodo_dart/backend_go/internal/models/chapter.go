package models

import "time"

// Chapter 章节实体（对齐 Python reading_engine.py:ChapterItem）
type Chapter struct {
	// 书籍 URL
	BookURL string `json:"bookUrl"`
	// 章节序号
	Index int `json:"index"`
	// 章节名
	Title string `json:"title"`
	// 章节 URL
	URL string `json:"url"`
	// 是否 VIP
	IsVip bool `json:"isVip"`
	// 是否付费
	IsPay bool `json:"isPay"`
	// 更新时间
	UpdateTime *time.Time `json:"updateTime,omitempty"`
	// 字数
	WordCount int `json:"wordCount"`
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
