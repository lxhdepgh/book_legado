# 快速启动指南

## 前置条件

- Go 1.21+ 已安装
- Flutter SDK（用于 Dart 前端）

## 方式一：源码直接运行（推荐开发使用）

### Windows (Git Bash)

```bash
cd legodo_dart/backend_go
./start_go.sh
```

### Windows (CMD)

```cmd
cd legodo_dart\backend_go
start_go.bat
```

### Linux/macOS

```bash
cd legodo_dart/backend_go
chmod +x start_go.sh
./start_go.sh
```

### 直接命令

```bash
cd legodo_dart/backend_go
go run ./cmd/server/
```

---

## 方式二：编译后运行（推荐生产使用）

### 编译

```bash
cd legodo_go
go build -o legodo_server.exe ./cmd/server/
```

### 运行

```bash
./legodo_server.exe
```

---

## 验证服务

### 1. 健康检查

```bash
curl http://127.0.0.1:8080/health
# 应返回: OK
```

### 2. WebSocket 测试

使用 `wscat` 工具：

```bash
# 安装 wscat
npm install -g wscat

# 连接
wscat -c ws://127.0.0.1:8080/rpc

# 发送测试消息
> {"v":1,"action":"check_source","payload":{"source":{"bookSourceUrl":"https://example.com"}}}

# 应收到响应
< {"success":true,"data":{"url":"https://example.com","name":"","status":"ok","response":200},"id":1}
```

### 3. Dart 测试脚本

```bash
cd ..  # 返回 legodo_dart 目录
dart test_go_connection.dart
```

---

## 预期输出

启动成功后应看到：

```
启动服务: StorageService
启动服务: SettingsService
启动服务: EventBus
启动服务: EngineHub
启动服务: RuleEngineService
启动服务: SourceService
启动服务: CheckService
启动服务: ReadingService
RPC 网关监听 :8080
Legodo Go 后端已启动，监听 :8080
```

---

## 常见问题

### Q: 提示 "未找到 Go 命令"

**解决**：安装 Go
- Windows: https://go.dev/dl/
- 下载 `go1.21.x.windows-amd64.msi`
- 安装后重启终端

### Q: 端口 8080 被占用

**解决**：修改端口

编辑 `cmd/server/main.go`：

```go
go func() {
    if err := gw.Start(":9090"); err != nil { // 改为其他端口
        log.Printf("RPC 网关错误: %v", err)
    }
}()
```

### Q: 依赖下载失败

**解决**：配置 Go 代理

```bash
# Windows
go env -w GOPROXY=https://goproxy.cn,direct

# Linux/macOS
export GOPROXY=https://goproxy.cn,direct
```

然后重新运行：

```bash
go mod tidy
go run ./cmd/server/
```

---

## 性能对比

| 启动方式 | 首次启动 | 后续启动 | 适用场景 |
|---------|---------|---------|---------|
| `go run` | ~5s (含编译) | ~2s | 开发调试 |
| 编译后运行 | ~5s (编译) + 0.5s | 0.5s | 生产部署 |

---

## 开发工作流

1. **修改代码** → 保存
2. **Ctrl+C** 停止服务
3. **重新运行** `go run ./cmd/server/`
4. **测试** 新功能

Go 的编译速度极快，通常 <2s 即可完成增量编译。
