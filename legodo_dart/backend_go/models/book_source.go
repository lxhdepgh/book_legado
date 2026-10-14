package models

import "encoding/json"

// SearchRule represents the search rule fields from a book source JSON.
type SearchRule struct {
	BookList   *string `json:"bookList"`
	Name       *string `json:"name"`
	Author     *string `json:"author"`
	Intro      *string `json:"intro"`
	CoverURL   *string `json:"coverUrl"`
	BookURL    *string `json:"bookUrl"`
	LastChapter *string `json:"lastChapter"`
	TocURL     *string `json:"tocUrl"`
	SearchURL  *string `json:"searchUrl"`
}

// BookInfoRule represents the book info rule fields.
type BookInfoRule struct {
	Name       *string `json:"name"`
	Author     *string `json:"author"`
	Intro      *string `json:"intro"`
	CoverURL   *string `json:"coverUrl"`
	TocURL     *string `json:"tocUrl"`
	InitJS     *string `json:"initJs"`
}

// TocRule represents the table of contents rule fields.
type TocRule struct {
	ChapterList  *string `json:"chapterList"`
	ChapterName  *string `json:"chapterName"`
	ChapterURL   *string `json:"chapterUrl"`
	IsVip        *string `json:"isVip"`
	UpdateStyle  *string `json:"updateStyle"`
}

// ContentRule represents the content rule fields.
type ContentRule struct {
	Title       *string `json:"title"`
	Content     *string `json:"content"`
	SubTitle    *string `json:"subTitle"`
	SubContent  *string `json:"subContent"`
	BaseURL     *string `json:"baseUrl"`
	WebJS       *string `json:"webJs"`
	TTS         *string `json:"tts"`
}

// ExploreRule represents the explore rule fields.
type ExploreRule struct {
	ExploreList *string `json:"exploreList"`
	Name        *string `json:"name"`
	Author      *string `json:"author"`
	Intro       *string `json:"intro"`
	CoverURL    *string `json:"coverUrl"`
	BookURL     *string `json:"bookUrl"`
	LastChapter *string `json:"lastChapter"`
}

// ReviewRule represents the review/discussion rule fields.
type ReviewRule struct {
	ReviewList  *string `json:"reviewList"`
	Author      *string `json:"author"`
	UserName    *string `json:"userName"`
	Content     *string `json:"content"`
	Time        *string `json:"time"`
	ReviewID    *string `json:"reviewId"`
	PraiseCount *string `json:"praiseCount"`
	ReplyCount  *string `json:"replyCount"`
	RootReview  *string `json:"rootReview"`
	NextRevPage *string `json:"nextRevPage"`
}

// BookSource represents a Legado-compatible book source.
// Aligns with Python app/models/book_source.py BookSource.
type BookSource struct {
	BookSourceName    string            `json:"bookSourceName"`
	BookSourceURL     string            `json:"bookSourceUrl"`
	BookSourceGroup   string            `json:"bookSourceGroup,omitempty"`
	BookSourceComment string            `json:"bookSourceComment,omitempty"`
	AuthorCustomField string            `json:"authorCustomField,omitempty"`
	KindCustomField   string            `json:"kindCustomField,omitempty"`
	WordCountCustomField string         `json:"wordCountCustomField,omitempty"`
	LastUpdateTimeCustomField string    `json:"lastUpdateTimeCustomField,omitempty"`
	TableOfContentsCustomField string   `json:"tableOfContentsCustomField,omitempty"`
	CustomSortRule    string            `json:"customSortRule,omitempty"`
	Enabled           bool              `json:"enabled"`
	EnabledCookieJar  bool              `json:"enabledCookieJar"`
	EnabledExplore    bool              `json:"enabledExplore"`
	EnabledReplace    bool              `json:"enabledReplace,omitempty"`
	EnabledAudit      bool              `json:"enabledAudit"`
	ConcurrentRate    string            `json:"concurrentRate,omitempty"`
	CustomOrder       int               `json:"customOrder"`
	RespondTime       int               `json:"respondTime"`
	SearchURL         string            `json:"searchUrl,omitempty"`
	ExploreURL        string            `json:"exploreUrl,omitempty"`
	Header            string            `json:"header,omitempty"`
	BookUrlPattern    string            `json:"bookUrlPattern,omitempty"`
	LoginURL          string            `json:"loginUrl,omitempty"`
	LoginCheckJS      string            `json:"loginCheckJs,omitempty"`
	LoginUI           string            `json:"loginUi,omitempty"`
	RuleSearch        json.RawMessage   `json:"ruleSearch,omitempty"` // will be unmarshaled into SearchRule or raw map
	RuleBookInfo      json.RawMessage   `json:"ruleBookInfo,omitempty"`
	RuleContent       json.RawMessage   `json:"ruleContent,omitempty"`
	RuleToc           json.RawMessage   `json:"ruleToc,omitempty"`
	RuleExplore       json.RawMessage   `json:"ruleExplore,omitempty"`
	RuleReview        json.RawMessage   `json:"ruleReview,omitempty"`
	ExploreScreen     string            `json:"exploreScreen,omitempty"`
	JavaScript        string            `json:"javaScript,omitempty"`
	VariableComment   string            `json:"variableComment,omitempty"`
	CoverDecodeJS     string            `json:"coverDecodeJs,omitempty"`
	RssUrl            string            `json:"rssUrl,omitempty"`
	AllowedMimeType   string            `json:"allowedMimeType,omitempty"`
	AcceptedMimeType  string            `json:"acceptedMimeType,omitempty"`
	LastUpdateTime    int64             `json:"lastUpdateTime"`
	Weight            int               `json:"weight"`
	SkipRegex         string            `json:"skipRegex,omitempty"`
	DiscoveryCategory string            `json:"discoveryCategory,omitempty"`
	FoundSiteCode     string            `json:"foundSiteCode,omitempty"`
}

// GetSearchRule parses the ruleSearch field into SearchRule.
func (b *BookSource) GetSearchRule() (*SearchRule, error) {
	if b.RuleSearch == nil {
		return &SearchRule{}, nil
	}
	var rule SearchRule
	err := json.Unmarshal(b.RuleSearch, &rule)
	return &rule, err
}

// GetBookInfoRule parses the ruleBookInfo field.
func (b *BookSource) GetBookInfoRule() (*BookInfoRule, error) {
	if b.RuleBookInfo == nil {
		return &BookInfoRule{}, nil
	}
	var rule BookInfoRule
	err := json.Unmarshal(b.RuleBookInfo, &rule)
	return &rule, err
}

// GetTocRule parses the ruleToc field.
func (b *BookSource) GetTocRule() (*TocRule, error) {
	if b.RuleToc == nil {
		return &TocRule{}, nil
	}
	var rule TocRule
	err := json.Unmarshal(b.RuleToc, &rule)
	return &rule, err
}

// GetContentRule parses the ruleContent field.
func (b *BookSource) GetContentRule() (*ContentRule, error) {
	if b.RuleContent == nil {
		return &ContentRule{}, nil
	}
	var rule ContentRule
	err := json.Unmarshal(b.RuleContent, &rule)
	return &rule, err
}

// ToMap converts the book source to a flat map (for DB storage as JSON).
func (b *BookSource) ToMap() (map[string]interface{}, error) {
	data, err := json.Marshal(b)
	if err != nil {
		return nil, err
	}
	var m map[string]interface{}
	json.Unmarshal(data, &m)
	return m, nil
}
