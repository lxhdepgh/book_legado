package services

// UpdateService 的护栏：全程离线（网络入口用构造期就备好的注入点，与 Python 的
// http_get_json 同一形态），钉的是"三种回话不许互相冒充"与**落库键的字面名**。
//
// 旧实现的两处谎报都在这里有对应的一条：
//   - 网络/配置失败被折成"已是最新"（界面读起来像查过了）；
//   - 忽略版本写进 update.ignore_version（少个 d），与 flet 侧各忽略各的。

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"

	"github.com/legodo-booksource/legodo_go/core"
)

func newUpdateTestStore(t *testing.T) (*core.SettingsService, *core.StorageService) {
	t.Helper()
	storage, err := core.NewStorageService(t.TempDir())
	if err != nil {
		t.Fatalf("建存储失败：%v", err)
	}
	t.Cleanup(func() { _ = storage.Close() })
	settings, err := core.NewSettingsService(storage, core.NewEventBus())
	if err != nil {
		t.Fatalf("建 settings 服务失败：%v", err)
	}
	return settings, storage
}

// fetchStub 替身：记下发没发过、发去了哪个地址，回预置的载荷或错误。
type fetchStub struct {
	calls int
	urls  []string
	data  map[string]interface{}
	err   error
}

func (f *fetchStub) do(_ context.Context, url string) (map[string]interface{}, error) {
	f.calls++
	f.urls = append(f.urls, url)
	if f.err != nil {
		return nil, f.err
	}
	return f.data, nil
}

func newStubbedUpdateService(t *testing.T, apiURL string, current string, stub *fetchStub) *UpdateService {
	t.Helper()
	settings, _ := newUpdateTestStore(t)
	svc := NewUpdateService(settings, nil, current)
	svc.fetchJSON = stub.do
	if apiURL != "" {
		if err := settings.Set("update.api_url", apiURL); err != nil {
			t.Fatalf("预置 api_url 失败：%v", err)
		}
	}
	return svc
}

const testReleaseURL = "http://updates.invalid.test/latest.json"

var updateTestRelease = map[string]interface{}{
	"tag_name": "v2.3.0",
	"name":     "标题当说明用",
	"html_url": "https://release.invalid.test/page",
	"assets": []interface{}{
		map[string]interface{}{"name": "checksums.txt", "browser_download_url": "https://d.invalid.test/checksums.txt"},
		map[string]interface{}{"name": "app.exe.sha256", "browser_download_url": "https://d.invalid.test/app.exe.sha256"},
		map[string]interface{}{"name": "legacy.zip", "browser_download_url": "https://d.invalid.test/legacy.zip", "size": float64(10)},
		map[string]interface{}{"name": "Legodo-Setup.exe", "browser_download_url": "https://d.invalid.test/Legodo-Setup.exe", "size": float64(456)},
	},
}

// TestUpdateCheckWithoutChannelNeitherFetchesNorClaimsLatest 没配更新源：
// 一次请求都不许发出去，回话必须是错，不许是"已是最新"那个空值。
func TestUpdateCheckWithoutChannelNeitherFetchesNorClaimsLatest(t *testing.T) {
	stub := &fetchStub{}
	svc := newStubbedUpdateService(t, "", "1.0.1", stub)

	info, err := svc.Check(context.Background())
	if err == nil {
		t.Fatalf("没有更新源时 Check 必须报错，得 info=%+v", info)
	}
	if info != nil {
		t.Fatalf("报错的同时不该给出更新：%+v", info)
	}
	if stub.calls != 0 {
		t.Fatalf("没配更新源却发了 %d 次请求：%v", stub.calls, stub.urls)
	}
	if !strings.Contains(err.Error(), "update.api_url") {
		t.Fatalf("错误文本要说清缺的是哪一格，得：%q", err.Error())
	}
}

// TestUpdateCheckFailureIsNotReportedAsLatest 远端挂了：界面那句"已是最新版本"
// 只能来自"真的比过了"，不能来自一次失败的请求。
func TestUpdateCheckFailureIsNotReportedAsLatest(t *testing.T) {
	stub := &fetchStub{err: errors.New("HTTP 500")}
	svc := newStubbedUpdateService(t, testReleaseURL, "1.0.1", stub)

	_, err := svc.Check(context.Background())
	if err == nil {
		t.Fatalf("拉取失败必须回错（否则界面读成\"已是最新版本\"）")
	}
	if !strings.Contains(err.Error(), "HTTP 500") {
		t.Fatalf("错误文本丢了原因：%q", err.Error())
	}
}

// TestUpdateCheckRecordsLastCheckTimeEvenOnFailure 查一次就记一次时刻（Python 同形：
// 失败路径也 touch），否则"上次检查时间"永远停在最后一次成功上。
func TestUpdateCheckRecordsLastCheckTimeEvenOnFailure(t *testing.T) {
	settings, storage := newUpdateTestStore(t)
	svc := NewUpdateService(settings, nil, "1.0.1")
	svc.fetchJSON = (&fetchStub{err: errors.New("连不上")}).do
	if err := settings.Set("update.api_url", testReleaseURL); err != nil {
		t.Fatalf("预置 api_url 失败：%v", err)
	}
	if _, err := svc.Check(context.Background()); err == nil {
		t.Fatalf("这条替身必须失败")
	}
	var value string
	if err := storage.QueryRow(
		`SELECT value FROM settings WHERE key='update.last_check_time'`).Scan(&value); err != nil {
		t.Fatalf("失败路径没记 last_check_time：%v", err)
	}
	var ts float64
	if err := json.Unmarshal([]byte(value), &ts); err != nil || ts <= 0 {
		t.Fatalf("last_check_time 不是正数时刻：%q", value)
	}
}

// TestUpdateCheckParsesReleaseAndPrefersInstaller 解析 release：版本去 v 前缀、
// 说明缺 body 用 name、安装包按后缀命中即止、校验资产只能当 digest 不能当下载地址。
func TestUpdateCheckParsesReleaseAndPrefersInstaller(t *testing.T) {
	stub := &fetchStub{data: updateTestRelease}
	svc := newStubbedUpdateService(t, testReleaseURL, "1.0.1", stub)

	info, err := svc.Check(context.Background())
	if err != nil {
		t.Fatalf("检查失败：%v", err)
	}
	if info == nil {
		t.Fatalf("远端 2.3.0 比本机 1.0.1 新，应给出更新")
	}
	if info.Version != "2.3.0" {
		t.Fatalf("版本号没去掉 v 前缀：%q", info.Version)
	}
	if info.Notes != "标题当说明用" {
		t.Fatalf("没有 body 时该用 name 当说明：%q", info.Notes)
	}
	if info.URL != "https://d.invalid.test/Legodo-Setup.exe" {
		t.Fatalf("安装包资产没选中：%q", info.URL)
	}
	if info.Size != 456 {
		t.Fatalf("size 跟错了资产：%d", info.Size)
	}
	if info.Digest != "https://d.invalid.test/checksums.txt" {
		t.Fatalf("digest 该取第一个校验/说明资产：%q", info.Digest)
	}
	if stub.calls != 1 || stub.urls[0] != testReleaseURL {
		t.Fatalf("请求发偏了：%v", stub.urls)
	}
}

// TestUpdateCheckNoUpdateWhenNotNewer 远端不比本机新 → (nil, nil)：这才是"已是最新"。
func TestUpdateCheckNoUpdateWhenNotNewer(t *testing.T) {
	for _, tc := range []struct{ local, remote string }{
		{"2.3.0", "v2.3.0"},
		{"2.3.1", "v2.3.0"},
		{"2.3.0", "2.3"},      // 短的在缺位上取 0，不比新的长
		{"1.0.1+2", "v1.0.1"}, // 构建号不参与比较
	} {
		stub := &fetchStub{data: map[string]interface{}{"tag_name": tc.remote}}
		svc := newStubbedUpdateService(t, testReleaseURL, tc.local, stub)
		info, err := svc.Check(context.Background())
		if err != nil {
			t.Fatalf("%s vs %s 不该报错：%v", tc.local, tc.remote, err)
		}
		if info != nil {
			t.Fatalf("本机 %s 时远端 %s 不该算更新，得 %+v", tc.local, tc.remote, info)
		}
	}
}

// TestUpdateCheckRespectsIgnoredVersion 用户忽略过这一版 → 静默（nil,nil），
// 但请求是真的发了（忽略不是"不检查"）。
func TestUpdateCheckRespectsIgnoredVersion(t *testing.T) {
	settings, _ := newUpdateTestStore(t)
	stub := &fetchStub{data: updateTestRelease}
	svc := NewUpdateService(settings, nil, "1.0.1")
	svc.fetchJSON = stub.do
	if err := settings.Set("update.api_url", testReleaseURL); err != nil {
		t.Fatalf("预置 api_url 失败：%v", err)
	}
	if err := settings.Set("update.ignored_version", "2.3.0"); err != nil {
		t.Fatalf("预置忽略版本失败：%v", err)
	}
	info, err := svc.Check(context.Background())
	if err != nil || info != nil {
		t.Fatalf("已忽略的版本又弹出来了：info=%+v err=%v", info, err)
	}
	if stub.calls != 1 {
		t.Fatalf("忽略≠不检查，请求发了一次才对：%d", stub.calls)
	}
}

// TestIgnoreVersionUsesSharedKey 忽略版本落在 "update.ignored_version"（与 flet 侧
// 同一个键），旧实现写的是 update.ignore_version（少个 d）——两边各忽略各的。
func TestIgnoreVersionUsesSharedKey(t *testing.T) {
	settings, storage := newUpdateTestStore(t)
	svc := NewUpdateService(settings, nil, "1.0.1")
	if err := svc.IgnoreVersion("2.3.0"); err != nil {
		t.Fatalf("忽略失败：%v", err)
	}
	var value string
	if err := storage.QueryRow(
		`SELECT value FROM settings WHERE key='update.ignored_version'`).Scan(&value); err != nil {
		t.Fatalf("按字面键读不到忽略版本：%v（写去的键不是这一格）", err)
	}
	var got string
	if err := json.Unmarshal([]byte(value), &got); err != nil || got != "2.3.0" {
		t.Fatalf("忽略版本内容不对：%q", value)
	}
	var legacy string
	if err := storage.QueryRow(
		`SELECT value FROM settings WHERE key='update.ignore_version'`).Scan(&legacy); err == nil {
		t.Fatalf("还在往旧键 update.ignore_version 写：%q", legacy)
	}
}

// TestUpdateInfoWireKeys 回包键名就是 Dart UpdateInfo.fromJson 读的那五个。
func TestUpdateInfoWireKeys(t *testing.T) {
	raw, err := json.Marshal(UpdateInfo{Version: "2.3.0", Notes: "n", URL: "u", Size: 456, Digest: "d"})
	if err != nil {
		t.Fatalf("序列化失败：%v", err)
	}
	var got map[string]interface{}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("回包不是对象：%s", raw)
	}
	want := []string{"digest", "notes", "size", "url", "version"}
	if len(got) != len(want) {
		t.Fatalf("键数不对：%s", raw)
	}
	for _, k := range want {
		if _, ok := got[k]; !ok {
			t.Fatalf("缺键 %s：%s", k, raw)
		}
	}
}

// TestAppVersionMatchesPubspec 版本基准不许漂：唯一来源是 legodo_dart/pubspec.yaml。
func TestAppVersionMatchesPubspec(t *testing.T) {
	body, err := os.ReadFile("../../../pubspec.yaml")
	if err != nil {
		t.Fatalf("读 pubspec.yaml 失败：%v", err)
	}
	m := regexp.MustCompile(`(?m)^version:\s*([0-9][0-9A-Za-z.\-_]*)`).FindSubmatch(body)
	if m == nil {
		t.Fatalf("pubspec.yaml 里没找到 version 行")
	}
	if got := string(m[1]); got != AppVersion {
		t.Fatalf("AppVersion=%q 与 pubspec 的 version=%q 不一致（取加号前那段）", AppVersion, got)
	}
}
