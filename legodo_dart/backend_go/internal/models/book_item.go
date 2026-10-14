package models

// BookItem 书籍项（搜索结果/书架项）
type BookItem struct {
	Name              string   `json:"name"`
	Author            string   `json:"author"`
	Intro             string   `json:"intro"`
	URL               string   `json:"url"`
	CoverURL          string   `json:"coverUrl"`
	TocURL            string   `json:"tocUrl"`
	Kind              string   `json:"kind"`
	LastChapter       string   `json:"lastChapter"`
	LatestChapterTitle string  `json:"latestChapterTitle"`
	WordCount         string   `json:"wordCount"`
	DownloadURLs      []string `json:"downloadUrls,omitempty"`
	Origin            string   `json:"origin"`
	OriginName        string   `json:"originName"`
	Type              int      `json:"type"`
	TotalChapterNum   int      `json:"totalChapterNum"`
	DurChapterIndex   int      `json:"durChapterIndex"`
	DurChapterTitle   string   `json:"durChapterTitle"`
	CanUpdate         bool     `json:"canUpdate"`
	CustomIntro       string   `json:"customIntro"`
	CustomTag         string   `json:"customTag"`
	CustomCoverURL    string   `json:"customCoverUrl"`
	Charset           string   `json:"charset"`
	Group             int      `json:"group"`
	LatestChapterTime int64    `json:"latestChapterTime"`
	LastCheckTime     int64    `json:"lastCheckTime"`
	LastCheckCount    int      `json:"lastCheckCount"`
	DurChapterPos     int      `json:"durChapterPos"`
	DurChapterTime    int64    `json:"durChapterTime"`
	Order             int      `json:"order"`
	OriginOrder       int      `json:"originOrder"`
	ReadConfig        map[string]interface{} `json:"readConfig,omitempty"`
	InfoHTML          string   `json:"infoHtml"`
	TocHTML           string   `json:"tocHtml"`
	Variables         map[string]interface{} `json:"variables,omitempty"`
	Origins           []string `json:"origins"`
}
