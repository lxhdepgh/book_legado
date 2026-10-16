package core

import (
	"context"
	"log"
	"sync"
)

// AppEvent is the base interface for all events in the system.
type AppEvent interface {
	// EventType returns a string identifying the event type (used for routing).
	EventType() string
}

// EventHandler is a function that processes an event.
type EventHandler func(event AppEvent)

// EventBus implements typed publish-subscribe with concurrent dispatch via goroutines.
// Aligns with Python core/bus.py EventBus.
//
// Key differences from Python:
// - Go replaces daemon-thread asyncio loop with goroutine dispatch
// - Handlers run concurrently, no ordering guarantee between handlers
// - Exceptions (panics) in handlers are recovered and logged
type EventBus struct {
	mu          sync.RWMutex
	handlers    map[string][]*handlerEntry // eventType -> handler list with IDs
	allHandlers []*handlerEntry            // catch-all (see SubscribeAll)
	nextID      int64                      // unique handler ID counter
	ctx         context.Context
	cancel      context.CancelFunc
}

type handlerEntry struct {
	id      int64
	handler EventHandler
}

// NewEventBus creates a new event bus.
func NewEventBus() *EventBus {
	ctx, cancel := context.WithCancel(context.Background())
	return &EventBus{
		handlers: make(map[string][]*handlerEntry),
		ctx:      ctx,
		cancel:   cancel,
	}
}

// Subscribe registers a handler for events of a given type.
// Returns an unsubscribe function.
func (eb *EventBus) Subscribe(eventType string, handler EventHandler) func() {
	eb.mu.Lock()
	entry := &handlerEntry{id: eb.nextID, handler: handler}
	eb.nextID++
	eb.handlers[eventType] = append(eb.handlers[eventType], entry)
	eb.mu.Unlock()

	return func() {
		eb.UnsubscribeByID(eventType, entry.id)
	}
}

// SubscribeAll registers a catch-all handler for every event type.
// Catch-all handlers run **synchronously** inside Publish (before the
// concurrent typed dispatch), so their observation order equals publish
// order — the FFI event ring depends on that.
// Returns an unsubscribe function.
func (eb *EventBus) SubscribeAll(handler EventHandler) func() {
	eb.mu.Lock()
	entry := &handlerEntry{id: eb.nextID, handler: handler}
	eb.nextID++
	eb.allHandlers = append(eb.allHandlers, entry)
	eb.mu.Unlock()

	return func() {
		eb.mu.Lock()
		defer eb.mu.Unlock()
		for i, h := range eb.allHandlers {
			if h.id == entry.id {
				eb.allHandlers = append(eb.allHandlers[:i], eb.allHandlers[i+1:]...)
				break
			}
		}
	}
}

// UnsubscribeByID removes a specific handler by its ID.
func (eb *EventBus) UnsubscribeByID(eventType string, id int64) {
	eb.mu.Lock()
	defer eb.mu.Unlock()
	handlers := eb.handlers[eventType]
	for i, h := range handlers {
		if h.id == id {
			eb.handlers[eventType] = append(handlers[:i], handlers[i+1:]...)
			break
		}
	}
}

// Publish sends an event to all registered handlers of that type.
// Catch-all handlers run synchronously (in publish order first); typed
// handlers are then called concurrently via goroutines.
func (eb *EventBus) Publish(event AppEvent) {
	eb.mu.RLock()
	handlers := eb.handlers[event.EventType()]
	all := eb.allHandlers
	eb.mu.RUnlock()

	for _, entry := range all {
		eb.safeDispatch(entry.handler, event)
	}
	for _, entry := range handlers {
		go eb.safeDispatch(entry.handler, event)
	}
}

// safeDispatch wraps handler execution in recover().
func (eb *EventBus) safeDispatch(handler EventHandler, event AppEvent) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[EventBus] handler panic for %s: %v", event.EventType(), r)
		}
	}()

	select {
	case <-eb.ctx.Done():
		return
	default:
		handler(event)
	}
}

// Close shuts down the event bus.
func (eb *EventBus) Close() {
	eb.cancel()
}
