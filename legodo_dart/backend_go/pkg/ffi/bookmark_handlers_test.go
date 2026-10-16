package main

// bookmark 面的契约测试。断言口径照 **Dart 适配器走线的那一侧**：
// payload 键来自 lib/core/rpc/ffi_engine_adapter.dart 发出的
// book_url / chapter_index / offset / summary / bookmark_id（与 Python
// app/gateway/methods.py 的 reader.bookmark_* 一致）；出参形状是
// BookmarkRow.fromJson 吃的 snake_case 行（created_at 为 epoch 秒）、
// add 回裸 int、remove 回裸 bool。
//
// 参数键读错不会报错：INSERT 照样成功，只是 book_url 落空串、列表永远为空 ——
// 所以这里用 Dart 的原样键走 route()，并且把整行读回来核。

import (
	"encoding/json"
	"testing"

	"github.com/legodo-booksource/legodo_go/internal/services"
)

const bookmarkTestBookURL = "https://src.test/g3/book/1.html"

func bookmarkRoute(t *testing.T, hub *services.ServiceHub, action string, p map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(route(hub, action, p))
	if err != nil {
		t.Fatalf("marshal envelope: %v", err)
	}
	var env map[string]interface{}
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("unmarshal envelope: %v", err)
	}
	if ok, _ := env["ok"].(bool); !ok {
		t.Fatalf("%s 失败：%v", action, env["error"])
	}
	return env
}

func TestBookmarkHandlersMatchDartContract(t *testing.T) {
	t.Setenv("LEGADO_BOOKSOURCE_DATA_DIR", t.TempDir())
	hub := services.NewServiceHub()
	if err := hub.RegisterInfrastructure(); err != nil {
		t.Fatalf("register infrastructure: %v", err)
	}
	if err := hub.RegisterBusinessServices(); err != nil {
		t.Fatalf("register business services: %v", err)
	}
	// 没走 StartAll：stopOrder 是 StartAll 才建的，StopAll 在这里是空转；
	// 不显式关库 Windows 上 TempDir 的 legado.db 会删不掉。
	t.Cleanup(func() {
		if s := hub.StorageService(); s != nil {
			_ = s.Close()
		}
	})

	// add：Dart 发 book_url/chapter_index/offset/summary，回**裸 int** id。
	add := bookmarkRoute(t, hub, "reader_bookmark_add", map[string]interface{}{
		"book_url": bookmarkTestBookURL, "chapter_index": float64(2),
		"offset": 0.35, "summary": "第3章 深潜",
	})
	if id, ok := add["data"].(float64); !ok || id != 1 {
		t.Fatalf("add 应回裸 int id 1：%T %v", add["data"], add["data"])
	}

	// 同一位置再加一条：Python 的 bookmarks 表没有 UNIQUE，重复添加必须成功
	// （canonical DDL 若带了 UNIQUE，这里会红）。
	add2 := bookmarkRoute(t, hub, "reader_bookmark_add", map[string]interface{}{
		"book_url": bookmarkTestBookURL, "chapter_index": float64(2),
		"offset": 0.35, "summary": "第3章 深潜（重复点）",
	})
	if id, ok := add2["data"].(float64); !ok || id != 2 {
		t.Fatalf("重复位置应照加、id=2：%T %v", add2["data"], add2["data"])
	}

	// list：行必须逐键是 BookmarkRow.fromJson 吃的那六个键。
	list := bookmarkRoute(t, hub, "reader_bookmarks", map[string]interface{}{"book_url": bookmarkTestBookURL})
	rows, ok := list["data"].([]interface{})
	if !ok || len(rows) != 2 {
		t.Fatalf("list 应回两行：%T %v", list["data"], list["data"])
	}
	first, _ := rows[0].(map[string]interface{})
	assertExactKeys(t, "reader_bookmarks 行", first,
		"bookmark_id", "book_url", "chapter_index", "offset", "summary", "created_at")
	if first["book_url"] != bookmarkTestBookURL || first["chapter_index"] != float64(2) || first["offset"] != 0.35 {
		t.Errorf("行值与落库不符：%v", first)
	}
	if first["summary"] != "第3章 深潜" {
		t.Errorf("summary 不符：%v", first["summary"])
	}
	// created_at 是 **epoch 秒**（Python time.time() / Dart BookmarkRow 模型）；
	// 毫秒会是 ~1.7e12，1e11 上限就是秒/毫秒的分水岭。
	created, ok := first["created_at"].(float64)
	if !ok || created < 1e9 || created > 1e11 {
		t.Errorf("created_at 应是 epoch 秒（~1.7e9），got %T %v", first["created_at"], first["created_at"])
	}

	// 别的书回空数组（不是 null）。
	other := bookmarkRoute(t, hub, "reader_bookmarks", map[string]interface{}{"book_url": "https://src.test/g3/book/other.html"})
	if otherRows, ok := other["data"].([]interface{}); !ok || len(otherRows) != 0 {
		t.Errorf("无书签必须回空数组：%T %v", other["data"], other["data"])
	}

	// remove：Dart 只发 book_url + bookmark_id，回**裸 bool**；删过的再删回 false
	// （Python 侧同样无错，只是 removed=false）。
	removed := bookmarkRoute(t, hub, "reader_bookmark_remove", map[string]interface{}{
		"book_url": bookmarkTestBookURL, "bookmark_id": float64(1),
	})
	if removed["data"] != true {
		t.Fatalf("首次删除应回 true：%v", removed["data"])
	}
	again := bookmarkRoute(t, hub, "reader_bookmark_remove", map[string]interface{}{
		"book_url": bookmarkTestBookURL, "bookmark_id": float64(1),
	})
	if again["data"] != false {
		t.Fatalf("重复删除应回 false（可能已被另一侧删过）：%v", again["data"])
	}

	// 剩下的那条还在，且 id=2。
	after := bookmarkRoute(t, hub, "reader_bookmarks", map[string]interface{}{"book_url": bookmarkTestBookURL})
	rowsAfter, _ := after["data"].([]interface{})
	if len(rowsAfter) != 1 {
		t.Fatalf("删一条后应剩一条：%v", rowsAfter)
	}
	if row, _ := rowsAfter[0].(map[string]interface{}); row["bookmark_id"] != float64(2) {
		t.Errorf("剩下的应是 id=2：%v", row)
	}
}
