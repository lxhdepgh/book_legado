package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"context"
	"encoding/json"
	"sync"
	"unsafe"

	"github.com/legodo-booksource/legodo_go/internal/engine"
	"github.com/legodo-booksource/legodo_go/internal/httpclient"
	"github.com/legodo-booksource/legodo_go/internal/models"
	"github.com/legodo-booksource/legodo_go/pkg/jsruntime"
)

// main 函数（c-shared 模式必需，但不会被调用）
func main() {}

// LegodoEngine FFI 引擎实例（单例）
var (
	engineInstance *engine.RuleEngine
	engineMu       sync.Mutex
	initOnce       sync.Once
)

// initEngine 初始化引擎（仅调用一次）
func initEngine() {
	initOnce.Do(func() {
		client := httpclient.NewClient()
		jsRuntime := jsruntime.NewRuntime()
		engineInstance = engine.NewRuleEngine(client, jsRuntime)
	})
}

// exportToCString 将 Go 字符串导出为 C 字符串（调用者负责 free）
func exportToCString(s string) *C.char {
	return C.CString(s)
}

// freeCString 释放 C 字符串
//export FreeString
func FreeString(s *C.char) {
	C.free(unsafe.Pointer(s))
}

// Initialize 初始化引擎（幂等）
//export Initialize
func Initialize() {
	initEngine()
}

// SearchBooks 搜索书籍
// 参数：sourceJSON - 书源 JSON 字符串，keyword - 搜索关键词
// 返回：书籍列表 JSON 字符串（需调用 FreeString 释放）
//export SearchBooks
func SearchBooks(sourceJSON *C.char, keyword *C.char) *C.char {
	initEngine()

	sourceStr := C.GoString(sourceJSON)
	keywordStr := C.GoString(keyword)

	var source models.BookSource
	if err := json.Unmarshal([]byte(sourceStr), &source); err != nil {
		return exportToCString(`{"error":"invalid source JSON: ` + err.Error() + `"}`)
	}

	ctx := context.Background()
	books, err := engineInstance.SearchBook(ctx, &source, keywordStr)
	if err != nil {
		return exportToCString(`{"error":"` + err.Error() + `"}`)
	}

	result := map[string]interface{}{
		"success": true,
		"data":    books,
	}
	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// GetBookInfo 获取书籍详情
// 参数：sourceJSON - 书源 JSON 字符串，bookURL - 书籍 URL
// 返回：书籍详情 JSON 字符串
//export GetBookInfo
func GetBookInfo(sourceJSON *C.char, bookURL *C.char) *C.char {
	initEngine()

	sourceStr := C.GoString(sourceJSON)
	urlStr := C.GoString(bookURL)

	var source models.BookSource
	if err := json.Unmarshal([]byte(sourceStr), &source); err != nil {
		return exportToCString(`{"error":"invalid source JSON: ` + err.Error() + `"}`)
	}

	ctx := context.Background()
	book, err := engineInstance.GetBookInfo(ctx, &source, urlStr)
	if err != nil {
		return exportToCString(`{"error":"` + err.Error() + `"}`)
	}

	result := map[string]interface{}{
		"success": true,
		"data":    book,
	}
	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// GetChapters 获取章节目录
// 参数：sourceJSON - 书源 JSON 字符串，bookURL - 书籍 URL
// 返回：章节列表 JSON 字符串
//export GetChapters
func GetChapters(sourceJSON *C.char, bookURL *C.char) *C.char {
	initEngine()

	sourceStr := C.GoString(sourceJSON)
	urlStr := C.GoString(bookURL)

	var source models.BookSource
	if err := json.Unmarshal([]byte(sourceStr), &source); err != nil {
		return exportToCString(`{"error":"invalid source JSON: ` + err.Error() + `"}`)
	}

	ctx := context.Background()
	chapters, err := engineInstance.GetChapters(ctx, &source, urlStr)
	if err != nil {
		return exportToCString(`{"error":"` + err.Error() + `"}`)
	}

	result := map[string]interface{}{
		"success": true,
		"data":    chapters,
	}
	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// GetContent 获取章节内容
// 参数：sourceJSON - 书源 JSON 字符串，chapterJSON - 章节 JSON（包含 title, url）
// 返回：章节内容 JSON 字符串
//export GetContent
func GetContent(sourceJSON *C.char, chapterJSON *C.char) *C.char {
	initEngine()

	sourceStr := C.GoString(sourceJSON)
	chapterStr := C.GoString(chapterJSON)

	var source models.BookSource
	if err := json.Unmarshal([]byte(sourceStr), &source); err != nil {
		return exportToCString(`{"error":"invalid source JSON: ` + err.Error() + `"}`)
	}

	var chapter models.Chapter
	if err := json.Unmarshal([]byte(chapterStr), &chapter); err != nil {
		return exportToCString(`{"error":"invalid chapter JSON: ` + err.Error() + `"}`)
	}

	ctx := context.Background()
	content, err := engineInstance.GetContent(ctx, &source, &chapter)
	if err != nil {
		return exportToCString(`{"error":"` + err.Error() + `"}`)
	}

	result := map[string]interface{}{
		"success": true,
		"data":    content,
	}
	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// CheckSource 检查书源可用性
// 参数：sourceJSON - 书源 JSON 字符串
// 返回：检查结果 JSON 字符串
//export CheckSource
func CheckSource(sourceJSON *C.char) *C.char {
	initEngine()

	sourceStr := C.GoString(sourceJSON)

	var source models.BookSource
	if err := json.Unmarshal([]byte(sourceStr), &source); err != nil {
		return exportToCString(`{"error":"invalid source JSON: ` + err.Error() + `"}`)
	}

	// 简单检查：尝试访问书源的搜索 URL
	ctx := context.Background()
	_, err := engineInstance.SearchBook(ctx, &source, "test")
	
	valid := err == nil
	errorMsg := ""
	if err != nil {
		errorMsg = err.Error()
	}

	result := map[string]interface{}{
		"success": true,
		"valid":   valid,
		"error":   errorMsg,
	}

	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// ExecuteJS 执行 JavaScript 代码
// 参数：jsCode - JS 代码，contextJSON - 上下文 JSON（包含 baseUrl, source, page, variables）
// 返回：执行结果 JSON 字符串
//export ExecuteJS
func ExecuteJS(jsCode *C.char, contextJSON *C.char) *C.char {
	initEngine()

	jsStr := C.GoString(jsCode)
	ctxStr := C.GoString(contextJSON)

	var ctx jsruntime.Context
	if err := json.Unmarshal([]byte(ctxStr), &ctx); err != nil {
		return exportToCString(`{"error":"invalid context JSON: ` + err.Error() + `"}`)
	}

	jsRuntime := jsruntime.NewRuntime()
	
	// 执行 JS
	val, err := jsRuntime.Execute(jsStr, &ctx)
	if err != nil {
		return exportToCString(`{"error":"JS execution failed: ` + err.Error() + `"}`)
	}

	result := map[string]interface{}{
		"success": true,
		"data":    val,
	}
	data, _ := json.Marshal(result)
	return exportToCString(string(data))
}

// Cleanup 清理引擎资源
//export Cleanup
func Cleanup() {
	engineMu.Lock()
	defer engineMu.Unlock()
	// TODO: 实现引擎的资源清理逻辑
	engineInstance = nil
}
