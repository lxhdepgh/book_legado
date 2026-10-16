package services

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/legodo-booksource/legodo_go/core"
)

// 对照物 = Python app/services/tools/update_service.py（M5，契约 §1.17）。
//
// 红线照旧：**只检查、不静默自替换**。下载/安装不在这一格（Dart 侧那颗按钮标着
// 「下载功能待实现」，协议面上也没有 download action）。
//
// 与 Python 的一处**有意**偏差：Python 的默认 API 地址是上游 Legado 仓库的
// releases/latest。那是另一个产品（Android 阅读器）的发布通道，拿它的 tag 跟本机
// 版本比，会把"别家发了新版本"显示成"本应用有更新"。Go 侧不设这个默认——
// 没配 `update.api_url` 就是**没有更新通道**，直接回一条看得见的错，不猜。

// AppVersion 是本机版本基准（更新闸门拿它比远端 tag）。
// 唯一来源是 legodo_dart/pubspec.yaml 的 version（形如 `1.0.1+2`，取加号前那段）；
// 这里的常量由 test/services_app_version_matches_pubspec_test.go 钉住，改 pubspec
// 不改这里会在 go test 里红。
const AppVersion = "1.0.1"

// settings 键名对齐 Python：update.api_url / update.ignored_version /
// update.last_check_time。Go 旧实现写的是 update.ignore_version（少个 d），
// 于是 flet 那边忽略过的版本在这里不生效——同一库两个前端各忽略各的。
const (
	apiURLSetting        = "update.api_url"
	ignoredVerSetting    = "update.ignored_version"
	lastCheckTimeSetting = "update.last_check_time"
)

// ErrNoUpdateChannel：没配更新源。回显式错误而不是"已是最新"，
// 因为后者是一次谎报（0 号原则：没有那条通道就说没有，别返回一个好看的空值）。
var ErrNoUpdateChannel = errors.New("未配置更新源：settings 里没有 update.api_url，本应用没有可用的发布通道")

// UpdateInfo 是一次可用更新的纯数据快照；键名即线上契约
// （Dart UpdateInfo.fromJson 读 version/notes/url/size/digest）。
type UpdateInfo struct {
	Version string `json:"version"`
	Notes   string `json:"notes"`
	URL     string `json:"url"`
	Size    int64  `json:"size"`
	Digest  string `json:"digest"`
}

// UpdateService 检查应用更新并记录"忽略某版本"。
type UpdateService struct {
	base     *core.BaseService
	settings *core.SettingsService
	httpC    *http.Client
	current  string

	// fetchJSON 是网络入口，测试里注入替身（全程不联网）——对齐 Python 的
	// http_get_json 构造入参，不是"顺手加的钩子"。
	fetchJSON func(ctx context.Context, url string) (map[string]interface{}, error)
}

// NewUpdateService 构造更新服务；currentVersion 空串=本机版本未登记，
// 此时任何远端版本都比本机新（Python 同形，它的注释解释了为什么不用 "0.0.0" 占位）。
func NewUpdateService(settings *core.SettingsService, httpC *http.Client, currentVersion string) *UpdateService {
	if httpC == nil {
		httpC = &http.Client{Timeout: 10 * time.Second}
	}
	s := &UpdateService{
		base:     core.NewBaseService("UpdateService", "SettingsService"),
		settings: settings,
		httpC:    httpC,
		current:  strings.TrimSpace(currentVersion),
	}
	s.fetchJSON = s.fetchJSONHTTP
	return s
}

func (s *UpdateService) Start() error { return s.base.Start() }
func (s *UpdateService) Stop() error  { return s.base.Stop() }

// CurrentVersion 回本机版本（空串=未登记，界面据此显示「未知」）。
func (s *UpdateService) CurrentVersion() string { return s.current }

// Check 查一次 releases API。
//
// 返回值三种情形，界面上是三句话：
//   - `(info, nil)`：有更新（远端比本机新且没被忽略）；
//   - `(nil, nil)`：已是最新，或该版本已被用户忽略；
//   - `(nil, err)`：没有更新通道 / 网络与解析失败——**不折算成"已是最新"**。
func (s *UpdateService) Check(ctx context.Context) (*UpdateInfo, error) {
	url := strings.TrimSpace(s.setting(apiURLSetting))
	if url == "" {
		return nil, ErrNoUpdateChannel
	}

	data, err := s.fetchJSON(ctx, url)
	s.touchLastCheck()
	if err != nil {
		return nil, fmt.Errorf("更新检查失败：%w", err)
	}

	info := parseRelease(data)
	if info == nil {
		return nil, nil
	}
	if compareVersions(info.Version, s.current) <= 0 {
		return nil, nil
	}
	if ignored := strings.TrimSpace(s.setting(ignoredVerSetting)); ignored != "" && ignored == info.Version {
		return nil, nil
	}
	return info, nil
}

// IgnoreVersion 记下用户忽略的那一版（该版本不再提示）。
func (s *UpdateService) IgnoreVersion(version string) error {
	if s.settings == nil {
		return errors.New("SettingsService 未就绪：忽略版本没处落库")
	}
	return s.settings.Set(ignoredVerSetting, strings.TrimSpace(version))
}

func (s *UpdateService) setting(key string) string {
	if s.settings == nil {
		return ""
	}
	return settingText(s.settings, key)
}

// settingText 取一格字符串设置。Go 的 SettingsService 里所有值都是**JSON 文本**
// （Set 走 json.Marshal），所以字符串值带着引号：直接当字符串用会拿到
// `"http://…"`，比远端版本时又会是 `"1.2.0"`——两边都"看着一样"地不相等。
// 这里解回原文；解不动（库里是裸写的历史值）就原样返回。
// Python 侧 core/settings.py 的 get 回的是解析后的值，语义与这里一致。
func settingText(settings *core.SettingsService, key string) string {
	if settings == nil {
		return ""
	}
	raw := settings.Get(key, "")
	var out string
	if err := json.Unmarshal([]byte(raw), &out); err == nil {
		return out
	}
	return raw
}

// touchLastCheck 无论有没有更新都记一次检查时刻（秒，浮点——Python 用 time.time()）。
func (s *UpdateService) touchLastCheck() {
	if s.settings == nil {
		return
	}
	_ = s.settings.Set(lastCheckTimeSetting, float64(time.Now().UnixMilli())/1000.0)
}

func (s *UpdateService) fetchJSONHTTP(ctx context.Context, url string) (map[string]interface{}, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := s.httpC.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	var data map[string]interface{}
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, fmt.Errorf("解析更新信息失败：%w", err)
	}
	return data, nil
}

// ---------------------------------------------------------------------------
// 解析 / 版本比较（纯函数，便于单测；照抄 Python _parse_release / _version_tuple）
// ---------------------------------------------------------------------------

var digitsRE = regexp.MustCompile(`\d+`)

// installerSuffixes 是 Python 那个元组的小写形态（比的是 name.lower()）。
var installerSuffixes = []string{".exe", ".msi", ".dmg", ".deb", ".rpm", ".appimage"}

// parseRelease 从 GitHub/Gitee release JSON 提取最新版本与首个安装包资产。
// 没有 tag_name 就不是一个 release → nil（调用方按"无更新"处理）。
func parseRelease(data map[string]interface{}) *UpdateInfo {
	tag := strings.TrimSpace(fieldString(data["tag_name"]))
	if tag == "" {
		return nil
	}
	version := strings.TrimLeft(tag, "vV")
	version = strings.TrimSpace(version)

	info := &UpdateInfo{
		Version: version,
		Notes:   firstNonEmpty(fieldString(data["body"]), fieldString(data["name"])),
	}

	assets, _ := data["assets"].([]interface{})
	for _, raw := range assets {
		asset, ok := raw.(map[string]interface{})
		if !ok {
			continue
		}
		browserURL := firstNonEmpty(fieldString(asset["browser_download_url"]), fieldString(asset["url"]))
		if browserURL == "" {
			continue
		}
		name := fieldString(asset["name"])
		lowerName := strings.ToLower(name)
		lowerURL := strings.ToLower(browserURL)
		// 校验和/说明类资产不当安装包，但它的 URL 可以留作 digest 来源。
		if strings.HasSuffix(lowerName, ".sha256") || strings.HasSuffix(lowerName, ".txt") ||
			strings.HasSuffix(lowerURL, ".sha256") {
			if info.Digest == "" {
				info.Digest = browserURL
			}
			continue
		}
		// 每遇一个可用资产就覆盖：Python 那条循环是这个形状（最后一个赢，
		// 除非中途命中安装包后缀即止）。别在这里"顺手"改成首个赢。
		info.URL = browserURL
		info.Size = fieldInt64(asset["size"])
		for _, suffix := range installerSuffixes {
			if strings.HasSuffix(lowerName, suffix) {
				return info
			}
		}
	}
	if info.URL == "" {
		// 无资产时仍提示有更新（用户可经 release 页手动下载）。
		info.URL = fieldString(data["html_url"])
	}
	return info
}

// versionParts 把 "1.2.3" / "v1.2.3-rc1" 抽成可比较的数字串切片。
// 空/无数字 → ["0"]（与 Python 的 (0,) 同形：任何远端版本都比它新）。
func versionParts(version string) []string {
	matches := digitsRE.FindAllString(version, -1)
	if len(matches) == 0 {
		return []string{"0"}
	}
	out := make([]string, 0, len(matches))
	for _, m := range matches {
		// 去前导零，否则 "1.02" 与 "1.2" 会按字符串比出大小。
		trimmed := strings.TrimLeft(m, "0")
		if trimmed == "" {
			trimmed = "0"
		}
		out = append(out, trimmed)
	}
	return out
}

// compareVersions 逐段比数字（Python 的元组序）：段数不同时短的补 0 再比，
// 与 Python `(1,0,1) > (1,0)` 的结论一致——短的那段在缺位上取 0。
func compareVersions(a, b string) int {
	pa, pb := versionParts(a), versionParts(b)
	n := len(pa)
	if len(pb) > n {
		n = len(pb)
	}
	for i := 0; i < n; i++ {
		x, y := 0, 0
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

func fieldString(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	default:
		return fmt.Sprintf("%v", t)
	}
}

func fieldInt64(v interface{}) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case int:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		return n
	default:
		return 0
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
