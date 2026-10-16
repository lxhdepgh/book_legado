package services

import (
	"errors"

	"github.com/legodo-booksource/legodo_go/core"
	"github.com/skip2/go-qrcode"
)

// 对照物 = Python app/services/tools/qr_code_service.py（M5，契约 §5）。
//
// Python 那一份：qrcode+Pillow 生成 PNG 字节流，opencv 的 QRCodeDetector 识图。
// Go 这一份用纯 Go 的 skip2/go-qrcode（MIT）替掉生成侧；识别侧没有对应的纯 Go
// 解码器，于是**显式回"未实现"**而不是返回空串装成"没识别到"——后者在界面上
// 与"这张图里确实没有二维码"长得一模一样（0 号原则）。

// qrImageSize 是生成的 PNG 边长（像素）。Python 用 box_size=10/border=2，
// 33 模块的版本约 350px；这里取一个同为整数模块尺寸的档，够用且不会被界面缩放糊掉。
const qrImageSize = 330

// ErrQRDecodeUnimplemented 说清"没做"而不是"没识别到"。
var ErrQRDecodeUnimplemented = errors.New(
	"二维码识别未实现：Go 引擎没有图像解码器（Python 侧用 opencv 的 QRCodeDetector），" +
		"要扫码请用系统相机或第三方工具")

// QRService 生成二维码 PNG；识别未实现。
type QRService struct {
	base *core.BaseService
}

func NewQRService() *QRService {
	return &QRService{base: core.NewBaseService("QRService")}
}

func (s *QRService) Start() error { return s.base.Start() }
func (s *QRService) Stop() error  { return s.base.Stop() }

// GeneratePNG 把文本编成二维码 PNG 字节流（纠错档 M，对齐 Python 的 ERROR_CORRECT_M）。
func (s *QRService) GeneratePNG(text string) ([]byte, error) {
	if text == "" {
		return nil, errors.New("二维码内容不能为空")
	}
	code, err := qrcode.New(text, qrcode.Medium)
	if err != nil {
		return nil, err
	}
	return code.PNG(qrImageSize)
}

// Decode 未实现——调用方拿到的是错误，不是一个空结果。
func (s *QRService) Decode(imageData string) (string, error) {
	return "", ErrQRDecodeUnimplemented
}
