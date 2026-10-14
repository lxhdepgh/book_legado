package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/legodo-booksource/legodo_go/internal/gateway"
	"github.com/legodo-booksource/legodo_go/internal/services"
)

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 构建服务枢纽
	hub := services.NewServiceHub()

	// 注册基础设施
	if err := hub.RegisterInfrastructure(); err != nil {
		log.Fatalf("注册基础设施失败: %v", err)
	}

	// 注册业务服务
	if err := hub.RegisterBusinessServices(); err != nil {
		log.Fatalf("注册业务服务失败: %v", err)
	}

	// 启动所有服务
	if err := hub.StartAll(ctx); err != nil {
		log.Fatalf("启动服务失败: %v", err)
	}

	// 启动 RPC 网关
	gw := gateway.NewGateway(hub)
	go func() {
		if err := gw.Start(":8080"); err != nil {
			log.Printf("RPC 网关错误: %v", err)
		}
	}()

	fmt.Println("Legodo Go 后端已启动，监听 :8080")

	// 等待中断信号
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	fmt.Println("\n正在关闭服务...")
	cancel()

	// 优雅停止
	if err := hub.StopAll(); err != nil {
		log.Printf("停止服务出错: %v", err)
	}

	gw.Stop()
	fmt.Println("服务已停止")
}
