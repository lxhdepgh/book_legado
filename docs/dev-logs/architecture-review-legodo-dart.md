# Legodo Dart Subproject Architecture Review

## 1. Project Overview

### Tech Stack Summary

| Layer | Technology | Details |
|-------|-----------|---------|
| **UI Framework** | Flutter (SDK `^3.13.2`) | Material Design, Riverpod 3.4.3 state management, go_router 18.0.2 navigation |
| **State Management** | flutter_riverpod ^3.4.3 | AsyncNotifierProvider pattern; all async data flows through StreamProvider / FutureProvider |
| **Navigation** | go_router ^18.0.2 | ShellRoute for app shell, GoRoute for group routes + full-page destinations |
| **Go Backend FFI** | dart:ffi + ffi ^2.1.0 | Single-process, zero-network overhead via unified `Call(action, paramsJSON)` entry point |
| **Legacy Communication** | WebSocket RPC + HTTP | Separate Go process with `/rpc` WebSocket endpoint + binary downloads via HTTP |
| **Build System** | Python script (`build_release.py`) | Compiles Go to shared library, packages Flutter Windows standalone |
| **Target Platform** | Windows-first desktop | DLL loading via `kernel32.dll` HeapAlloc/HeapFree; Linux/macOS stubs exist but untested |

### Architecture Model: Single-Process Flutter UI + Go Engine

```
                           Desktop Process
                           ┌─────────────────────┐
                           │   main.dart          │
                           │   bootstrapEngine()  │
                           │   └─ FFI first       │
                           │   └─ then Go server  │
                           │   └─ then Fake       │
                           └──────┬──────────────┘
                                  │
                  ┌───────────────┼───────────────┐
                  │               │               │
           ┌──────▼──────┐ ┌─────▼──────┐ ┌──────▼──────┐
           │  FFI Mode   │ │ WS Mode    │ │ Fake Mode   │
           │  Adapter    │ │ Repository │ │ Repository  │
           └──────┬──────┘ └─────┬──────┘ └─────────────┘
                  │               │
           ┌──────▼──────┐ ┌─────▼──────┐
           │ GoFFIClient │ │ RpcClient  │
           │ (singleton) │ │ (WebSocket)│
           └──────┬──────┘ └────────────┘
                  │
           ┌──────▼──────┐
           │ Go.Engine .dll│   ← Unified: Call(action, params) → ResultEnvelope
           └──────────────┘
```

### Key Constraints
- **AOT-compatible FFI**: Memory allocation uses native OS APIs (HeapAlloc on Windows), not C allocator
- **No network overhead**: All engine calls are synchronous in-process
- **Unified action dispatch**: Every engine interaction funnels through `Call(action_name, paramsJSON)` returning `ResultEnvelope {ok, data, error, id, version}`
- **Event-driven progress**: Long-running jobs use job mode with event streaming (currently only via WebSocket path; FFI adapter returns error for events stream)

---

## 2. Current Architecture Map

### Directory Structure

```
lib/
├── main.dart                          # Entry: bootstrap engine, handle failure
├── app/
│   ├── bootstrap.dart                 # Engine bootstrap (FFI -> WS -> Fake chain)
│   ├── router.dart                    # Route table: 3 groups + reader + 9 tools
│   └── shell.dart                     # AppShell: NavigationRail/BottomNav, breakpoint at 720px
├── core/
│   ├── di/providers.dart              # DI: engineRepositoryProvider, engineEventsProvider
│   ├── events/                        # Event system
│   │   ├── engine_event.dart          # EngineEvent{name, payload}
│   │   ├── engine_event_client.dart   # WebSocket event consumer
│   │   ├── event_listener.dart        # Lifecycle-aware subscription wrapper
│   │   ├── event_names.dart           # Event name constants
│   │   └── webview_request.dart       # WebView request handling (<useweb>)
│   ├── ffi/engine_ffi.dart            # GoFFIClient singleton (~231 lines)
│   ├── rpc/                           # Protocol layer (CORE - see Section 4)
│   │   ├── engine_repository.dart     # Abstract interface: ~58 methods, 643 lines
│   │   ├── engine_status.dart         # ServiceStatus model
│   │   ├── envelope.dart              # EngineFailureKind enum, RpcEnvelope class
│   │   ├── ffi_engine_adapter.dart    # FFI implementation (~1180 lines) [LARGEST FILE]
│   │   ├── rpc_engine_repository.dart # WebSocket implementation (~909 lines)
│   │   ├── fake_engine_repository.dart# Test double (~1008 lines)
│   │   ├── websocket_rpc_client.dart  # Low-level WebSocket client (~183 lines)
│   │   ├── websocket_engine_adapter.dart # Older intermediate adapter (137 lines)
│   │   ├── engine_endpoint.dart       # Host/port/token for engine process
│   │   ├── engine_process.dart        # Process management helpers
│   │   └── action_budgets.dart        # Action timeout/budget configuration
│   ├── settings/settings_map.dart     # SettingsMap: typed accessors for settings.read
│   ├── webview/webview_host.dart      # WebView integration for <useweb> sources
│   └── widgets/
│       ├── tabbed_group.dart          # TabbedGroup: lazy page tabs, Offstage keep-alive
│       ├── failure_banner.dart        # Error display component
│       └── state_bar.dart             # Status bar widget
├── features/                          # Feature modules (see Section 3)
│   ├── bookshelf/presentation/        # BookshelfScreen, SourceSwitchDialog
│   ├── bookshelf/domain/              # BookshelfNotifier, SourceSwitchNotifier
│   ├── bookshelf/data/                # BookshelfItem, SourceSwitchModels
│   ├── check/presentation/            # CheckScreen
│   ├── check/domain/                  # CheckNotifier
│   ├── check/data/                    # CheckSnapshot, CheckRow, CheckSourceReport, etc.
│   ├── find_books/presentation/       # FindBooksScreen
│   ├── find_books/domain/             # FindBooksNotifier
│   ├── find_books/data/               # BookRow, CategoryRow, SearchScope, BookIntro
│   ├── reader/presentation/           # ReaderScreen
│   ├── reader/domain/                 # ReaderController, TextLayout, ContinuousReading
│   ├── reader/data/                   # ChapterRow, ChapterDocument, BookmarkRow, ReadingPosition, etc.
│   ├── rss/presentation/              # RssScreen
│   ├── rss/domain/                    # RssNotifier
│   ├── rss/data/                      # RssSourceRow, RssArticleRow, RssArticleDetail
│   ├── settings/presentation/         # AppearanceScreen, UpdateScreen, ToolsOverviewScreen, etc.
│   ├── settings/domain/               # AppearanceNotifier
│   ├── settings/data/                 # ThemePreferences, AppTheme skins, UpdateInfo
│   ├── sources/presentation/          # SourcesScreen
│   ├── sources/domain/                # SourcesNotifier
│   └── sources/data/                  # SourceRecord, AuditReport, ImportOutcome, SaveOutcome
```

### Data Flow Diagram

```
User taps "List Books"
       │
       ▼
BookshelfScreen.build()
       │
       ├── ref.watch(bookshelfItemsProvider)
       │        │
       │        ▼
       │   BookshelfNotifier
       │        │
       │        ▼
       │   ref.read(engineRepositoryProvider).listBookshelf()
       │        │
       │        ▼
       │   FFIEngineAdapter.listBookshelf()
       │        │
       │        ▼
       │   GoFFIClient.call("bookshelf_list", {"include_staged": false})
       │        │
       │        ▼
       │   [dart:ffi] → legodo_engine.dll::Call("bookshelf_list", "{}")
       │        │
       │        ▼
       │   Go route() → doBookshelfList(hub) → svc.List() → SQLite → result[]
       │        │
       │        ▼
       │   Parse result['data'] → [BookshelfItem] → emit
       │
       ▼
BookshelfScreen renders item list
```

---

## 3. Feature Module Inventory

| Feature | Screens/Views | Data Models | Domain/Logic | Key Actions |
|---------|--------------|-------------|--------------|-------------|
| **bookshelf** | `BookshelfScreen`, `SourceSwitchDialog` | `BookshelfItem`, `SwitchRun`, `SwitchCandidate`, `SwitchApplyResult` | `BookshelfNotifier`, `SourceSwitchNotifier` | `listBookshelf`, `shelveBook`, `switchSeeds`, `switchScan`, `switchSnapshot`, `switchCancel`, `applySwitch` |
| **check** | `CheckScreen` | `CheckSnapshot`, `CheckSummary`, `CheckTiering`, `CheckRow`, `CheckSourceReport` | `CheckNotifier` | `checkStart`, `checkSnapshot`, `checkCancel`, `checkSource` |
| **find_books** | `FindBooksScreen` | `BookRow`, `CategoryRow`, `SearchScope`, `BookIntro` | `FindBooksNotifier` | `browseCategories`, `browseBooks`, `searchStart`, `searchStop`, `searchScopes` |
| **reader** | `ReaderScreen` | `ChapterRow`, `ChapterDocument`, `BookmarkRow`, `ReadingPosition`, `ReaderSession`, `StagedRecord`, `ContentSearchResult` | `ReaderController`, `TextLayout`, `ContinuousReading` | `openReader`, `closeReader`, `chapterRows`, `chapterDocument`, `readPosition`, `savePosition`, `stagedRecord`, `promoteStaged`, `discardStaged`, `listBookmarks`, `addBookmark`, `removeBookmark`, `searchBookContent` |
| **rss** | `RssScreen` | `RssSourceRow`, `RssArticleRow`, `RssArticleDetail` | `RssNotifier` | `listRssSources`, `listRssArticles`, `openRssArticle`, `addRssSource`, `removeRssSource`, `refreshRssSource`, `refreshAllRssSources`, `markRssArticleRead`, `starRssArticle` |
| **settings** | `AppearanceScreen`, `UpdateScreen`, `ToolsOverviewScreen`, `ReplaceRulesScreen`, `FontScreen`, `QrCodeScreen`, `ExportScreen`, `WebDavScreen`, `WebBridgeScreen`, `TtsScreen`, `BrowserScreen` | `ThemePreferences`, `AppTheme` skins, `UpdateInfo` | `AppearanceNotifier` | `themePreferences`, `saveTheme`, `updateCheck`, `updateIgnore`, tool domain methods (qr, browser, replace, font, export, webdav, webbridge) |
| **sources** | `SourcesScreen` | `SourceRecord`, `AuditReport`, `ImportOutcome`, `SaveOutcome` | `SourcesNotifier` | `listSources`, `loadSource`, `saveSource`, `deleteSource`, `setSourceEnabled`, `setSourceGroup`, `batchSetEnabled`, `batchDelete`, `exportSources`, `auditSource`, `importText`, `importUrl` |

### Navigation Hierarchy

```
AppShell (ShellRoute)
├── /sources (NavigationRail tab)
│   ├── 管理  → SourcesScreen
│   └── 检测  → CheckScreen
├── /reading (NavigationRail tab)
│   ├── 找书  → FindBooksScreen
│   ├── 书架  → BookshelfScreen
│   └── 订阅  → RssScreen
├── /settings (NavigationRail tab)
│   ├── 外观  → AppearanceScreen
│   ├── 更新  → UpdateScreen
│   └── 工具  → ToolsOverviewScreen
│
Full-page destinations (sibling of shell, push/pop):
├── /reader                    → ReaderScreen (full-screen, per-book ProviderScope)
├── /tools/browser             → BrowserScreen
├── /tools/replace-rules       → ReplaceRulesScreen
├── /tools/font                → FontScreen
├── /tools/qr-code             → QrCodeScreen
├── /tools/export              → ExportScreen
├── /tools/webdav              → WebDavScreen
├── /tools/web-bridge          → WebBridgeScreen
└── /tools/tts                 → TtsScreen
```

### TabbedGroup Behavior (migrated from flet)

- **Lazy construction**: Each tab's screen built only on first visit (avoids unnecessary API calls)
- **Offstage keep-alive**: Visited tabs remain mounted -- subscriptions survive tab switches. Switching to another tab makes current tab `Offstage` rather than disposing it.
- **Last-tab memory**: Remembers which tab was last viewed per group (stored in `_lastTab` map in router.dart line 167)
- **Shelf probe heuristic**: For reading group, shows `书架` first if books exist, else `找书`

---

## 4. Core Infrastructure Deep Dive

### 4.1 RPC Layer -- EngineRepository Abstract Interface

**File**: `lib/core/rpc/engine_repository.dart` (643 lines)

The `EngineRepository` abstract class declares ~58 methods across these domains:

| Domain | Methods | Direction | Notes |
|--------|---------|-----------|-------|
| `engine.*` | hello, serviceStatus, shutdown, webContentReady, events | Control | webContentReady is for `<useweb>` reverse rendering; events() returns Stream |
| `update.*` | updateCheck, updateIgnore | Read-only | Version check domain |
| `bookshelf.*` | listBookshelf, shelveBook | CRUD | Shelf management |
| `source.*` | list/load/save/delete/setEnabled/setGroup/batchSetEnabled/batchDelete/export/audit | CRUD | Source management (9 methods) |
| `import.*` | importText, importUrl | Write | Import from text/URL |
| `browse.*` / `search.*` | browseCategories/browseBooks, searchStart/searchStop/searchScopes | Read | Discovery and browsing |
| `check.*` | checkStart/snapshot/cancel/source | Read + Job | Source quality checking |
| `switch.*` | switchSeeds/scan/snapshot/cancel/apply (5 methods) | Job | Source replacement/scanning |
| `rss.*` | list/add/remove/refresh/mark/read/star (9 methods) | CRUD + Job | RSS subscription management |
| `reader.*` | open/close/toc/content/position/get/save/staged/promote/discard/bookmarks/list/add/remove/search (17 methods) | Session | Reading session management |
| `settings/theme.*` | readSettings, writeSetting, themePreferences, saveTheme | Config | App preferences |
| Tool domain | qr, browser, replace, font, export, webdav, webbridge (~15 methods) | Mixed | G4-E Windows tools |

**Default body throws `_missing(action)`** rather than being truly abstract. Benefits:
- New methods added without override get a loud runtime failure ("当前环境不支持 X")
- Widget tests can create `UnwiredEngineRepository` that fails noisily on every call
- Never silently returns null (which would be confused with "empty result")

### 4.2 FFI Layer -- GoFFIClient Singleton

**File**: `lib/core/ffi/engine_ffi.dart` (231 lines)

```dart
class GoFFIClient {
  static GoFFIClient get instance => _instance ??= GoFFIClient._(); // singleton
  void load();           // Opens legodo_engine.dll, resolves Initialize/Call/Cleanup/FreeString
  void initialize();     // Calls engine.Initialize()
  Map<String, dynamic> call(String action, Map<String, dynamic> params); // Unified entry
  void cleanup();        // Calls engine.Cleanup()
}
```

Memory management uses OS-native heap functions:
- Windows: `GetProcessHeap()` + `HeapAlloc()` / `HeapFree()` from `kernel32.dll`
- Avoids C allocator issues in AOT compilation
- UTF-8 encoded strings passed as `char*` pointers, freed with `FreeString`

### 4.3 FFI Engine Adapter -- Thin Wrapper over GoFFIClient

**File**: `lib/core/rpc/ffi_engine_adapter.dart` (1180 lines) -- THE LARGEST SOURCE FILE

Implements `EngineRepository` by calling `GoFFIClient.call(action, params)` then parsing `result['data']`:

```dart
// Shared parsing helpers (lines 42-96) defined once at top:
String _s(Map? m, String k, {String def = ''})     // Safe string extraction
bool _b(Map? m, String k, {bool def = false})       // Safe bool extraction
int _n(Map? m, String k, {int def = 0})             // Safe numeric extraction
double _d(Map? m, String k, {double def = 0})       // Safe double extraction
List<Object?> _l(Map? m, String k)                  // List extraction
Map? _mm(Map? m, String k)                          // Nested map extraction
List<Map> _toMaps(List?)                            // List cast to typed maps

// Core dispatch template (used by every method):
Future<T> _syncCall<T>(String action, Map<String, dynamic> params,
    T Function(dynamic data) parseFn) async {
  final result = _callGo(action, params);  // Returns {ok: bool, data: ?, error: ?}
  if (result['ok'] != true) throw EngineFailure(...);
  return parseFn(result['data']);
}
```

Every method follows identical boilerplate:
```dart
@override
Future<List<BookshelfItem>> listBookshelf({...}) async {
  return await _syncCall<List<BookshelfItem>>('bookshelf_list', {...}, (data) {
    final items = _toMaps(_l(data, 'data') ?? _l(data, ''));  // <-- THE BUG IS HERE
    return items.map((it) => BookshelfItem(
      name: _s(it, 'name'), author: _s(it, 'author'), ...
    )).toList();
  });
}
```

### 4.4 WebSocket RPC Client (Legacy Path)

**Files**: `websocket_rpc_client.dart` (183 lines), `rpc_engine_repository.dart` (909 lines), `websocket_engine_adapter.dart` (137 lines)

Legacy separate-process communication via WebSocket at `/rpc`:
```json
Request:  {"v": 1, "action": "...", "payload": {...}}
Response: {"success": true/false, "data": ..., "error": "...", "id": ...}
```

Used when FFI fails and Go server exe exists. The `RpcEngineRepository` has proper shape checking (`_rows`, `_asMap`, `_asBool`, `_asInt`). The `WebSocketEngineAdapter` appears to be an older intermediate attempt implementing only a subset of methods (searchBooks, getBookInfo, getChapters, getContent, checkSource) and throws UnimplementedError on most others.

### 4.5 Fake Engine Repository

**File**: `lib/core/rpc/fake_engine_repository.dart` (1008 lines)

Complete test double implementing all methods. Features:
- Constructor parameters pre-fill all response types
- Call tracking: `callsFor(action)`, `payloadFor(action)`, `callOrder`
- Delay injection for loading states
- Event emission: `emit(EngineEvent)`, `emitStreamError()`
- Gold sample factory: `fromRows()` ingests real process output

### 4.6 DI Container

**File**: `lib/core/di/providers.dart`

```dart
// Default: unwired (throws loudly on every call)
final Provider<EngineRepository> engineRepositoryProvider =
    Provider((ref) => const UnwiredEngineRepository());

// Engine endpoint for binary downloads (null in test/fake mode)
final Provider<EngineEndpoint?> engineEndpointProvider =
    Provider<EngineEndpoint?>((ref) => null);

// Events stream: listens to engineRepository.events(), retry: null prevents double-retry
final StreamProvider<EngineEvent> engineEventsProvider = StreamProvider(
  (ref) => ref.watch(engineRepositoryProvider).events(),
  retry: (_, _) => null,  // Retry handled by EngineEventClient internally
);
```

Bootstrapping injects via `ProviderScope.overrides`:
```dart
scope(child) => ProviderScope(
  overrides: [
    engineRepositoryProvider.overrideWithValue(repository),
    engineHandleProvider.overrideWithValue(handle),
    engineEndpointProvider.overrideWithValue(handle?.started.endpoint),
  ],
  child,
);
```

### 4.7 Navigation System

**Files**: `router.dart` (352 lines), `shell.dart` (107 lines)

Three-group design mirroring flet's `NAV_META`:
1. **书源 (Sources)**: Management + Check tabs
2. **阅读 (Reading)**: Find Books -> Bookshelf -> Subscribe tabs
3. **配置 (Settings)**: Appearance -> Update -> Tools tabs

Breakpoint-based layout: Wide (>=720px): NavigationRail left + content right. Narrow (<720px): NavigationBar bottom (only with >=2 destinations). Threshold matches flet's `NARROW_WIDTH_THRESHOLD` exactly.

---

## 5. Go Backend Architecture

### 5.1 Directory Structure

```
backend_go/
├── cmd/server/main.go           # Entry point
├── pkg/ffi/engine.go            # ★★★ FFI exports: Initialize, Call, Cleanup, FreeString
├── internal/services/           # Business services (20 files)
│   ├── hub.go                   # ServiceHub: lifecycle, topo-order registration
│   ├── register.go              # Registration helper functions
│   ├── bookshelf_service.go     # BookshelfService
│   ├── source_service.go        # SourceService
│   ├── check_service.go         # SourceCheckService
│   ├── reader_service.go        # ReaderService
│   ├── reading_service.go       # ReadingService
│   ├── search_service.go        # ContentSearchService
│   ├── rss_service.go           # RSSService
│   ├── import_service.go        # ImportService
│   ├── replace_service.go       # ReplaceService
│   ├── font_service.go          # FontService
│   ├── theme_service.go         # ThemeService
│   ├── update_service.go        # UpdateService
│   ├── browser_service.go       # BrowserService
│   ├── qr_service.go            # QRService
│   ├── export_service.go        # ExportService
│   ├── webdav_service.go        # WebDAVService
│   ├── readaloud_service.go     # ReadAloudService
│   ├── event_bus.go             # EventBus
│   └── webdav_service.go
├── internal/engine/rule_engine.go   # Book source rule evaluation
├── internal/httpclient/client.go   # HTTP client session
├── internal/models/                # Internal domain models (BookSource, BookItem, Chapter)
├── core/                           # Shared infrastructure (ServiceHub trait, EventBus, Storage, Settings)
├── gateway/                        # WebSocket gateway (separate-process mode)
├── pkg/jsruntime/runtime.go        # Goja JavaScript runtime
├── pkg/ruleparser/parser.go        # Rule parsing
└── go.mod                          # Go 1.26, modernc.org/sqlite, goja, gorilla/websocket
```

### 5.2 FFI Entry Point -- `pkg/ffi/engine.go` (1024 lines)

Three exported C functions plus one free:
```go
//export Initialize  -> boot ServiceHub (infra + business services, start all)
//export Call        -> universal router: action_raw + params_json -> ResultEnvelope JSON string
//export Cleanup     -> stop all services, nil hub pointer
//export FreeString  -> release C strings returned by Call
```

#### Convention-Based Routing

```go
func route(h *services.ServiceHub, action string, p map[string]interface{}) ResultEnvelope {
    switch action {
    case "source_list", "source.list": return doSourceList(h)
    case "bookshelf_list", "bookshelf.list": return doBookshelfList(h)
    case "reader_open", "reader.open": return doReaderOpen(h, p)
    // ... ~60 cases supporting both snake_case and dot notation
    default:
        return failResult("unimplemented: "+action, "")
    }
}
```

### 5.3 Handler Patterns

Two patterns exist:

**Full handler** (real logic):
```go
func doSourceList(h *services.ServiceHub) ResultEnvelope {
    if svc := h.SourceService(); svc != nil {
        srcs := svc.List()
        data := make([]interface{}, len(srcs))
        for i, src := range srcs {
            data[i] = srcToMap(src)
        }
        return okResult(data, "")
    }
    return failResult("source service unavailable", "")
}
```

**Stub handler** (returns placeholder):
```go
case "search_start", "search.start":
    return okResult(map[string]interface{}{"jobId": "", "status": "stub"}, "")
```

### 5.4 ServiceHub Pattern

Manages ~20 services with dependency-ordered startup/shutdown via topological sort. Infrastructure services: EventBus, Storage, Settings, HttpClient, JSRuntime, RuleEngine. Business services: Source, Check, Bookshelf, Reader, Search, RSS, etc. Twenty accessor methods provide typed access (e.g., `h.SourceService()`, `h.CheckService()`).

---

## 6. Identified Problems

### P0 -- Critical

**P0.1: Protocol mismatch -- `_l(data, 'data')` extracts from already-unwrapped data**

In `FFIEngineAdapter._syncCall`:
```dart
final result = _callGo(action, params);  // Returns {ok: true, data: [...], error: ""}
return parseFn(result['data']);           // ← data is ALREADY the bare array/record
```

But EVERY parse function expects a ResultEnvelope and calls `_l(data, 'data')`:
```dart
_toMaps(_l(data, 'data') ?? _l(data, ''))   // Expects result["data"]["data"]
```

**Impact**: When Go returns `{"ok":true,"data":[{"name":"..."}]}`, Dart calls `_l([{"name":"..."}], 'data')` which returns `null` because arrays have no keys. So **all list-returning methods return empty results**.

**Affected methods** (at least ~20): `listBookshelf`, `listSources`, `browseCategories`, `browseBooks`, `switchSeeds`, `listRssSources`, `listRssArticles`, `chapterRows`, `listBookmarks`, and more.

### P0.2: Many Go backend handlers are stubs returning placeholder data

Critical stubs blocking functionality:

| Handler | Returns | Impact |
|---------|---------|--------|
| `search_start` | `{jobId:"",status:"stub"}` | Multi-source search blocked |
| `switch_seeds` | `{seeds:[]}` | Source switching seeds blocked |
| `switch_snapshot` | `{total:0,done:0}` | Switch progress always zero |
| `reader_toc` | `[]` | Chapter list always empty |
| `reader_content` | `{title:"",contents:[]}` | Chapter content always empty |
| `reader_search_content` | `[]` | In-book search broken |
| `browse_categories` | `{categories:[]}` | Category browsing blocked |
| `browse_books` | `{books:[]}` | Browse listing blocked |

### P1 -- High

**P1.1: EngineRepository imports feature models** (core/rpc imports features/*)

```dart
import '../../features/check/data/check_models.dart';
import '../../features/find_books/data/book_row.dart';
import '../../features/reader/data/bookmark_row.dart';
// ... 15+ feature imports
```

Not a true circular dependency (features don't import core/rpc), but means protocol file grows with every new feature. The comment in engine_repository.dart acknowledges this trade-off.

**P1.2: FFIEngineAdapter is ~1180 lines of mostly parsing boilerplate**

Second-largest file. Most methods follow identical structure: call Go -> check ok -> unwrap data -> extract fields. Makes it hard to audit individual method correctness and difficult to refactor when data shapes change.

**P1.3: Parser logic scattered inline**

Parsing helpers like `_parseCheckRows`, `_parseReadingPosition`, `_parseBlocks` are inside FFIEngineAdapter but belong to their respective domain models.

### P2 -- Medium

**P2.1: RpcEngineRepository (WebSocket) coexists with FFIEngineAdapter (FFI)**

Total ~1229 lines of parallel RPC implementation that could diverge. If FFI is permanent, remove legacy. If multi-process remains supported, add clearer ownership separation.

**P2.2: bootstrap.dart complex fallback chain**

`FFI connect -> Go server process -> FakeEngineRepository -> Throw EngineFailure`. On failure, accesses `GoFFIClient.instance.libraryPath` which creates the singleton even during fallback.

### P3 -- Low

**P3.1: Go backend `go.mod` specifies `go 1.26.0`** -- invalid version (current stable is 1.22.x). Would prevent compilation.

**P3.2: Inconsistent naming convention in Go routing** -- mixing `snake_case` and `dot_notation` across related actions (acceptable per convention but makes grep harder).

---

## 7. Dependencies & Coupling Analysis

### Cross-Module Imports Matrix

| From \ To | core/rpc | bookshelf | find_books | reader | rss | settings | sources |
|-----------|----------|-----------|------------|--------|-----|----------|---------|
| **core/rpc/ffi_engine_adapter** | --- | bookshelf_item, source_switch_models | book_row, category_row, search_scope | bookmark_row, chapter_document, chapter_row, content_search_result, reading_position, reader_session, staged_record | rss_models | theme_preferences, update_models | import_outcome, source_audit, source_record |
| **core/rpc/engine_repository** | --- | bookshelf_item, source_switch_models | book_row, category_row, search_scope | bookmark_row, chapter_document, chapter_row, content_search_result, reading_position, reader_session, staged_record | rss_models | theme_preferences, update_models | import_outcome, source_audit, source_record |
| **core/rpc/rpc_engine_repo** | --- | bookshelf_item, source_switch_models | book_row, category_row, search_scope | bookmark_row, chapter_document, chapter_row, content_search_result, reading_position, reader_session, staged_record | rss_models | theme_preferences, update_models | import_outcome, source_audit, source_record |
| **core/rpc/fake_engine_repo** | --- | bookshelf_item, source_switch_models | book_row, category_row, search_scope | bookmark_row, chapter_document, chapter_row, content_search_result, reading_position, reader_session, staged_record | rss_models | theme_preferences, update_models | import_outcome, source_audit, source_record |
| **features/*/presentation/** | uses engineRepositoryProvider from core/di | Own models + notifier | Own models + notifier | Own models + controller | Own models + notifier | Own models + notifier | Own models + notifier |

### Assessment

- **Risk Level**: MODERATE
- **core/rpc -> features/**: One-directional coupling. Core imports all feature models. Features do NOT import anything from core/rpc. No circular dependency.
- **Model ownership**: `BookRow` owned by `features/find_books/data/`. Core/rpc imports it for type safety. Changing BookRow requires recompiling both the feature AND any RPC adapter.
- **Feature isolation**: Features are clean -- `bookshelf/` does not import `find_books/` models. Only shared via core protocol.

---

## 8. Proposed Architecture & Refactor Plan

### Phase 1: Immediate Fix (Protocol Mismatch) -- ~1 day

Change `_syncCall` signature so parse functions receive both the raw envelope AND unwrapped data:

```dart
// BEFORE (broken):
Future<T> _syncCall<T>(String action, Map<String, dynamic> params,
    T Function(dynamic data) parseFn) async {
  final result = _callGo(action, params);
  return parseFn(result['data']);  // data already unwrapped
}

// AFTER (correct):
Future<T> _syncCall<T>(String action, Map<String, dynamic> params,
    T Function(dynamic data) parser) async {
  final result = _callGo(action, params);
  if (result['ok'] != true) throw EngineFailure(...);
  return parser(result['data']);  // pass directly
}
```

Then fix each method's parse lambda:
```dart
// BEFORE (everywhere):
_l(data, 'data') ?? _l(data, '')

// AFTER (for lists):
(data is List ? data : [])

// AFTER (for single objects):
data as Map?
```

### Phase 2: Parser Extraction -- ~3 days

Move parsing logic out of adapter into dedicated parsers:
```
lib/core/rpc/parsers/
├── parser_utils.dart         # _s, _b, _n, _d, _l, _toMaps, _toStrings
├── book_row_parser.dart      # BookRow.parseFrom(data), BookRowParser.parseMany(data)
├── source_record_parser.dart
├── check_models_parser.dart  # CheckSnapshot, CheckRow, CheckSummary
├── chapter_parser.dart       # ChapterRow, ChapterDocument, ContentBlock
├── rss_parser.dart           # RssSourceRow, RssArticleRow, RssArticleDetail
├── reader_parser.dart        # ReadingPosition, BookmarkRow, ReaderSession
└── shelf_parser.dart         # BookshelfItem, StagedRecord
```

After extraction, adapter methods become one-liners:
```dart
Future<List<BookRow>> browseBooks({...}) async {
  return await _syncCall('browse_books', {...}, BookRowParser.parseMany);
}
```

### Phase 3: Thin Adapter -- ~2 days

After Phase 2, `FFIEngineAdapter` drops from ~1180 to ~200 lines. Just action dispatch + generic `_syncCall` template.

### Phase 4: Cleanup -- ~2 days

- Decide on WebSocket legacy code retention or removal
- Fix `go.mod` version specification
- Add contract test verifying all 58 EngineRepository methods have implementations

### Future State Ideal Directory Structure

```
lib/
├── core/
│   ├── rpc/
│   │   ├── engine_repository.dart          # Abstract contract ONLY (no feature imports)
│   │   ├── parsers/                         # NEW: extracted parsing per domain
│   │   ├── ffi_engine_adapter.dart          # Thin dispatcher (~200 lines)
│   │   ├── fake_engine_repository.dart      # Test double
│   │   └── ...                               # envelope, status, etc.
│   ├── di/providers.dart
│   ├── ffi/engine_ffi.dart
│   └── ...
├── features/                                # UNCHANGED
└── app/                                     # UNCHANGED
```

Note: Moving feature imports OUT of `EngineRepository` to a shared `models/` directory would require significant rework. The current arrangement (core knowing about feature model classes) is acceptable given the project's feature-first directory structure trade-off. The key improvement is extracting parsing logic where the biggest bug surface area lives.

---

## 9. Go Backend Completion Gap

**Estimated completion: ~40-50% of handlers have real implementations**

| Service | Real | Stub | Criticality |
|---------|------|------|-------------|
| SourceService | 6/9 methods | batchEnable/group/export partial | HIGH |
| BookshelfService | 4/4 methods | import stub | MEDIUM |
| CheckService | 4/4 methods | sweep incomplete | HIGH |
| ReaderService | 4/17 methods | toc/content/staged/search all stubbed | CRITICAL |
| ContentSearch | 0/3 methods | all stubbed | HIGH |
| RSSService | 5/9 methods | article detail stubbed | MEDIUM |
| ReplaceService | 1/5 methods | most stubbed | LOW |
| FontService | 0/4 methods | all stubbed | LOW |
| UpdateService | 0/2 methods | all stubbed | LOW |
| BrowserService | 0/3 methods | all stubbed | LOW |
| QRService | 0/2 methods | echoes input | LOW |
| ExportService | 0/2 methods | stubbed | LOW |
| WebDAVService | 0/3 methods | stubbed | LOW |
| ImportService | 1/2 methods | url stubbed | MEDIUM |
| ReadAloudService | 0/4 methods | all stubbed | LOW |

Priority for completion: ReaderService (toc/content), ContentSearchService, SearchService, SourceService.

---

## 10. Action Items (Prioritized Checklist)

| # | Item | Priority | Effort | Blocked By |
|---|------|----------|--------|------------|
| **1** | Fix `_l(data, 'data')` protocol mismatch in FFIEngineAdapter | P0 / Critical | 1 day | None |
| **2** | Extract parsers from FFIEngineAdapter to `core/rpc/parsers/` directory | P1 / High | 3 days | #1 |
| **3** | Reduce FFIEngineAdapter to ~200 thin dispatcher | P1 / High | 2 days | #2 |
| **4** | Complete critical Go stubs: reader.toc/content, search, switch, browse | P1 / High | 5-8 days | — |
| **5** | Decide: keep or remove WebSocket/RPC legacy code | P2 / Medium | 0.5 day (decision) | — |
| **6** | Fix `go.mod` Go version specification | P3 / Low | 5 min | — |
| **7** | Add integration test verifying FFI round-trip success | P2 / Medium | 2 days | #1 |
| **8** | Document action implementation status table for verification | P3 / Low | 1 day | — |
| **9** | Audit all adapter methods for silent-failure patterns (fallback defaults) | P1 / High | 1 day | #1 |
| **10** | Add contract test ensuring all 58 methods have non-default implementations | P2 / Medium | 1 day | — |

### Suggested Execution Order

```
Week 1:  #1 Protocol fix + #9 Silent failure audit
Week 2:  #2 Parser extraction + #3 Adapter thinning
Week 3:  #4 Part 1: Complete critical Go stubs (reader.toc/content, search, switch)
Week 4:  #4 Part 2: Remaining Go stubs + #7 Integration tests  
Week 5:  #5 WebSocket code decision + ship cleaned version
```
