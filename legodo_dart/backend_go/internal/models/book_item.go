package models

// BookItem 书籍项（搜索结果/书架项）
type BookItem struct {
	Name               string                 `json:"name"`
	Author             string                 `json:"author"`
	Intro              string                 `json:"intro"`
	URL                string                 `json:"url"`
	CoverURL           string                 `json:"coverUrl"`
	TocURL             string                 `json:"tocUrl"`
	Kind               string                 `json:"kind"`
	LastChapter        string                 `json:"lastChapter"`
	LatestChapterTitle string                 `json:"latestChapterTitle"`
	WordCount          string                 `json:"wordCount"`
	DownloadURLs       []string               `json:"downloadUrls,omitempty"`
	Origin             string                 `json:"origin"`
	OriginName         string                 `json:"originName"`
	Type               int                    `json:"type"`
	TotalChapterNum    int                    `json:"totalChapterNum"`
	DurChapterIndex    int                    `json:"durChapterIndex"`
	DurChapterTitle    string                 `json:"durChapterTitle"`
	CanUpdate          bool                   `json:"canUpdate"`
	CustomIntro        string                 `json:"customIntro"`
	CustomTag          string                 `json:"customTag"`
	CustomCoverURL     string                 `json:"customCoverUrl"`
	Charset            string                 `json:"charset"`
	Group              int                    `json:"group"`
	LatestChapterTime  int64                  `json:"latestChapterTime"`
	LastCheckTime      int64                  `json:"lastCheckTime"`
	LastCheckCount     int                    `json:"lastCheckCount"`
	DurChapterPos      int                    `json:"durChapterPos"`
	DurChapterTime     int64                  `json:"durChapterTime"`
	Order              int                    `json:"order"`
	OriginOrder        int                    `json:"originOrder"`
	ReadConfig         map[string]interface{} `json:"readConfig,omitempty"`
	InfoHTML           string                 `json:"infoHtml"`
	TocHTML            string                 `json:"tocHtml"`
	Variables          map[string]interface{} `json:"variables,omitempty"`
	Origins            []string               `json:"origins"`
}

// AddOrigin 记录一个新的来源书源 URL（对齐 Python BookItem.add_origin）。
func (b *BookItem) AddOrigin(origin string) {
	if origin == "" {
		return
	}
	for _, existing := range b.Origins {
		if existing == origin {
			return
		}
	}
	b.Origins = append(b.Origins, origin)
}

// OriginSet 本书的全部来源：主 origin 在前，其余按记录顺序（对齐 Python origin_set）。
func (b *BookItem) OriginSet() []string {
	values := append([]string(nil), b.Origins...)
	if b.Origin != "" {
		contained := false
		for _, existing := range values {
			if existing == b.Origin {
				contained = true
				break
			}
		}
		if !contained {
			values = append([]string{b.Origin}, values...)
		}
	}
	return values
}
