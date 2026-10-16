package services

import "strconv"

// ReadingPosition 续读位置：reader.position_get / position_save / reader.open 的
// position 那一格（三处共用一份形状，对齐 Python ReadingPosition.as_reading）。
//
// **线上键名是蛇形**（golden reader.position_get.json）：chapter_url / chapter_name /
// chapter_index / scroll_offset / scroll_ratio / chapter_pos / chapter_paragraph /
// font_size / last_read_at。抄错一个下划线不会报错，那一格静默落默认值。
//
// chapter_pos = 章内字符偏移（text_layout 那把尺），像素 scroll_offset 不跨端比。
// chapter_paragraph 可空：nil = 这份存档没记过段号，不是"第 0 段"。
type ReadingPosition struct {
	ChapterURL      string
	ChapterName     string
	ChapterIndex    int
	ScrollOffset    float64
	ScrollRatio     float64
	CharacterOffset int
	ParagraphIndex  *int
	FontSize        int
	LastReadAt      int64
}

// DefaultReadingPosition 没有存档时的那一份（font_size 16，与 Python 缺省一致）。
func DefaultReadingPosition() ReadingPosition {
	return ReadingPosition{FontSize: 16}
}

// readingPositionFromDict 客户端报的坐标（position_json）→ ReadingPosition，
// 口径对齐 Python ReadingPosition.from_reading：负偏移归零、比例夹 [0,1]、
// 段号只认"存了且非负"、字号至少 1。
func readingPositionFromDict(raw map[string]interface{}) ReadingPosition {
	position := DefaultReadingPosition()
	if raw == nil {
		return position
	}
	position.ChapterURL = mapString(raw, "chapter_url")
	position.ChapterName = mapString(raw, "chapter_name")
	position.ChapterIndex = mapInt(raw, "chapter_index")
	position.ScrollOffset = mapFloat(raw, "scroll_offset")
	if position.ScrollOffset < 0 {
		position.ScrollOffset = 0
	}
	position.ScrollRatio = mapFloat(raw, "scroll_ratio")
	if position.ScrollRatio < 0 {
		position.ScrollRatio = 0
	}
	if position.ScrollRatio > 1 {
		position.ScrollRatio = 1
	}
	position.CharacterOffset = mapInt(raw, "chapter_pos")
	if position.CharacterOffset < 0 {
		position.CharacterOffset = 0
	}
	position.ParagraphIndex = paragraphOrNone(raw["chapter_paragraph"])
	// Python: max(1, int(value.get("font_size", 16) or 16))——缺/空/0 落 16，负数落 1。
	position.FontSize = mapInt(raw, "font_size")
	if position.FontSize == 0 {
		position.FontSize = 16
	} else if position.FontSize < 0 {
		position.FontSize = 1
	}
	position.LastReadAt = mapInt64(raw, "last_read_at")
	return position
}

// asReading 存档形状（9 键，键名蛇形）——position_get / position_save / reader.open
// 三处的回话都走这一份。
func (p ReadingPosition) asReading() map[string]interface{} {
	var paragraph interface{}
	if p.ParagraphIndex != nil {
		paragraph = *p.ParagraphIndex
	}
	return map[string]interface{}{
		"chapter_url":       p.ChapterURL,
		"chapter_name":      p.ChapterName,
		"chapter_index":     p.ChapterIndex,
		"scroll_offset":     p.ScrollOffset,
		"scroll_ratio":      p.ScrollRatio,
		"chapter_pos":       p.CharacterOffset,
		"chapter_paragraph": paragraph,
		"font_size":         p.FontSize,
		"last_read_at":      p.LastReadAt,
	}
}

// archiveReading 存档那一侧的形状：books 表不存 chapter_url/chapter_name
// （Python 设计 §3.9），所以续读回读时这两格恒为空串——续读的那一章由
// chapter_index 在目录里定位。position_get 与 reader.open 用它。
func (p ReadingPosition) archiveReading() map[string]interface{} {
	archived := p
	archived.ChapterURL = ""
	archived.ChapterName = ""
	return archived.asReading()
}

// paragraphOrNone 段序号只在"存了且非负"时才算数：其余一律 nil（= 未知）。
// 口径对齐 Python _paragraph_or_none：int() 转不过去（None/空串/非数字串）也是 nil。
func paragraphOrNone(value interface{}) *int {
	index, ok := intOrNone(value)
	if !ok || index < 0 {
		return nil
	}
	return &index
}

func intOrNone(value interface{}) (int, bool) {
	switch v := value.(type) {
	case float64:
		return int(v), true
	case int:
		return v, true
	case int64:
		return int(v), true
	case string:
		parsed, err := strconv.Atoi(v)
		if err != nil {
			return 0, false
		}
		return parsed, true
	default:
		return 0, false
	}
}

func mapString(m map[string]interface{}, key string) string {
	if value, ok := m[key].(string); ok {
		return value
	}
	return ""
}

func mapInt(m map[string]interface{}, key string) int {
	index, _ := intOrNone(m[key])
	return index
}

func mapInt64(m map[string]interface{}, key string) int64 {
	switch value := m[key].(type) {
	case float64:
		return int64(value)
	case int:
		return int64(value)
	case int64:
		return value
	case string:
		parsed, err := strconv.ParseInt(value, 10, 64)
		if err != nil {
			return 0
		}
		return parsed
	default:
		return 0
	}
}

func mapFloat(m map[string]interface{}, key string) float64 {
	return floatOrZero(m[key])
}

// floatOrZero interface{} → float64（非数落 0）：JSON 解码与 SQL driver 回来的
// 形状都随来路而变（REAL 回 float64、整数回 int64、也可能回字符串），
// 统一走这一口，别按类型硬断。
func floatOrZero(value interface{}) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case int:
		return float64(v)
	case int64:
		return float64(v)
	case string:
		parsed, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return 0
		}
		return parsed
	default:
		return 0
	}
}
