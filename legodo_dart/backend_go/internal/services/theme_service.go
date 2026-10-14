package services

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/legodo-booksource/legodo_go/core"
)

// ThemePreferences holds UI theme settings.
type ThemePreferences struct {
	ScreenMode    string            `json:"screenMode"`    // "light", "dark", "system"
	FontSize      int               `json:"fontSize"`
	FontFamily    string            `json:"fontFamily"`
	LineSpacing   float64           `json:"lineSpacing"`
	ParagraphSpac float64           `json:"paragraphSpac"`
	BgColor       string            `json:"bgColor"`
	TxtColor      string            `json:"txtColor"`
	MarginPercent int               `json:"marginPercent"`
	Image         map[string]string `json:"image,omitempty"` // background image mapping
}

// ThemeService manages theme preferences and custom backgrounds.
type ThemeService struct {
	base     *core.BaseService
	settings *core.SettingsService
	storage  *core.StorageService
	backgroundsDir string
}

// NewThemeService creates a new theme service.
func NewThemeService(settings *core.SettingsService, storage *core.StorageService) *ThemeService {
	bgDir := filepath.Join(storage.DataDir(), "cache", "backgrounds")
	os.MkdirAll(bgDir, 0755)
	return &ThemeService{
		base:             core.NewBaseService("ThemeService"),
		settings:         settings,
		storage:          storage,
		backgroundsDir:   bgDir,
	}
}

// Start initializes.
func (s *ThemeService) Start() error { return s.base.Start() }
func (s *ThemeService) Stop() error  { return s.base.Stop() }

// GetPreferences returns current theme preferences as JSON string.
func (s *ThemeService) GetPreferences() (string, error) {
	val := s.settings.Get("theme.preferences", "{}")
	var prefs ThemePreferences
	if err := json.Unmarshal([]byte(val), &prefs); err != nil {
		prefs = ThemePreferences{
			ScreenMode: "light",
			FontSize:   16,
			BgColor:    "#FFFFFF",
			TxtColor:   "#333333",
		}
	}
	data, err := json.MarshalIndent(prefs, "", "  ")
	return string(data), err
}

// SavePreferences saves theme preferences to settings.
func (s *ThemeService) SavePreferences(rawPrefs string) error {
	var prefs ThemePreferences
	if err := json.Unmarshal([]byte(rawPrefs), &prefs); err != nil {
		return fmt.Errorf("invalid preferences: %w", err)
	}
	return s.settings.Set("theme.preferences", rawPrefs)
}

// GetPreferenceField returns a single preference field value.
func (s *ThemeService) GetPreferenceField(field string) interface{} {
	val := s.settings.Get("theme.preferences", "{}")
	var prefs ThemePreferences
	if err := json.Unmarshal([]byte(val), &prefs); err != nil {
		return nil
	}
	switch field {
	case "screenMode":
		return prefs.ScreenMode
	case "fontSize":
		return prefs.FontSize
	case "fontFamily":
		return prefs.FontFamily
	case "lineSpacing":
		return prefs.LineSpacing
	case "bgColor":
		return prefs.BgColor
	case "txtColor":
		return prefs.TxtColor
	case "marginPercent":
		return prefs.MarginPercent
	default:
		return nil
	}
}

// BackgroundPaths returns the paths of available background images.
func (s *ThemeService) BackgroundPaths() ([]string, error) {
	fileInfos, err := os.ReadDir(s.backgroundsDir)
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, fi := range fileInfos {
		if !fi.IsDir() {
			paths = append(paths, filepath.Join(s.backgroundsDir, fi.Name()))
		}
	}
	return paths, nil
}
