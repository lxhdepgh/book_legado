package models

import (
	"encoding/json"
	"time"
)

// BookSourceType 书源类型枚举
type BookSourceType int

const (
	TypeText   BookSourceType = 0 // 文本
	TypeAudio  BookSourceType = 1 // 音频
	TypeImage  BookSourceType = 2 // 图片
	TypeFile   BookSourceType = 3 // 文件下载
	TypeVideo  BookSourceType = 4 // 视频
)

// BookSource 书源实体（对齐 Python models/book_source.py + Legado BookSource.kt）
type BookSource struct {
	// 主键：书源 URL
	BookSourceURL string `json:"bookSourceUrl"`
	// 书源名称
	BookSourceName string `json:"bookSourceName"`
	// 分组
	BookSourceGroup *string `json:"bookSourceGroup,omitempty"`
	// 类型
	BookSourceType BookSourceType `json:"bookSourceType"`
	// 详情页 URL 正则
	BookURLPattern *string `json:"bookUrlPattern,omitempty"`
	// 手动排序编号
	CustomOrder int `json:"customOrder"`
	// 是否启用
	Enabled bool `json:"enabled"`
	// 启用发现
	EnabledExplore bool `json:"enabledExplore"`
	// JS 库
	JsLib *string `json:"jsLib,omitempty"`
	// 启用 CookieJar
	EnabledCookieJar *bool `json:"enabledCookieJar,omitempty"`
	// 并发率（如 "1000" 表示 1 秒 1 次）
	ConcurrentRate *string `json:"concurrentRate,omitempty"`
	// 请求头（JSON 字符串）
	Header *string `json:"header,omitempty"`
	// 登录地址
	LoginURL *string `json:"loginUrl,omitempty"`
	// 登录 UI 配置
	LoginUI *string `json:"loginUi,omitempty"`
	// 登录检测 JS
	LoginCheckJS *string `json:"loginCheckJs,omitempty"`
	// 封面解密 JS
	CoverDecodeJS *string `json:"coverDecodeJs,omitempty"`
	// 注释
	BookSourceComment *string `json:"bookSourceComment,omitempty"`
	// 自定义变量说明
	VariableComment *string `json:"variableComment,omitempty"`
	// 最后更新时间
	LastUpdateTime time.Time `json:"lastUpdateTime"`
	// 响应时间（毫秒）
	RespondTime int64 `json:"respondTime"`
	// 权重（智能排序）
	Weight int `json:"weight"`
	// 发现 URL
	ExploreURL *string `json:"exploreUrl,omitempty"`
	// 发现筛选规则
	ExploreScreen *string `json:"exploreScreen,omitempty"`
	// 搜索 URL
	SearchURL *string `json:"searchUrl,omitempty"`

	// 规则段
	RuleSearch    *SearchRule    `json:"ruleSearch,omitempty"`
	RuleExplore   *ExploreRule   `json:"ruleExplore,omitempty"`
	RuleBookInfo  *BookInfoRule  `json:"ruleBookInfo,omitempty"`
	RuleToc       *TocRule       `json:"ruleToc,omitempty"`
	RuleContent   *ContentRule   `json:"ruleContent,omitempty"`
	RuleReview    *ReviewRule    `json:"ruleReview,omitempty"`

	// 事件监听
	EventListener bool `json:"eventListener"`
	// 自定义按钮
	CustomButton bool `json:"customButton"`
}

// GetSearchRule 获取搜索规则（懒初始化）
func (bs *BookSource) GetSearchRule() *SearchRule {
	if bs.RuleSearch != nil {
		return bs.RuleSearch
	}
	bs.RuleSearch = &SearchRule{}
	return bs.RuleSearch
}

// GetContentRule 获取正文规则
func (bs *BookSource) GetContentRule() *ContentRule {
	if bs.RuleContent != nil {
		return bs.RuleContent
	}
	bs.RuleContent = &ContentRule{}
	return bs.RuleContent
}

// GetBookInfoRule 获取书籍信息规则
func (bs *BookSource) GetBookInfoRule() *BookInfoRule {
	if bs.RuleBookInfo != nil {
		return bs.RuleBookInfo
	}
	bs.RuleBookInfo = &BookInfoRule{}
	return bs.RuleBookInfo
}

// GetTocRule 获取目录规则
func (bs *BookSource) GetTocRule() *TocRule {
	if bs.RuleToc != nil {
		return bs.RuleToc
	}
	bs.RuleToc = &TocRule{}
	return bs.RuleToc
}

// UnmarshalJSON 自定义 JSON 解析（对齐 Gson 语义）
func (bs *BookSource) UnmarshalJSON(data []byte) error {
	type Alias BookSource
	aux := &struct {
		LastUpdateTime interface{} `json:"lastUpdateTime"`
		*Alias
	}{
		Alias: (*Alias)(bs),
	}

	if err := json.Unmarshal(data, aux); err != nil {
		return err
	}

	// 处理时间戳（Legado 存的是毫秒时间戳）
	if ts, ok := aux.LastUpdateTime.(float64); ok {
		bs.LastUpdateTime = time.Unix(0, int64(ts)*int64(time.Millisecond))
	}

	return nil
}

// MarshalJSON 自定义 JSON 序列化
func (bs BookSource) MarshalJSON() ([]byte, error) {
	type Alias BookSource
	return json.Marshal(&struct {
		LastUpdateTime int64 `json:"lastUpdateTime"`
		*Alias
	}{
		LastUpdateTime: bs.LastUpdateTime.UnixNano() / int64(time.Millisecond),
		Alias:          (*Alias)(&bs),
	})
}
