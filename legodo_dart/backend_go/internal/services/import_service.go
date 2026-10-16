package services

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ImportService 是 `app/services/sources/online_import_service.py` 的对应物：
// 按 kind 分发的导入门面，**自己一律不落库**（红线同 Python：本服务不持有落库逻辑）。
//
// 三条从门面来的规矩：
//   - `book_source` 是唯一有落点的类型；其余 kind 只登记说明（count=0、placeholder=true），
//     界面据此写"未落地（占位）"，不许写成"导入了 0 条"。
//   - 表里没有的 kind 是 errors（未知资源类型），不是 placeholder——拼错类型的用户要听见声音。
//   - 拉取/解析失败记成一条 error 后照原形状返回，不抛穿：门面那一层的调用方是同步 FFI，
//     抛穿只会把"哪一条源坏了"这句人话丢在通道里。
type ImportService struct {
	sourceSvc *SourceService
	httpC     *http.Client
	// fetchText 是测试注入点；nil 时走 httpC（30s 总超时，对齐 Python 的 ClientTimeout(total=30)）。
	fetchText func(url string) (string, error)
}

// NewImportService 造门面；hc 为 nil 时用带 30s 超时的默认客户端。
func NewImportService(sourceSvc *SourceService, hc *http.Client) *ImportService {
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	return &ImportService{sourceSvc: sourceSvc, httpC: hc}
}

// ImportKindBookSource 是当前唯一有落点的 kind（Python 的 _KIND_READY）。
const ImportKindBookSource = "book_source"

// importUTF8BOM 写成三个字节转义而不是 \uFEFF：源码里放一个真实 BOM 会被 Go 编译器
// 判成"文件中段有 BOM"直接拒绝编译。
const importUTF8BOM = "\xEF\xBB\xBF"

// pendingKindText：尚无落点的类型 → 给人看的说明。文案逐字取自 Python 的 _KIND_PENDING，
// 那份措辞是指着"去哪一页加"写的，换成占位符式的"暂未支持"会让人不知道下一步做什么。
var pendingKindText = map[string]string{
	"txt_rules":     "TXT 目录规则目前没有独立的存储落点，暂不支持导入",
	"replace_rules": "替换规则请在「配置 → 替换规则」页里添加，导入通道未接",
	"dict_rules":    "词典规则请在「配置 → 词典」页里添加，导入通道未接",
	"rss":           "RSS 源请在「配置 → 更多工具」的 RSS 页里添加，导入通道未接",
	"tts":           "TTS 朗读在阅读器内设置，导入通道未接",
	"theme":         "主题在「配置 → 外观」页里选择，导入通道未接",
}

// ImportResult 是一次导入的结果快照（契约 §7：kind/count/errors/warnings/placeholder）。
type ImportResult struct {
	Kind        string
	Count       int
	Errors      []string
	Warnings    []string
	Placeholder bool
}

func newImportResult(kind string) *ImportResult {
	return &ImportResult{Kind: kind, Errors: []string{}, Warnings: []string{}}
}

// ImportText 导入本地/调用方已读入的文本。mode 见 SourceService.ImportSourcesJSON。
func (s *ImportService) ImportText(text, kind, mode string) *ImportResult {
	// Windows 上存的 JSON 常带 UTF-8 BOM，而它会让整份解析失败——先剥掉再判形状，
	// 否则"这个文件不能导入：JSON 解析失败"会是用户看到的第一句假话。
	text = strings.TrimLeft(text, importUTF8BOM)
	if note, pending := pendingKindText[kind]; pending {
		result := newImportResult(kind)
		result.Placeholder = true
		result.Warnings = append(result.Warnings, note)
		return result
	}
	if kind != ImportKindBookSource {
		result := newImportResult(kind)
		result.Errors = append(result.Errors, fmt.Sprintf("未知资源类型：%q", kind))
		return result
	}

	result := newImportResult(kind)
	if err := preflightSourceText(text); err != nil {
		result.Errors = append(result.Errors, err.Error())
		return result
	}
	if s.sourceSvc == nil {
		result.Errors = append(result.Errors, "SourceService 未就绪，本次导入未落库")
		return result
	}
	count, errs := s.sourceSvc.ImportSourcesJSON(text, mode)
	result.Count = count
	result.Errors = append(result.Errors, errs...)
	return result
}

// ImportFromURL 先拉文本再按 kind 分发；拉取失败只记一条 error（不猜内容、不落库）。
func (s *ImportService) ImportFromURL(url, kind, mode string) *ImportResult {
	text, err := s.fetch(url)
	if err != nil {
		result := newImportResult(kind)
		result.Errors = append(result.Errors, fmt.Sprintf("拉取失败：%v", err))
		return result
	}
	return s.ImportText(text, kind, mode)
}

func (s *ImportService) fetch(url string) (string, error) {
	if s.fetchText != nil {
		return s.fetchText(url)
	}
	if s.httpC == nil {
		return "", fmt.Errorf("没有可用的 HTTP 客户端")
	}
	resp, err := s.httpC.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	// 2MiB 上限：书源 JSON 分享文件到这个尺寸已经异常大，多出来的钱不该由一次导入付。
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2*1024*1024))
	if err != nil {
		return "", err
	}
	return string(body), nil
}

// preflightSourceText 只做"这份文本能不能当书源读"的形状检查，错误文案取自 Python 门面
// （`_parse_source_text`）：落库层那份是同一条拒绝的另一种说法，界面上先出的是门面这一份。
func preflightSourceText(text string) error {
	var data interface{}
	if err := json.Unmarshal([]byte(text), &data); err != nil {
		return fmt.Errorf("JSON 解析失败：%v", err)
	}
	if _, isObject := data.(map[string]interface{}); isObject {
		return nil
	}
	items, isArray := data.([]interface{})
	if !isArray {
		return fmt.Errorf("书源 JSON 顶层应为数组或对象")
	}
	for _, item := range items {
		if _, ok := item.(map[string]interface{}); ok {
			return nil
		}
	}
	return fmt.Errorf("书源 JSON 中没有有效对象")
}
