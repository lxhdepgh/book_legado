package services

import (
	"regexp"
	"strings"
)

// 自然段切分与章内坐标：书内索引、阅读进度共用的一把尺
// （对齐 Python app/core/text_layout.py）。
//
// 正文"一段"是谁定义的，决定了三件事能不能对上：索引里的第 k 行、进度里
// 落库的字符偏移、以及保存时夹回的那一段。所以切段只此一份。
// 标题识别与段首缩进是显示的事，不进这里——缩进每段多两个字，偏移就整体漂。

var inlineSpaceRE = regexp.MustCompile(`[ \t\f\v]+`)

// ParagraphLines 正文原文 → 自然段文本列表（不缩进、不认标题）。
// 换行符统一、\u00a0 当普通空格、行内连续空白压成一个空格、去首尾空白、空行整条丢掉。
func ParagraphLines(value string) []string {
	normalized := strings.ReplaceAll(value, "\r\n", "\n")
	normalized = strings.ReplaceAll(normalized, "\r", "\n")
	normalized = strings.ReplaceAll(normalized, "\u00a0", " ")
	lines := make([]string, 0)
	for _, raw := range strings.Split(normalized, "\n") {
		// TrimSpace 吃 Unicode 空白（含全角空格 \u3000），与 Python str.strip() 同一口径。
		line := strings.TrimSpace(inlineSpaceRE.ReplaceAllString(raw, " "))
		if line != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

// ChapterParagraphs 正文文本块 → 本章自然段列表（媒体块不产段，与显示面同一口径）。
func ChapterParagraphs(contents []string) []string {
	paragraphs := make([]string, 0)
	for _, content := range contents {
		if content == "" {
			continue
		}
		paragraphs = append(paragraphs, ParagraphLines(content)...)
	}
	return paragraphs
}

// ParagraphOffsets 每段段首在章内字符流里的偏移（"\n".join(paragraphs) 的下标）。
func ParagraphOffsets(paragraphs []string) []int {
	offsets := make([]int, 0, len(paragraphs))
	cursor := 0
	for _, text := range paragraphs {
		offsets = append(offsets, cursor)
		cursor += len([]rune(text)) + 1
	}
	return offsets
}

// ChapterTextLength 本章字符流总长（空章为 0；段间那个换行算一格）。
func ChapterTextLength(paragraphs []string) int {
	if len(paragraphs) == 0 {
		return 0
	}
	total := len(paragraphs) - 1
	for _, text := range paragraphs {
		total += len([]rune(text))
	}
	return total
}
