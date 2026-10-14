package services

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// ImportResult holds the outcome of an import operation.
type ImportResult struct {
	Sources int      `json:"sources"`
	Errors  []string `json:"errors"`
}

// BookSourceImport is a simplified source for import parsing.
type BookSourceImport struct {
	URL   string `json:"bookSourceUrl"`
	Name  string `json:"bookSourceName"`
	Group string `json:"bookSourceGroup"`
}

// ImportService handles text-based and URL-based book source import.
type ImportService struct {
	base      *core.BaseService
	sourceSvc *SourceService
	bus       *core.EventBus
	httpC     *http.Client
	mu        sync.Mutex
	cache     map[string]string // url -> JSON string
}

// NewImportService creates a new import service.
func NewImportService(sourceSvc *SourceService, bus *core.EventBus, hc *http.Client) *ImportService {
	return &ImportService{
		base:      core.NewBaseService("ImportService", "SourceService"),
		sourceSvc: sourceSvc,
		bus:       bus,
		httpC:     hc,
		cache:     make(map[string]string),
	}
}

// Start initializes.
func (s *ImportService) Start() error { return s.base.Start() }
func (s *ImportService) Stop() error  { return s.base.Stop() }

// ImportText imports book sources from raw text.
// Supports both single-source JSON and multi-source JSON array.
func (s *ImportService) ImportText(text string) (*ImportResult, error) {
	text = strings.TrimSpace(text)
	result := &ImportResult{}

	var srcs []*BookSourceImport

	if len(text) > 0 && text[0] == '[' {
		srcs, result.Errors = parseJSONArrayOfSources(text)
	} else if len(text) > 0 && text[0] == '{' {
		src, err := parseSingleSourceObject(text)
		if err != nil {
			result.Errors = append(result.Errors, fmt.Sprintf("parse single: %v", err))
			result.Sources = 0
			return result, nil
		}
		srcs = append(srcs, src)
	} else {
		// Try each line as a potential JSON block
		lines := strings.Split(text, "\n")
		currentBlock := ""
		for _, line := range lines {
			line = strings.TrimSpace(line)
			if line == "" || line == "---" || line == "===" {
				if currentBlock != "" {
					srcs = s.parseBlock(currentBlock, result)
					currentBlock = ""
				}
			} else {
				currentBlock += line + "\n"
			}
		}
		if currentBlock != "" {
			srcs = append(srcs, s.parseBlock(currentBlock, result)...)
		}
	}

	result.Sources = len(srcs)
	for _, src := range srcs {
		s.mu.Lock()
		if src.URL != "" {
			s.cache[src.URL] = src.Name
		}
		s.mu.Unlock()
	}

	return result, nil
}

func (s *ImportService) parseBlock(text string, result *ImportResult) []*BookSourceImport {
	text = strings.TrimSpace(text)
	if len(text) > 0 && text[0] == '[' {
		srcs, errs := parseJSONArrayOfSources(text)
		result.Errors = append(result.Errors, errs...)
		return srcs
	}
	src, err := parseSingleSourceObject(text)
	if err != nil {
		result.Errors = append(result.Errors, fmt.Sprintf("parse block: %v", err))
		return nil
	}
	return []*BookSourceImport{src}
}

// ImportFromURL fetches and imports book sources from a remote URL.
func (s *ImportService) ImportFromURL(url string) (*ImportResult, error) {
	resp, err := s.httpC.Get(url)
	if err != nil {
		return nil, fmt.Errorf("fetch import URL: %w", err)
	}
	defer resp.Body.Close()

	bodyBytes, _ := io.ReadAll(io.LimitReader(resp.Body, 2*1024*1024))
	return s.ImportText(string(bodyBytes))
}

// GetCached returns cached import data for a source URL.
func (s *ImportService) GetCached(url string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.cache[url]
	return v, ok
}

// --- Parsing helpers ---

func parseSingleSourceObject(text string) (*BookSourceImport, error) {
	var src BookSourceImport
	if err := json.Unmarshal([]byte(text), &src); err != nil {
		return nil, fmt.Errorf("unmarshal: %w", err)
	}
	if src.URL == "" {
		return nil, fmt.Errorf("missing bookSourceUrl")
	}
	return &src, nil
}

func parseJSONArrayOfSources(text string) ([]*BookSourceImport, []string) {
	var srcs []BookSourceImport
	err := json.Unmarshal([]byte(text), &srcs)
	if err != nil {
		return nil, []string{fmt.Sprintf("parse array: %v", err)}
	}
	result := make([]*BookSourceImport, 0)
	for _, src := range srcs {
		if src.URL != "" {
			result = append(result, &src)
		}
	}
	return result, nil
}
