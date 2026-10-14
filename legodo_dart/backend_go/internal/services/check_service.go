package services

import (
	"fmt"
	"sync"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// CheckState represents the result of a single source check.
type CheckState string

const (
	StateAvailable    CheckState = "available"
	StateUnavailable  CheckState = "unavailable"
	StateNotChecked   CheckState = "not_checked"
	StateDisabled     CheckState = "disabled"
)

// CheckResult holds the result of checking a single source.
type CheckResult struct {
	SourceURL string
	State     CheckState
	Duration  time.Duration
	Error     string
}

// SourceCheckService manages the four-stage source check pipeline.
type SourceCheckService struct {
	base      *core.BaseService
	sourceSvc *SourceService
	settings  *core.SettingsService
	bus       *core.EventBus
	mu        sync.Mutex
	running   bool
	snapshot  *CheckSummary
	timeout   time.Duration
	concurrency int
	autoDisable bool
}

// NewCheckService creates a new SourceCheckService.
func NewCheckService(sourceSvc *SourceService, settings *core.SettingsService, bus *core.EventBus) *SourceCheckService {
	s := &SourceCheckService{
		base:        core.NewBaseService("SourceCheckService", "SourceService", "SettingsService"),
		sourceSvc:   sourceSvc,
		settings:    settings,
		bus:         bus,
		timeout:     time.Second * 30,
		concurrency: 32,
		autoDisable: false,
	}

	s.base.SetOnStart(func() error { return nil })
	s.base.SetOnStop(func() error { return nil })

	return s
}

// Start initializes the service.
func (s *SourceCheckService) Start() error { return s.base.Start() }

// Stop releases resources.
func (s *SourceCheckService) Stop() error { return s.base.Stop() }

// RunAll starts a full check pipeline on all sources.
func (s *SourceCheckService) RunAll(groups []string) (*CheckSummary, error) {
	s.mu.Lock()
	if s.running {
		s.mu.Unlock()
		return s.snapshot, fmt.Errorf("check already running")
	}
	s.running = true
	s.mu.Unlock()

	sources := s.sourceSvc.List()
	total := len(sources)
	doneCh := make(chan *CheckResult, total)
	sem := make(chan struct{}, s.concurrency)

	for _, src := range sources {
		if !src.Enabled {
			continue
		}
		go func(src models.BookSource) {
			sem <- struct{}{}
			defer func() { <-sem }()

			start := time.Now()
			state := StateNotChecked
			var errMsg string

			// Stage 1: connectivity check (simplified)
			// TODO: actual HTTP request to source URL

			duration := time.Since(start)
			doneCh <- &CheckResult{
				SourceURL: src.BookSourceURL,
				State:     state,
				Duration:  duration,
				Error:     errMsg,
			}
		}(src)
	}

	results := make([]*CheckResult, 0, total)
	for i := 0; i < total; i++ {
		result := <-doneCh
		results = append(results, result)
	}
	close(doneCh)

	s.mu.Lock()
	s.running = false
	s.snapshot = &CheckSummary{
		Results: results,
		Total:   total,
		Done:    len(results),
		UpdatedAt: time.Now(),
	}
	s.mu.Unlock()

	s.bus.Publish(&core.CheckFinishedEvent{
		Done:      len(results),
		Total:     total,
		StateClass: string(StateAvailable),
	})

	return s.snapshot, nil
}

// Snapshot returns the latest check summary.
func (s *SourceCheckService) Snapshot() *CheckSummary {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.snapshot == nil {
		return &CheckSummary{}
	}
	return s.snapshot
}

// Cancel stops any running check.
func (s *SourceCheckService) Cancel() {
	s.mu.Lock()
	s.running = false
	s.mu.Unlock()
}

// CheckSummary holds the aggregated results of a check run.
type CheckSummary struct {
	Results   []*CheckResult
	Total     int
	Done      int
	UpdatedAt time.Time
}
