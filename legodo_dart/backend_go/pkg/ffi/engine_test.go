package main

// P6：FFI 面出参形状的核对测试。断言口径是 Dart 解析器吃的那些键
// （lib/core/rpc/ffi_engine_adapter.dart 的 chapterRows → ChapterRow），
// 不是 Go 自己觉得顺手的样子。

import (
	"sort"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/models"
)

// assertExactKeys 键集两个方向都钉：多一个键漏一个键都要红。
func assertExactKeys(t *testing.T, where string, got map[string]interface{}, want ...string) {
	t.Helper()
	gotKeys := make([]string, 0, len(got))
	for key := range got {
		gotKeys = append(gotKeys, key)
	}
	sort.Strings(gotKeys)
	sorted := append([]string(nil), want...)
	sort.Strings(sorted)
	if len(gotKeys) != len(sorted) {
		t.Errorf("%s 键集不符：got %v want %v", where, gotKeys, sorted)
		return
	}
	for i := range gotKeys {
		if gotKeys[i] != sorted[i] {
			t.Errorf("%s 键集不符：got %v want %v", where, gotKeys, sorted)
			return
		}
	}
}

// reader.toc 的每一行：Dart chapterRows 逐键读 name/url/index/isVolume/isVip/
// isPay/tag/wordCount —— 少一个键那一格就静默落默认值。
func TestChapterRowShape(t *testing.T) {
	row := chapterRow(&models.Chapter{
		BookURL: "https://src.test/g3/book/1.html",
		Index:   3,
		Title:   "第4章 测试章",
		URL:     "https://src.test/g3/ch/4.html",
		IsVip:   true,
		WordCount: 4321,
	})

	assertExactKeys(t, "chapterRow", row,
		"name", "url", "index", "isVolume", "isVip", "isPay", "tag", "wordCount")
	if row["name"] != "第4章 测试章" || row["url"] != "https://src.test/g3/ch/4.html" {
		t.Errorf("name/url 不符：%v / %v", row["name"], row["url"])
	}
	if row["index"] != 3 || row["isVip"] != true || row["isPay"] != false {
		t.Errorf("index/isVip/isPay 不符：%v %v %v", row["index"], row["isVip"], row["isPay"])
	}
	// 卷标与字数：Dart 侧这两格是字符串（_s），给数字会让类型转静默落空。
	if row["isVolume"] != false || row["tag"] != "" {
		t.Errorf("isVolume/tag 不符：%v / %v", row["isVolume"], row["tag"])
	}
	if wordCount, ok := row["wordCount"].(string); !ok || wordCount != "4321" {
		t.Errorf("wordCount 应是字符串 \"4321\"：%T %v", row["wordCount"], row["wordCount"])
	}

	// 真语料形状（缓存目录行 / Legado 规则）：卷、字数原文优先——"3240字"这类
	// 原文是 Legado 语料里的显示口径，结构化字数只是没有原文时的回读。
	live := chapterRow(&models.Chapter{
		Title: "第二卷", URL: "https://src.test/g3/vol2", IsVolume: true,
		Tag: "更新时间：2024-01-02", WordCountText: "3240字", WordCount: 3240,
	})
	if live["isVolume"] != true || live["tag"] != "更新时间：2024-01-02" {
		t.Errorf("isVolume/tag 应原样上送：%v / %v", live["isVolume"], live["tag"])
	}
	if live["wordCount"] != "3240字" {
		t.Errorf("有字数原文时应优先用原文，得到 %v", live["wordCount"])
	}
}

// shelve/resume/save_progress 的缺省都是 true：gb 会把"没传"读成 false，
// 那是协议面的静默翻转 —— 这条钉住 gbDefault 的两个方向。
func TestGbDefaultTwoDirections(t *testing.T) {
	if !gbDefault(map[string]interface{}{}, "shelve", true) {
		t.Error("缺省应落 def=true")
	}
	if gbDefault(map[string]interface{}{"shelve": false}, "shelve", true) {
		t.Error("显式 false 不该被缺省盖掉")
	}
	if !gbDefault(map[string]interface{}{"shelve": true}, "shelve", false) {
		t.Error("显式 true 不该被缺省盖掉")
	}
	if gbDefault(map[string]interface{}{"shelve": "true"}, "shelve", true) == false {
		t.Error("非布尔值应落 def（字符串 \"true\" 不算 true）")
	}
}
