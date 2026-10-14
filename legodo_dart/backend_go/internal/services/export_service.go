package services

import (
	"archive/zip"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// ExportJob represents an ongoing export task.
type ExportJob struct {
	ID        string `json:"id"`
	Type      string `json:"type"` // "epub" or "txt"
	Status    string `json:"status"` // "running", "done", "failed"
	Done      int    `json:"done"`
	Total     int    `json:"total"`
	ErrorMsg  string `json:"errorMsg"`
	OutputDir string `json:"outputDir"`
}

// ExportService manages EPUB and TXT export of books.
type ExportService struct {
	base   *core.BaseService
	mu     sync.RWMutex
	jobs   map[string]*ExportJob
	output string
	bus    *core.EventBus
}

// NewExportService creates a new export service.
func NewExportService(outputDir string, bus *core.EventBus) *ExportService {
	os.MkdirAll(outputDir, 0755)
	return &ExportService{
		base:   core.NewBaseService("ExportService"),
		jobs:   make(map[string]*ExportJob),
		output: outputDir,
		bus:    bus,
	}
}

// Start initializes.
func (s *ExportService) Start() error { return s.base.Start() }
func (s *ExportService) Stop() error  { return s.base.Stop() }

// CreateJob starts a new export job.
func (s *ExportService) CreateJob(jobID, bookName, format string) *ExportJob {
	job := &ExportJob{
		ID:        jobID,
		Type:      format,
		Status:    "running",
		OutputDir: filepath.Join(s.output, jobID),
	}
	s.mu.Lock()
	s.jobs[jobID] = job
	s.mu.Unlock()
	return job
}

// GetJob returns job status.
func (s *ExportService) GetJob(id string) (*ExportJob, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	j, ok := s.jobs[id]
	if !ok {
		return nil, false
	}
	cp := *j
	return &cp, true
}

// CompleteJob marks a job as done or failed.
func (s *ExportService) CompleteJob(id, status, errMsg string) {
	s.mu.Lock()
	if j, ok := s.jobs[id]; ok {
		j.Status = status
		if errMsg != "" {
			j.ErrorMsg = errMsg
		}
	}
	s.mu.Unlock()
}

// ListJobs returns all export jobs.
func (s *ExportService) ListJobs() []ExportJob {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]ExportJob, 0, len(s.jobs))
	for _, j := range s.jobs {
		result = append(result, *j)
	}
	return result
}

// ExportTXT exports a book's chapters as plain text files.
func (s *ExportService) ExportTXT(bookName string, titleFunc func(string) string, contentFunc func(string) (string, error), chapterCount int) error {
	dir := filepath.Join(s.output, bookName)
	os.MkdirAll(dir, 0755)
	f, err := os.Create(filepath.Join(dir, "output.txt"))
	if err != nil {
		return fmt.Errorf("create txt: %w", err)
	}
	defer f.Close()

	for i := 0; i < chapterCount; i++ {
		title := titleFunc(fmt.Sprintf("chapter-%d", i))
		content, err := contentFunc(fmt.Sprintf("url-%d", i))
		if err == nil {
			f.WriteString("--- " + title + " ---\n")
			f.WriteString(content + "\n\n")
		}
	}
	return nil
}

// ExportEPUB exports chapters into a ZIP file (epub container).
func (s *ExportService) ExportEPUB(bookName string, tocFunc func(int) (string, string, error), chapterCount int) error {
	path := filepath.Join(s.output, bookName+".epub")
	f, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create epub: %w", err)
	}
	defer f.Close()

	w := zip.NewWriter(f)
	defer w.Close()

	// Write TOC placeholder
	writing, _ := w.Create("toc.xhtml")
	writing.Write([]byte("<html><body><h1>TOC</h1></body></html>"))

	for i := 0; i < chapterCount; i++ {
		title, url, err := tocFunc(i)
		if err != nil {
			continue
		}
		_, name := filepath.Split(url)
		pagename := filepath.Join("pages", name+".xhtml")
		page, _ := w.Create(pagename)
		page.Write([]byte("<html><body><h2>" + title + "</h2></body></html>"))
	}
	return nil
}
