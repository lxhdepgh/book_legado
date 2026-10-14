# Legodo Go 后端开发文档

## 项目结构

```
legodo_go/
├── cmd/server/              # 主入口
│   └── main.go              # 服务启动/停止逻辑
├── internal/                # 内部实现（不对外暴露）
│   ├── engine/              # 规则引擎核心
│   │   ├── rule_engine.go   # 规则执行器
│   │   └── pre_executor.go  # 预执行器
│   ├── httpclient/          # HTTP 客户端
│   │   ├── client.go        # ClientSession 封装
│   │   └── response.go      # Response 包装
│   ├── models/              # 数据模型
│   │   ├── book_source.go   # BookSource 实体
│   │   └── chapter.go       # Chapter 实体
│   ├── services/            # 业务服务
│   │   ├── hub.go           # ServiceHub 生命周期
│   │   ├── register.go      # 服务注册
│   │   └── ...              # 28 个服务
│   ├── gateway/             # RPC 网关
│   │   └── gateway.go       # WebSocket + HTTP
│   └── storage/             # 存储层
│       ├── sqlite.go        # SQLite 连接
│       └── cache.go         # 文件缓存
├── pkg/                     # 公共库（可被外部引用）
│   ├── ruleparser/          # 规则解析器
│   │   └── parser.go        # CSS/JSONPath/XPath
│   └── jsruntime/           # JS 运行时
│       └── runtime.go       # goja 封装
└── tests/                   # 测试用例
```

## 技术栈对照

| Python | Go | 说明 |
|--------|----|------|
| aiohttp | net/http + golang.org/x/net | HTTP 客户端 |
| BeautifulSoup | goquery | HTML 解析 |
| QuickJS | goja | JS 引擎 |
| jsonpath-ng | tidwall/gjson | JSONPath |
| lxml/xpath | antchfx/htmlquery | XPath |
| asyncio | goroutine + channel | 并发模型 |
| SQLAlchemy | database/sql + modernc.org/sqlite | ORM/数据库 |

## 开发流程

### 1. 安装依赖

```bash
go mod tidy
```

### 2. 运行测试

```bash
go test ./... -v
```

### 3. 构建

```bash
# Windows
go build -o legodo_server.exe ./cmd/server/

# Linux/macOS
go build -o legodo_server ./cmd/server/

# Android (AAR)
gomobile bind -target=android -o legodo_engine.aar ./pkg/jsruntime
```

### 4. 运行

```bash
./legodo_server.exe
# 或
go run ./cmd/server/
```

## 核心模块实现进度

- ✅ 项目骨架
- ✅ HTTP 客户端层
- ✅ 规则解析器（CSS/JSONPath/XPath）
- ✅ JS 引擎集成（goja）
- ✅ 数据模型与存储
  - ✅ BookSource / SearchRule / ContentRule 等
  - ✅ BookItem / Chapter / ChapterContent
- ✅ 规则引擎核心
  - ✅ SearchBook（搜索书籍）
  - ✅ GetBookInfo（获取详情）
  - ✅ GetChapters（获取目录）
  - ✅ GetContent（获取正文）
- ⏳ 服务基础设施
- ⏳ RPC 接口层
- ⏳ 测试用例

## 与 Python 版差异

### HTTP 客户端

**Python**:
```python
async with aiohttp.ClientSession() as session:
    async with session.get(url) as resp:
        text = await resp.text()
```

**Go**:
```go
client, _ := httpclient.NewClient(nil)
resp, _ := client.Get(ctx, url)
text, _ := resp.BodyString()
```

### 规则解析

**Python**:
```python
soup = BeautifulSoup(html, "html.parser")
nodes = soup.select(".book-list li")
```

**Go**:
```go
doc, _ := ruleparser.ParseHTML(html)
nodes := doc.Select(".book-list li")
```

### JS 执行

**Python**:
```python
ctx = quickjs.Context()
result = ctx.eval(js_code)
```

**Go**:
```go
rt := jsruntime.NewRuntime()
result, _ := rt.Execute(jsCode, &jsruntime.Context{...})
```

## 性能对比（预期）

| 指标 | Python | Go | 提升 |
|------|--------|----|------|
| 冷启动 | ~2s | ~0.5s | 4x |
| 内存占用 | ~120MB | ~35MB | 3.4x ↓ |
| 并发扫描 | 32 源/秒 | 100+ 源/秒 | 3x |
| APK 体积 | ~120MB | ~35MB | 3.4x ↓ |

## 下一步

1. 实现数据模型（BookSource/Chapter）
2. 实现 SQLite 存储层
3. 实现 RuleEngineService
4. 编写单元测试
5. 与 Dart 前端联调
