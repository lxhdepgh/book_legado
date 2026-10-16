package services

// QRService 的护栏：生成侧钉"这真的是一张 PNG"，识别侧钉"没实现要说成没实现"。
//
// 旧桩两发都是假成功：generate 把入参原样回显（界面 base64Decode 一段文本，
// 崩在渲染上），decode 回空串（界面读成"这张图里没有二维码"，而真相是没人解码）。

import (
	"bytes"
	"strings"
	"testing"
)

var pngMagic = []byte{0x89, 'P', 'N', 'G', 0x0d, 0x0a, 0x1a, 0x0a}

func TestQRGenerateReturnsRealPNG(t *testing.T) {
	svc := NewQRService()
	const text = `{"bookSourceUrl":"http://loopback.invalid.test"}`

	first, err := svc.GeneratePNG(text)
	if err != nil {
		t.Fatalf("生成失败：%v", err)
	}
	if !bytes.HasPrefix(first, pngMagic) {
		t.Fatalf("回的不是 PNG（前 8 字节不符魔数），长度 %d", len(first))
	}
	if len(first) < 200 {
		t.Fatalf("这张 PNG 小得不像有内容：%d 字节", len(first))
	}
	second, err := svc.GeneratePNG(text)
	if err != nil || !bytes.Equal(first, second) {
		t.Fatalf("同一段文本两发不一致（二维码该是纯函数）：%v", err)
	}
	// 换个内容必须换图：否则等于"生成"根本没看入参。
	other, err := svc.GeneratePNG(text + "，另一段")
	if err != nil {
		t.Fatalf("第二发失败：%v", err)
	}
	if bytes.Equal(first, other) {
		t.Fatalf("不同文本生成了同一张图，编码器没吃入参")
	}
}

func TestQRGenerateRejectsEmptyText(t *testing.T) {
	if _, err := NewQRService().GeneratePNG(""); err == nil {
		t.Fatalf("空内容必须报错（Python 同形：二维码内容不能为空）")
	}
}

func TestQRDecodeSaysUnimplementedNotNotFound(t *testing.T) {
	got, err := NewQRService().Decode("iVBORw0KGgoAAAANSUhEUg==")
	if err == nil {
		t.Fatalf("识别侧未实现，却回了一次成功：%q", got)
	}
	if !strings.Contains(err.Error(), "未实现") {
		t.Fatalf("错误文本要说\"未实现\"，不能像\"没扫到\"：%q", err.Error())
	}
	if got != "" {
		t.Fatalf("失败时不该带内容：%q", got)
	}
}
