# Legodo Go Backend

Legado 书源规则引擎的 Go 实现，作为 `legodo_dart` Flutter 项目的后端服务。

## 目录结构

```
legodo_dart/
├── lib/                    # Flutter Dart 代码
├── backend_go/             # Go 后端服务（本目录）
│   ├── cmd/server/         # 主入口
│   ├── internal/           # 内部实现
│   │   ├── engine/         # 规则引擎核心
│   │   ├── httpclient/     # HTTP 客户端
│   │   ├── models/         # 数据模型
│   │   ├── services/       # 业务服务
│   │   ├── gateway/        # RPC 网关
│   │   └── storage/        # SQLite 存储
│   ├── pkg/                # 公共库
│   │   ├── ruleparser/     # CSS/JSONPath/XPath 解析器
│   │   └── jsruntime/      # goja JS 引擎封装
│   └── tests/              # 测试用例
└── android/                # Android 构建配置
```

## 技术栈

- **JS 引擎**: goja (纯 Go，ES6 支持)
- **HTML 解析**: goquery (CSS 选择器) + htmlquery (XPath)
- **JSONPath**: tidwall/gjson
- **HTTP**: net/http + golang.org/x/net/proxy
- **数据库**: modernc.org/sqlite
- **RPC**: gorilla/websocket

## 开发

```bash
# 安装依赖
go mod tidy

# 运行测试
go test ./...

# 构建
go build -o legodo_server cmd/server/main.go
```

## 与 Python 版对照

| Python 模块 | Go 实现 | 说明 |
|------------|---------|------|
| aiohttp | internal/httpclient | 连接池/代理/CookieJar |
| BeautifulSoup | pkg/ruleparser (goquery) | CSS 选择器 |
| QuickJS | pkg/jsruntime (goja) | JS 执行 |
| app/services/engine/* | internal/engine/* | 规则引擎核心 |
| app/core/hub.py | internal/services/hub.go | 服务生命周期 |

## 许可证

MIT
