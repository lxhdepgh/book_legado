package services

import (
	"context"
	"fmt"
	"net/http"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// RegisterInfrastructure registers core infrastructure services.
func (h *ServiceHub) RegisterInfrastructure() error {
	eventBus := core.NewEventBus()
	h.eventBus = eventBus

	if err := h.registerNamed("EventBus", &infraSvc{
		base:   core.NewBaseService("EventBus"),
		onStart: func(ctx context.Context) error { return nil },
		stopFn: func() error { eventBus.Close(); return nil },
		bus:    eventBus,
	}); err != nil {
		return err
	}

	storageDir := "./data"
	storageSvc, err := core.NewStorageService(storageDir)
	if err != nil {
		return fmt.Errorf("create storage: %w", err)
	}
	h.storageSvc = storageSvc

	if err := h.registerNamed("StorageService", &infraSvc{
		base:   core.NewBaseService("StorageService"),
		stopFn: func() error { storageSvc.Close(); return nil },
	}); err != nil {
		return err
	}

	settingsSvc, err := core.NewSettingsService(storageSvc, eventBus)
	if err != nil {
		return fmt.Errorf("create settings: %w", err)
	}
	h.settingsSvc = settingsSvc

	if err := h.registerNamed("SettingsService", &infraSvc{
		base:   core.NewBaseService("SettingsService", "StorageService"),
		stopFn: func() error { return nil },
		bus:    eventBus,
	}); err != nil {
		return err
	}

	return nil
}

// RegisterBusinessServices registers all business domain services.
func (h *ServiceHub) RegisterBusinessServices() error {
	httpClient := httpclient.NewClient()
	jsRuntime := jsruntime.NewRuntime()
	ruleEngine := engine.NewRuleEngine(httpClient, jsRuntime)

	h.httpClient = httpClient
	h.jsRuntime = jsRuntime
	h.ruleEngine = ruleEngine

	// RuleEngineService
	if err := h.registerNamed("RuleEngineService", &bizSvc{
		base:   core.NewBaseService("RuleEngineService"),
		stopFn: func() error { httpClient.Close(); return nil },
	}); err != nil {
		return err
	}

	// SourceService
	sourceSvc := NewSourceService(h.storageSvc, h.settingsSvc, ruleEngine, h.eventBus)
	h.sourceSvc = sourceSvc
	if err := h.registerNamed("SourceService", &bizSvc{
		base:   core.NewBaseService("SourceService", "StorageService", "RuleEngineService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// CheckService
	checkSvc := NewCheckService(sourceSvc, h.settingsSvc, h.eventBus)
	h.checkSvc = checkSvc
	if err := h.registerNamed("SourceCheckService", &bizSvc{
		base:   core.NewBaseService("SourceCheckService", "SourceService", "SettingsService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ReadingService
	readingSvc := NewReadingService(h)
	h.readingSvc = readingSvc
	if err := h.registerNamed("ReadingService", &bizSvc{
		base:   core.NewBaseService("ReadingService", "SourceService", "RuleEngineService"),
		stopFn: func() error { readingSvc.Close(); return nil },
	}); err != nil {
		return err
	}

	// ContentSearchService
	searchSvc := NewContentSearchService(h.storageSvc, h.eventBus)
	h.searchSvc = searchSvc
	if err := h.registerNamed("ContentSearchService", &bizSvc{
		base:   core.NewBaseService("ContentSearchService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// RSSService
	rssSvc := NewRSSService(h.storageSvc, h.eventBus, httpClient)
	h.rssSvc = rssSvc
	if err := h.registerNamed("RSSService", &bizSvc{
		base:   core.NewBaseService("RSSService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// BookshelfService
	bookshelfSvc := NewBookshelfService(h.storageSvc, h.eventBus)
	h.bookshelfSvc = bookshelfSvc
	if err := h.registerNamed("BookshelfService", &bizSvc{
		base:   core.NewBaseService("BookshelfService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ReaderService
	readerSvc := NewReaderService(h.storageSvc, h.eventBus)
	h.readerSvc = readerSvc
	if err := h.registerNamed("ReaderService", &bizSvc{
		base:   core.NewBaseService("ReaderService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ReplaceService
	replaceSvc := NewReplaceService(h.storageSvc, h.eventBus)
	h.replaceSvc = replaceSvc
	if err := h.registerNamed("ReplaceService", &bizSvc{
		base:   core.NewBaseService("ReplaceService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// FontService
	fontSvc := NewFontService(h.storageSvc, h.eventBus, "./data/user-fonts")
	h.fontSvc = fontSvc
	if err := h.registerNamed("FontService", &bizSvc{
		base:   core.NewBaseService("FontService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ThemeService
	themeSvc := NewThemeService(h.settingsSvc, h.storageSvc)
	h.themeSvc = themeSvc
	if err := h.registerNamed("ThemeService", &bizSvc{
		base:   core.NewBaseService("ThemeService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// UpdateService
	updateSvc := NewUpdateService(h.settingsSvc, &http.Client{})
	h.updateSvc = updateSvc
	if err := h.registerNamed("UpdateService", &bizSvc{
		base:   core.NewBaseService("UpdateService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// BrowserService
	browserSvc := NewBrowserService()
	h.browserSvc = browserSvc
	if err := h.registerNamed("BrowserService", &bizSvc{
		base:   core.NewBaseService("BrowserService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// QRService
	qrSvc := NewQRService()
	h.qrSvc = qrSvc
	if err := h.registerNamed("QRService", &bizSvc{
		base:   core.NewBaseService("QRService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ExportService
	exportSvc := NewExportService("./data/export", h.eventBus)
	h.exportSvc = exportSvc
	if err := h.registerNamed("ExportService", &bizSvc{
		base:   core.NewBaseService("ExportService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// WebDAVService
	webdavCfg := WebDAVConfig{}
	webdavSvc := NewWebDAVService(&http.Client{}, webdavCfg, "./data", h.eventBus)
	h.webdavSvc = webdavSvc
	if err := h.registerNamed("WebDAVService", &bizSvc{
		base:   core.NewBaseService("WebDAVService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ImportService
	importSvc := NewImportService(sourceSvc, h.eventBus, &http.Client{})
	h.importSvc = importSvc
	if err := h.registerNamed("ImportService", &bizSvc{
		base:   core.NewBaseService("ImportService", "SourceService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// ReadAloudService
	readAloudSvc := NewReadAloudService(h.storageSvc, h.eventBus)
	h.readAloudSvc = readAloudSvc
	if err := h.registerNamed("ReadAloudService", &bizSvc{
		base:   core.NewBaseService("ReadAloudService", "StorageService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	return nil
}
