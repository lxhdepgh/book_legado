package services

import (
	"context"
	"sync"
)

// Event 事件接口
type Event interface {
	Name() string
}

// EventHandler 事件处理器
type EventHandler func(ctx context.Context, event Event) error

// EventBus 事件总线（对齐 Python core/bus.py）
type EventBus struct {
	mu         sync.RWMutex
	handlers   map[string][]EventHandler
	ctx        context.Context
	cancelFunc context.CancelFunc
}

// NewEventBus 创建事件总线
func NewEventBus() *EventBus {
	ctx, cancel := context.WithCancel(context.Background())
	return &EventBus{
		handlers:   make(map[string][]EventHandler),
		ctx:        ctx,
		cancelFunc: cancel,
	}
}

// Subscribe 订阅事件
func (eb *EventBus) Subscribe(eventName string, handler EventHandler) {
	eb.mu.Lock()
	defer eb.mu.Unlock()

	eb.handlers[eventName] = append(eb.handlers[eventName], handler)
}

// Unsubscribe 取消订阅（移除所有该名称的处理器）
func (eb *EventBus) Unsubscribe(eventName string) {
	eb.mu.Lock()
	defer eb.mu.Unlock()

	delete(eb.handlers, eventName)
}

// Publish 发布事件（同步调用所有处理器）
func (eb *EventBus) Publish(ctx context.Context, event Event) error {
	eb.mu.RLock()
	handlers, ok := eb.handlers[event.Name()]
	eb.mu.RUnlock()

	if !ok {
		return nil // 无订阅者
	}

	for _, handler := range handlers {
		select {
		case <-eb.ctx.Done():
			return eb.ctx.Err()
		case <-ctx.Done():
			return ctx.Err()
		default:
			if err := handler(ctx, event); err != nil {
				return err
			}
		}
	}

	return nil
}

// Close 关闭事件总线
func (eb *EventBus) Close() {
	eb.cancelFunc()
}
