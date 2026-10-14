package services

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
)

// UpdateInfo contains release information.
type UpdateInfo struct {
	Version   string    `json:"version"`
	URL       string    `json:"url"`
	Download  string    `json:"download"`
	Content   string    `json:"content"`
	PublishAt time.Time `json:"publishAt"`
	IsForce   bool      `json:"isForce"`
}

// UpdateService checks for application updates.
type UpdateService struct {
	base     *core.BaseService
	settings *core.SettingsService
	httpC    *http.Client
}

// NewUpdateService creates a new update service.
func NewUpdateService(settings *core.SettingsService, httpC *http.Client) *UpdateService {
	return &UpdateService{
		base:     core.NewBaseService("UpdateService"),
		settings: settings,
		httpC:    httpC,
	}
}

// Start initializes.
func (s *UpdateService) Start() error { return s.base.Start() }
func (s *UpdateService) Stop() error  { return s.base.Stop() }

// Check checks for a new version via remote URL.
func (s *UpdateService) Check() (*UpdateInfo, error) {
	ignoreVer := s.settings.Get("update.ignore_version", "")
	url := s.settings.Get("update.check_url", "")
	if url == "" {
		url = "https://api.github.com/repos/legado-project/legado/releases/latest"
	}

	resp, err := s.httpC.Get(url)
	if err != nil {
		return nil, fmt.Errorf("check update: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("check update: HTTP %d", resp.StatusCode)
	}

	// Parse JSON response
	var data map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, fmt.Errorf("parse update info: %w", err)
	}

	version := getStringField(data, "tag_name")
	downloadURL := getStringField(data, "html_url")

	if ignoreVer == version {
		return nil, nil // user ignored this version
	}

	info := &UpdateInfo{
		Version: version,
		URL:     downloadURL,
	}

	// Get download link from assets
	if assets, ok := data["assets"].([]interface{}); ok && len(assets) > 0 {
		if asset, ok := assets[0].(map[string]interface{}); ok {
			info.Download = getStringField(asset, "browser_download_url")
		}
	}

	content := getStringField(data, "body")
	if content != "" {
		info.Content = content
		info.PublishAt = time.Now()
	}

	return info, nil
}

// IgnoreVersion sets the version to ignore.
func (s *UpdateService) IgnoreVersion(version string) {
	s.settings.Set("update.ignore_version", version)
}

// SetCheckURL configures the update check endpoint.
func (s *UpdateService) SetCheckURL(url string) {
	s.settings.Set("update.check_url", url)
}

// getStringField extracts a string field from a map.
func getStringField(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		return fmt.Sprintf("%v", v)
	}
	return ""
}

var _ = io.EOF // used for import reference

// getString is the string package; trimWhitespace is alias.
func getString(s string) string { return strings.TrimSpace(s) }
