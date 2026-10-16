package services

import (
	"encoding/json"
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// 外观偏好的 Go 侧对照物 = Python app/services/tools/theme_service.py（M5，契约 §12）。
//
// 三格 `{skin_id, mode, eink}` 是**线上契约**，键名取自真响应
// （tests/gateway/golden/theme.preferences.json 的 field_names = eink/mode/skin_id），
// 落库走统一 settings 键 themeSettingsKey（旧 appearance.json 已停止写入）。
const (
	// themeSettingsKey 对齐 Python THEME_SETTINGS_KEY；Go 的 settings 值一律 JSON。
	themeSettingsKey = "appearance.snapshot"

	// defaultSkin 对齐 Python DEFAULT_SKIN：表外的皮肤名折回它，不是报错。
	defaultSkin = "classic_blue"
)

// validSkins 是 BUILTIN_SKINS 的 id 集（app/themes/builtin_themes.py:40-53）。
// 调色板本身不在这里——那一份在 Dart 的 kAppSkins（皮肤表连名字带主色），
// Go 只管"这个 id 认不认"，认不出就折回默认，避免界面拿到没定义的皮肤。
var validSkins = map[string]bool{
	"classic_blue":   true,
	"emerald_green":  true,
	"warm_amber":     true,
	"elegant_purple": true,
	"ink_gray":       true,
}

// validModes 对齐 Python VALID_MODES。
var validModes = map[string]bool{"light": true, "dark": true, "system": true}

// ThemePreferences 是三格外观快照（不是"主题数据"：Go 侧不产调色板）。
type ThemePreferences struct {
	SkinID string `json:"skin_id"`
	Mode   string `json:"mode"`
	Eink   bool   `json:"eink"`
}

func defaultThemePreferences() ThemePreferences {
	return ThemePreferences{SkinID: defaultSkin, Mode: "system", Eink: false}
}

// ThemeService 读写外观偏好：内存一份快照 + settings 持久化。
type ThemeService struct {
	base     *core.BaseService
	settings *core.SettingsService

	mu      sync.RWMutex
	current ThemePreferences
}

// NewThemeService 只需要 SettingsService：Python 那份外壳的 storage 入参
// 是"以前写 appearance.json"留下的，M5 起落库并入 settings 后就没用过了。
func NewThemeService(settings *core.SettingsService) *ThemeService {
	return &ThemeService{
		base:     core.NewBaseService("ThemeService", "SettingsService"),
		settings: settings,
		current:  defaultThemePreferences(),
	}
}

// Start 走 bizSvc 的惯例：把加载挂在 onStart 钩子上，状态机交给 BaseService。
func (s *ThemeService) Start() error {
	s.base.SetOnStart(func() error { return s.loadSnapshot() })
	return s.base.Start()
}

func (s *ThemeService) Stop() error { return s.base.Stop() }

// loadSnapshot 读回那一格；读不到/读坏都按默认档，不阻塞启动。
// （Python 同名路径把异常吞成 raw=None，这里连"库里的值是坏的"都不该让设置页打不开。）
func (s *ThemeService) loadSnapshot() error {
	raw, ok := s.readSettings()
	if !ok {
		return nil
	}
	s.mu.Lock()
	s.current = normalizeThemePreferences(raw)
	s.mu.Unlock()
	return nil
}

// readSettings 取 themeSettingsKey 那一格；缺键或不是对象时 ok=false。
func (s *ThemeService) readSettings() (map[string]interface{}, bool) {
	if s.settings == nil {
		return nil, false
	}
	val := s.settings.Get(themeSettingsKey, "")
	if val == "" {
		return nil, false
	}
	var raw map[string]interface{}
	if err := json.Unmarshal([]byte(val), &raw); err != nil {
		return nil, false
	}
	return raw, true
}

// GetPreferences 返回当前快照（值拷贝：调用方改不动服务内部态）。
func (s *ThemeService) GetPreferences() ThemePreferences {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.current
}

// SetPreferences 合并更新：nil 的那一格保持原值（theme.save 的 payload 三格各自可缺省）。
// 落库失败只吞掉持久化，内存态照样生效——与 Python "存储未就绪时内存态仍生效"一致。
func (s *ThemeService) SetPreferences(skinID, mode *string, eink *bool) ThemePreferences {
	s.mu.Lock()
	raw := map[string]interface{}{
		"skin_id": s.current.SkinID,
		"mode":    s.current.Mode,
		"eink":    s.current.Eink,
	}
	if skinID != nil {
		raw["skin_id"] = *skinID
	}
	if mode != nil {
		raw["mode"] = *mode
	}
	if eink != nil {
		raw["eink"] = *eink
	}
	s.current = normalizeThemePreferences(raw)
	next := s.current
	s.mu.Unlock()

	if s.settings != nil {
		// settings.Set 会 json.Marshal 这个结构体 → 库里那格就是三格 JSON。
		_ = s.settings.Set(themeSettingsKey, next)
	}
	return next
}

// normalizeThemePreferences 逐格归一：表外皮肤→默认、表外明暗→system、eink 取布尔。
// 认不出来的值是"用户手滑/旧库里的历史值"，不是"报错让用户打不开设置页"。
func normalizeThemePreferences(raw map[string]interface{}) ThemePreferences {
	prefs := defaultThemePreferences()
	if skin, ok := raw["skin_id"].(string); ok && validSkins[skin] {
		prefs.SkinID = skin
	}
	if mode, ok := raw["mode"].(string); ok && validModes[mode] {
		prefs.Mode = mode
	}
	if eink, ok := raw["eink"].(bool); ok {
		prefs.Eink = eink
	}
	return prefs
}
