package services

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// FontEntry represents a font available for reading.
type FontEntry struct {
	ID       int64  `json:"id"`
	Family   string `json:"family"`
	Path     string `json:"path"`
	System   bool   `json:"isSystem"`
}

// FontService manages user-installed fonts and system font scanning.
type FontService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
	mu      sync.RWMutex
	userDir string
	cached  []FontEntry
}

// NewFontService creates a new font service.
func NewFontService(storage *core.StorageService, bus *core.EventBus, fontUserDir string) *FontService {
	return &FontService{
		base:    core.NewBaseService("FontService", "StorageService"),
		storage: storage,
		bus:     bus,
		userDir: fontUserDir,
		cached:  make([]FontEntry, 0),
	}
}

// Start loads fonts from DB and scans user dir.
func (s *FontService) Start() error {
	s.base.SetOnStart(func() error { return s.loadAllFonts() })
	return s.base.Start()
}
func (s *FontService) Stop() error { return s.base.Stop() }

func (s *FontService) loadAllFonts() error {
	rows, err := s.storage.Query("SELECT id, family, path, is_system FROM fonts")
	if err != nil || rows == nil {
		return err
	}
	defer rows.Close()

	for rows.Next() {
		var f FontEntry
		if scanErr := rows.Scan(&f.ID, &f.Family, &f.Path, &f.System); scanErr != nil {
			continue
		}
		s.cached = append(s.cached, f)
	}
	return rows.Err()
}

// List returns all known fonts.
func (s *FontService) List() []FontEntry {
	cp := make([]FontEntry, len(s.cached))
	copy(cp, s.cached)
	return cp
}

// ScanSystem simulates scanning Windows fonts via registry or directory listing.
func (s *FontService) ScanSystem() ([]FontEntry, error) {
	sysDirs := []string{`C:\Windows\Fonts`}
	found := make([]FontEntry, 0)

	for _, dir := range sysDirs {
		fileInfos, err := os.ReadDir(dir)
		if err != nil {
			continue // skip if can't access
		}
		for _, fi := range fileInfos {
			if strings.HasSuffix(strings.ToLower(fi.Name()), ".ttf") ||
				strings.HasSuffix(strings.ToLower(fi.Name()), ".otf") {
				entry := FontEntry{
					Family: strings.TrimSuffix(fi.Name(), filepath.Ext(fi.Name())),
					Path:   filepath.Join(dir, fi.Name()),
					System: true,
				}
				found = append(found, entry)
			}
		}
	}
	return found, nil
}

// AddUserFont copies a font file to user font directory and registers it.
func (s *FontService) AddUserFont(displayName, sourcePath string) (int64, error) {
	os.MkdirAll(s.userDir, 0755)
	destPath := filepath.Join(s.userDir, filepath.Base(sourcePath))
	srcFile, err := os.Open(sourcePath)
	if err != nil {
		return 0, fmt.Errorf("open source: %w", err)
	}
	defer srcFile.Close()
	dstFile, err := os.Create(destPath)
	if err != nil {
		return 0, fmt.Errorf("create dest: %w", err)
	}
	defer dstFile.Close()
	if _, err = dstFile.ReadFrom(srcFile); err != nil {
		return 0, err
	}

	result, err := s.storage.Execute(
		`INSERT INTO fonts (family, path, is_system) VALUES (?, ?, 0)`,
		displayName, destPath,
	)
	if err != nil {
		return 0, err
	}
	id, _ := result.LastInsertId()
	entry := FontEntry{ID: id, Family: displayName, Path: destPath, System: false}
	s.cached = append(s.cached, entry)
	return id, nil
}

// RemoveUserFont removes a user-added font by ID.
func (s *FontService) RemoveUserFont(id int64) error {
	row := s.storage.QueryRow("SELECT path FROM fonts WHERE id=?", id)
	var path string
	if err := row.Scan(&path); err != nil {
		return err
	}
	_, err := s.storage.Execute("DELETE FROM fonts WHERE id=?", id)
	if err == nil {
		os.Remove(path)
		for i, f := range s.cached {
			if f.ID == id {
				s.cached = append(s.cached[:i], s.cached[i+1:]...)
				break
			}
		}
	}
	return err
}

// SelectActive sets the active reading font name in settings.
func (s *FontService) SelectActive(family string) error {
	_, err := s.storage.Execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000)", "reader.font.family", family)
	return err
}
