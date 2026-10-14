package core

import (
	"context"
	"fmt"
	"log"
	"sync"
)

// ServiceHub is the main process controller: registers services,
// resolves dependency topologies, starts/stops in order, aggregates status.
type ServiceHub struct {
	mu       sync.RWMutex
	services map[string]Service // name -> service instance
	order    []string           // registration order (for deterministic topo sort)
}

// NewServiceHub creates a new service hub.
func NewServiceHub() *ServiceHub {
	return &ServiceHub{
		services: make(map[string]Service),
		order:    make([]string, 0),
	}
}

// Register adds a service to the hub. Services must be registered before StartAll/StopAll.
func (h *ServiceHub) Register(svc Service) error {
	h.mu.Lock()
	defer h.mu.Unlock()

	name := svc.Name()
	if _, exists := h.services[name]; exists {
		return fmt.Errorf("service %s already registered", name)
	}

	h.services[name] = svc
	h.order = append(h.order, name)

	// Notify service of hub reference
	svc.SetHub(h)
	return nil
}

// Get retrieves a service by name.
func (h *ServiceHub) Get(name string) (Service, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	svc, ok := h.services[name]
	return svc, ok
}

// All returns all registered services.
func (h *ServiceHub) All() []Service {
	h.mu.RLock()
	defer h.mu.RUnlock()
	result := make([]Service, 0, len(h.services))
	for _, name := range h.order {
		if svc, ok := h.services[name]; ok {
			result = append(result, svc)
		}
	}
	return result
}

// StartAll starts all services in dependency order (dependencies first).
// A single service failure does not block others; failed services enter SUSPENDED state.
func (h *ServiceHub) StartAll(ctx context.Context) error {
	order := h.topoOrder()
	for _, name := range order {
		svc := h.services[name]
		log.Printf("[ServiceHub] starting service: %s", name)
		if err := svc.Start(); err != nil {
			log.Printf("[ServiceHub] service %s start failed: %v", name, err)
		}
		_ = ctx
	}
	return nil
}

// StopAll stops all services in reverse dependency order (consumers first).
func (h *ServiceHub) StopAll() error {
	order := h.topoOrder()
	for i := len(order) - 1; i >= 0; i-- {
		name := order[i]
		svc := h.services[name]
		log.Printf("[ServiceHub] stopping service: %s", name)
		if err := svc.Stop(); err != nil {
			log.Printf("[ServiceHub] service %s stop error: %v", name, err)
		}
	}
	return nil
}

// StatusReport collects status snapshots from all services.
func (h *ServiceHub) StatusReport() []ServiceStatus {
	h.mu.RLock()
	defer h.mu.RUnlock()
	report := make([]ServiceStatus, 0, len(h.services))
	for _, svc := range h.services {
		report = append(report, svc.Status())
	}
	return report
}

// Has checks if a service is registered.
func (h *ServiceHub) Has(name string) bool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	_, ok := h.services[name]
	return ok
}

// topoOrder returns services ordered by dependency (Kahn's algorithm).
// Dependencies start first; ties broken by registration order.
func (h *ServiceHub) topoOrder() []string {
	h.mu.RLock()
	defer h.mu.RUnlock()

	indeg := make(map[string]int)
	edges := make(map[string][]string)

	for _, name := range h.order {
		svc := h.services[name]
		if svc == nil {
			continue
		}
		deps := svc.Dependencies()
		indeg[name] = len(deps)
		for _, dep := range deps {
			if _, exists := h.services[dep]; exists {
				edges[dep] = append(edges[dep], name)
			}
		}
	}

	var ready []string
	for _, name := range h.order {
		if indeg[name] == 0 {
			ready = append(ready, name)
		}
	}

	var order []string
	for len(ready) > 0 {
		name := ready[0]
		ready = ready[1:]
		order = append(order, name)

		for _, nxt := range edges[name] {
			indeg[nxt]--
			if indeg[nxt] == 0 {
				ready = append(ready, nxt)
			}
		}
	}

	if len(order) != len(h.services) {
		log.Printf("[ServiceHub] cycle detected in service dependencies, using registration order")
		return h.order
	}

	return order
}
