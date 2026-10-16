package services

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// envOr reads an environment variable with a fallback for unset/empty.
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

const appDirName = "LegadoBookSourceCheck"

// defaultDataDir mirrors Python app_paths.py default_app_paths() data 分支。
// 打包版双击运行时若回退 ./data，会在 exe 旁边生一个空库——用户书架/书源全空。
func defaultDataDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "./data"
	}
	switch runtime.GOOS {
	case "windows":
		if local := os.Getenv("LOCALAPPDATA"); local != "" {
			return filepath.Join(local, appDirName)
		}
		return filepath.Join(home, "AppData", "Local", appDirName)
	case "darwin":
		return filepath.Join(home, "Library", "Application Support", appDirName)
	default:
		if xdg := os.Getenv("XDG_DATA_HOME"); xdg != "" {
			return filepath.Join(xdg, appDirName)
		}
		return filepath.Join(home, ".local", "share", appDirName)
	}
}

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

	// LEGADO_BOOKSOURCE_DATA_DIR 是 Python app_paths.py 的既有契约：打包与语料
	// 探针都靠它换库根（见 .probe/r4_service_baseline.py 的 boot()），Go 侧对齐
	// 这一格；没设时与 Python 同走系统标准位置（Windows 是
	// %LOCALAPPDATA%/LegadoBookSourceCheck，即用户真库所在），不再回退 ./data。
	storageDir := envOr("LEGADO_BOOKSOURCE_DATA_DIR", defaultDataDir())
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
	checkSvc := NewCheckService(h)
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
	fontSvc := NewFontService(h.storageSvc, h.eventBus, filepath.Join(h.storageSvc.DataDir(), "user-fonts"))
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
	exportSvc := NewExportService(filepath.Join(h.storageSvc.DataDir(), "export"), h.eventBus)
	h.exportSvc = exportSvc
	if err := h.registerNamed("ExportService", &bizSvc{
		base:   core.NewBaseService("ExportService"),
		stopFn: func() error { return nil },
	}); err != nil {
		return err
	}

	// WebDAVService
	webdavCfg := WebDAVConfig{}
	webdavSvc := NewWebDAVService(&http.Client{}, webdavCfg, h.storageSvc.DataDir(), h.eventBus)
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
