package services

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
)

// selectedFontFamilyKey 是当前选中字体族的 settings 键（对齐 Python
// app/services/tools/font_service.py 的 SELECTED_FAMILY_KEY）。阅读器排版
// 偏好读的就是这同一个键，另写一个键会导致选完字体正文不变字。
const selectedFontFamilyKey = "reader.font_family"

// fontExts 是可枚举的字体扩展名。
var fontExts = map[string]bool{
	".ttf": true, ".otf": true, ".ttc": true, ".woff": true, ".woff2": true,
}

// FontEntry 一行字体：系统字体（只读枚举，不落库）与用户字体（fonts 表行）
// 共用这个形状。font 表列对齐 Python schema.py v5：id/name/family/path。
type FontEntry struct {
	ID     int64  `json:"id"`
	Name   string `json:"name"`
	Family string `json:"family"`
	Path   string `json:"path"`
	Source string `json:"source"`
}

// FontService manages user-installed fonts and system font scanning.
type FontService struct {
	base     *core.BaseService
	storage  *core.StorageService
	settings *core.SettingsService
	mu       sync.RWMutex
	userDir  string
	cached   []FontEntry
}

// NewFontService creates a new font service.
func NewFontService(storage *core.StorageService, settings *core.SettingsService, fontUserDir string) *FontService {
	return &FontService{
		base:     core.NewBaseService("FontService", "StorageService"),
		storage:  storage,
		settings: settings,
		userDir:  fontUserDir,
		cached:   make([]FontEntry, 0),
	}
}

// Start loads user fonts from DB.
func (s *FontService) Start() error {
	s.base.SetOnStart(func() error { return s.loadAllFonts() })
	return s.base.Start()
}
func (s *FontService) Stop() error { return s.base.Stop() }

func (s *FontService) loadAllFonts() error {
	rows, err := s.storage.Query("SELECT id, name, family, path FROM fonts ORDER BY id ASC")
	if err != nil || rows == nil {
		return err
	}
	defer rows.Close()

	entries := make([]FontEntry, 0)
	for rows.Next() {
		var f FontEntry
		if scanErr := rows.Scan(&f.ID, &f.Name, &f.Family, &f.Path); scanErr != nil {
			continue
		}
		f.Source = "user"
		entries = append(entries, f)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	s.cached = entries
	s.mu.Unlock()
	return nil
}

// List returns registered user fonts.
func (s *FontService) List() []FontEntry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	cp := make([]FontEntry, len(s.cached))
	copy(cp, s.cached)
	return cp
}

// systemFontDirs 按平台分支列出系统字体目录（对齐 Python _system_font_dirs）。
func systemFontDirs() []string {
	home, _ := os.UserHomeDir()
	switch runtime.GOOS {
	case "windows":
		return []string{`C:\Windows\Fonts`}
	case "darwin":
		return []string{
			filepath.Join(home, "Library", "Fonts"),
			"/Library/Fonts",
		}
	default:
		xdg := os.Getenv("XDG_DATA_HOME")
		if xdg == "" {
			xdg = filepath.Join(home, ".local", "share")
		}
		return []string{
			filepath.Join(home, ".fonts"),
			filepath.Join(xdg, "fonts"),
			"/usr/share/fonts",
		}
	}
}

// ScanSystem enumerates system font files (read-only, not persisted).
// 族名用文件名主干近似：Python 走 fontTools 读 name 表，Go 暂无字体解析。
func (s *FontService) ScanSystem() ([]FontEntry, error) {
	found := make([]FontEntry, 0)
	seen := make(map[string]bool)
	for _, dir := range systemFontDirs() {
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue // 目录不存在/不可读跳过
		}
		for _, fi := range entries {
			if fi.IsDir() || !fontExts[strings.ToLower(filepath.Ext(fi.Name()))] {
				continue
			}
			path := filepath.Join(dir, fi.Name())
			key := strings.ToLower(path)
			if seen[key] {
				continue
			}
			seen[key] = true
			family := strings.TrimSuffix(fi.Name(), filepath.Ext(fi.Name()))
			found = append(found, FontEntry{Name: family, Family: family, Path: path, Source: "system"})
		}
	}
	return found, nil
}

// AddUserFont copies a font file into the data-dir fonts/ and registers it.
// 同路径已登记过直接回已有 id（UNIQUE(path)）；目标名加 unix 秒前缀防重名。
func (s *FontService) AddUserFont(sourcePath string) (int64, error) {
	src := filepath.Clean(sourcePath)
	if fi, err := os.Stat(src); err != nil || fi.IsDir() {
		return 0, fmt.Errorf("字体文件不存在: %s", sourcePath)
	}
	if err := os.MkdirAll(s.userDir, 0755); err != nil {
		return 0, fmt.Errorf("create font dir: %w", err)
	}
	family := strings.TrimSuffix(filepath.Base(src), filepath.Ext(src))
	dest := filepath.Join(s.userDir, fmt.Sprintf("%d_%s", time.Now().Unix(), filepath.Base(src)))

	srcFile, err := os.Open(src)
	if err != nil {
		return 0, fmt.Errorf("open source: %w", err)
	}
	defer srcFile.Close()
	dstFile, err := os.Create(dest)
	if err != nil {
		return 0, fmt.Errorf("create dest: %w", err)
	}
	defer dstFile.Close()
	if _, err = dstFile.ReadFrom(srcFile); err != nil {
		return 0, err
	}

	result, err := s.storage.Execute(
		`INSERT OR IGNORE INTO fonts (name, family, path) VALUES (?, ?, ?)`,
		family, family, dest,
	)
	if err != nil {
		return 0, err
	}
	var id int64
	if n, _ := result.RowsAffected(); n > 0 {
		id, _ = result.LastInsertId()
	} else {
		row := s.storage.QueryRow("SELECT id FROM fonts WHERE path = ?", dest)
		_ = row.Scan(&id)
	}
	if id == 0 {
		return 0, fmt.Errorf("register font failed: %s", dest)
	}

	entry := FontEntry{ID: id, Name: family, Family: family, Path: dest, Source: "user"}
	s.mu.Lock()
	s.cached = append(s.cached, entry)
	s.mu.Unlock()
	return id, nil
}

// RemoveUserFont deletes the registration row and the copied font file.
func (s *FontService) RemoveUserFont(id int64) (bool, error) {
	row := s.storage.QueryRow("SELECT path FROM fonts WHERE id = ?", id)
	var path string
	if err := row.Scan(&path); err != nil {
		return false, nil // 行不存在：Python 侧同样回 False 无错
	}
	if _, err := s.storage.Execute("DELETE FROM fonts WHERE id = ?", id); err != nil {
		return false, err
	}
	if path != "" {
		_ = os.Remove(path)
	}
	s.mu.Lock()
	for i, f := range s.cached {
		if f.ID == id {
			s.cached = append(s.cached[:i], s.cached[i+1:]...)
			break
		}
	}
	s.mu.Unlock()
	return true, nil
}

// SelectActive sets the active reading font family (settings key reader.font_family).
func (s *FontService) SelectActive(family string) error {
	return s.settings.Set(selectedFontFamilyKey, family)
}

// SelectedFamily returns the active reading font family ("" = system default).
func (s *FontService) SelectedFamily() string {
	return s.settings.Get(selectedFontFamilyKey, "")
}
