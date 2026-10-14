package services

import (
	"context"
	"fmt"
	"log"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// ServiceState represents lifecycle state.
type ServiceState = core.ServiceState

const (
	StateInit      core.ServiceState = "INIT"
	StateStarting  core.ServiceState = "STARTING"
	StateRunning   core.ServiceState = "RUNNING"
	StateStopping  core.ServiceState = "STOPPING"
	StateDisposed  core.ServiceState = "DISPOSED"
)

// ServiceHub manages all registered services, their lifecycle, and provides access.
type ServiceHub struct {
	mu          sync.RWMutex
	services    map[string]core.Service
	order       []string
	startOrder  []string
	stopOrder   []string

	// Shared components (set during registration)
	eventBus        *core.EventBus
	storageSvc      *core.StorageService
	settingsSvc     *core.SettingsService
	httpClient      *httpclient.ClientSession
	jsRuntime       *jsruntime.Runtime
	ruleEngine      *engine.RuleEngine
	sourceSvc       *SourceService
	checkSvc        *SourceCheckService
	readingSvc      *ReadingService
	searchSvc       *ContentSearchService
	rssSvc          *RSSService
	bookshelfSvc    *BookshelfService
	readerSvc       *ReaderService
	replaceSvc      *ReplaceService
	fontSvc         *FontService
	themeSvc        *ThemeService
	updateSvc       *UpdateService
	browserSvc      *BrowserService
	qrSvc           *QRService
	exportSvc       *ExportService
	webdavSvc       *WebDAVService
	importSvc       *ImportService
	readAloudSvc    *ReadAloudService
}

// NewServiceHub creates a new service hub.
func NewServiceHub() *ServiceHub {
	return &ServiceHub{
		services: make(map[string]core.Service),
		order:    make([]string, 0),
	}
}

// registerNamed adds a named service with lifecycle callbacks.
func (h *ServiceHub) registerNamed(name string, svc serviceAdapter) error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if _, exists := h.services[name]; exists {
		return fmt.Errorf("service %s already registered", name)
	}

	wrapper := &serviceWrapper{adapter: svc, name: name}
	h.services[name] = wrapper
	h.order = append(h.order, name)
	return nil
}

// Get retrieves a service by name.
func (h *ServiceHub) Get(name string) (core.Service, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	svc, ok := h.services[name]
	return svc, ok
}

// All returns all registered services in registration order.
func (h *ServiceHub) All() []core.Service {
	h.mu.RLock()
	defer h.mu.RUnlock()
	result := make([]core.Service, 0, len(h.services))
	for _, name := range h.order {
		if svc, ok := h.services[name]; ok {
			result = append(result, svc)
		}
	}
	return result
}

// StartAll starts all services in dependency order.
func (h *ServiceHub) StartAll(ctx context.Context) error {
	order := h.topoOrder()
	h.startOrder = order
	h.stopOrder = make([]string, len(order))
	for i := len(order) - 1; i >= 0; i-- {
		h.stopOrder[len(order)-1-i] = order[i]
	}

	for _, name := range order {
		svc := h.services[name]
		log.Printf("[ServiceHub] starting: %s", name)
		if err := svc.Start(); err != nil {
			log.Printf("[ServiceHub] service %s failed: %v", name, err)
		}
	}
	return nil
}

// StopAll stops all services in reverse order.
func (h *ServiceHub) StopAll() error {
	for i := len(h.stopOrder) - 1; i >= 0; i-- {
		name := h.stopOrder[i]
		svc := h.services[name]
		log.Printf("[ServiceHub] stopping: %s", name)
		if err := svc.Stop(); err != nil {
			log.Printf("[ServiceHub] service %s error: %v", name, err)
		}
	}
	return nil
}

// StatusReport collects status from all services.
func (h *ServiceHub) StatusReport() []core.ServiceStatus {
	h.mu.RLock()
	defer h.mu.RUnlock()
	report := make([]core.ServiceStatus, 0, len(h.services))
	for _, svc := range h.services {
		report = append(report, svc.Status())
	}
	return report
}

// Has checks if a service is registered.
func (h *ServiceHub) Has(name string) bool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	_, ok := h.services[name]
	return ok
}

// topoOrder returns topological sort of service names.
func (h *ServiceHub) topoOrder() []string {
	h.mu.RLock()
	defer h.mu.RUnlock()

	indeg := make(map[string]int)
	edges := make(map[string][]string)

	for _, name := range h.order {
		svc := h.services[name]
		if svc == nil {
			continue
		}
		deps := svc.Dependencies()
		indeg[name] = len(deps)
		for _, dep := range deps {
			if _, exists := h.services[dep]; exists {
				edges[dep] = append(edges[dep], name)
			}
		}
	}

	var ready []string
	for _, name := range h.order {
		if indeg[name] == 0 {
			ready = append(ready, name)
		}
	}

	var order []string
	for len(ready) > 0 {
		name := ready[0]
		ready = ready[1:]
		order = append(order, name)
		for _, nxt := range edges[name] {
			indeg[nxt]--
			if indeg[nxt] == 0 {
				ready = append(ready, nxt)
			}
		}
	}

	if len(order) != len(h.services) {
		log.Printf("[ServiceHub] cycle in dependencies, using registration order")
		return h.order
	}
	return order
}

// --- Accessors for shared components ---

func (h *ServiceHub) EventBus() *core.EventBus            { return h.eventBus }
func (h *ServiceHub) StorageService() *core.StorageService { return h.storageSvc }
func (h *ServiceHub) SettingsService() *core.SettingsService { return h.settingsSvc }
func (h *ServiceHub) HTTPClient() *httpclient.ClientSession { return h.httpClient }
func (h *ServiceHub) JSRuntime() *jsruntime.Runtime        { return h.jsRuntime }
func (h *ServiceHub) RuleEngine() *engine.RuleEngine       { return h.ruleEngine }
func (h *ServiceHub) SourceService() *SourceService        { return h.sourceSvc }
func (h *ServiceHub) CheckService() *SourceCheckService          { return h.checkSvc }
func (h *ServiceHub) ReadingService() *ReadingService      { return h.readingSvc }
func (h *ServiceHub) ContentSearchService() *ContentSearchService { return h.searchSvc }
func (h *ServiceHub) RSSService() *RSSService              { return h.rssSvc }
func (h *ServiceHub) BookshelfService() *BookshelfService  { return h.bookshelfSvc }
func (h *ServiceHub) ReaderService() *ReaderService        { return h.readerSvc }
func (h *ServiceHub) ReplaceService() *ReplaceService      { return h.replaceSvc }
func (h *ServiceHub) FontService() *FontService            { return h.fontSvc }
func (h *ServiceHub) ThemeService() *ThemeService          { return h.themeSvc }
func (h *ServiceHub) UpdateService() *UpdateService        { return h.updateSvc }
func (h *ServiceHub) BrowserService() *BrowserService      { return h.browserSvc }
func (h *ServiceHub) QRService() *QRService                { return h.qrSvc }
func (h *ServiceHub) ExportService() *ExportService        { return h.exportSvc }
func (h *ServiceHub) WebDAVService() *WebDAVService        { return h.webdavSvc }
func (h *ServiceHub) ImportService() *ImportService        { return h.importSvc }
func (h *ServiceHub) ReadAloudService() *ReadAloudService  { return h.readAloudSvc }

// --- Internal interfaces ---

type serviceAdapter interface {
	Name() string
	Dependencies() []string
	Start(ctx context.Context) error
	Stop() error
	Status() core.ServiceStatus
}

type serviceWrapper struct {
	adapter serviceAdapter
	name    string
}

func (w *serviceWrapper) Name() string             { return w.adapter.Name() }
func (w *serviceWrapper) Dependencies() []string   { return w.adapter.Dependencies() }
func (w *serviceWrapper) SetBus(*core.EventBus)    {}
func (w *serviceWrapper) SetHub(hub *core.ServiceHub) {}
func (w *serviceWrapper) Start() error             { return w.adapter.Start(context.Background()) }
func (w *serviceWrapper) Stop() error              { return w.adapter.Stop() }
func (w *serviceWrapper) Status() core.ServiceStatus { return w.adapter.Status() }

type infraSvc struct {
	base   *core.BaseService
	onStart func(ctx context.Context) error
	onStop func() error
	extra  interface{}
	bus    *core.EventBus
	stopFn func() error
}

func (i *infraSvc) Name() string                       { return i.base.Name() }
func (i *infraSvc) Dependencies() []string             { return i.base.Dependencies() }
func (i *infraSvc) Start(ctx context.Context) error {
	if i.onStart != nil {
		if err := i.onStart(ctx); err != nil {
			return err
		}
	}
	return i.base.Start()
}
func (i *infraSvc) Stop() error {
	if i.stopFn != nil {
		if err := i.stopFn(); err != nil {
			return err
		}
	}
	return i.base.Stop()
}
func (i *infraSvc) Status() core.ServiceStatus      { return i.base.Status() }

type bizSvc struct {
	base    *core.BaseService
	onStart func(ctx context.Context) error
	stopFn  func() error
}

func (b *bizSvc) Name() string                     { return b.base.Name() }
func (b *bizSvc) Dependencies() []string           { return b.base.Dependencies() }
func (b *bizSvc) Start(ctx context.Context) error {
	if b.onStart != nil {
		if err := b.onStart(ctx); err != nil {
			return err
		}
	}
	return b.base.Start()
}
func (b *bizSvc) Stop() error {
	if b.stopFn != nil {
		if err := b.stopFn(); err != nil {
			return err
		}
	}
	return b.base.Stop()
}
func (b *bizSvc) Status() core.ServiceStatus      { return b.base.Status() }
