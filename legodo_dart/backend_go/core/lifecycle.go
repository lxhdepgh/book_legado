package core

import (
	"sync"
	"time"
)

// ServiceState represents the state of a service in its lifecycle.
type ServiceState string

const (
	StateRegistered ServiceState = "REGISTERED"
	StateInit       ServiceState = "INIT"
	StateStarting   ServiceState = "STARTING"
	StateRunning    ServiceState = "RUNNING"
	StateSuspended  ServiceState = "SUSPENDED"
	StateStopping   ServiceState = "STOPPING"
	StateDisposed   ServiceState = "DISPOSED"
)

// ServiceStatus is an immutable snapshot of a service's status.
type ServiceStatus struct {
	ServiceID string
	State     ServiceState
	LastError string
	StartedAt float64 // Unix seconds
	UpdatedAt float64
	Progress  float64
}

// NewServiceStatus creates a fresh status snapshot.
func NewServiceStatus(serviceID string) *ServiceStatus {
	now := float64(time.Now().UnixMilli()) / 1000.0
	return &ServiceStatus{
		ServiceID: serviceID,
		State:     StateInit,
		StartedAt: now,
		UpdatedAt: now,
	}
}

// Transition moves the status to a new state.
func (s *ServiceStatus) Transition(state ServiceState, err error) {
	s.State = state
	s.UpdatedAt = float64(time.Now().UnixMilli()) / 1000.0
	if err != nil {
		s.LastError = err.Error()
	}
}

// Service defines the interface for all services in the hub.
type Service interface {
	// Name returns the unique service name.
	Name() string

	// Dependencies lists the names of services this depends on.
	Dependencies() []string

	// Start transitions from INIT -> STARTING -> RUNNING or SUSPENDED.
	// Implemented by calling the onStart hook.
	Start() error

	// Stop transitions from RUNNING -> STOPPING -> DISPOSED.
	// Implemented by calling the onStop hook.
	Stop() error

	// Status returns current status snapshot.
	Status() ServiceStatus

	// SetBus assigns the event bus (called by Hub after registration).
	SetBus(bus *EventBus)

	// SetHub assigns the service hub (called by Hub after registration).
	SetHub(hub *ServiceHub)
}

// BaseService provides common lifecycle machinery.
// Concrete services embed *BaseService and override onStart/onStop via setters.
type BaseService struct {
	name        string
	dependencies []string
	status      ServiceStatus
	mu          sync.RWMutex
	hub         *ServiceHub
	bus         *EventBus
	onStart     func() error
	onStop      func() error
	suspendFn   func()
	resumeFn    func() error
}

// NewBaseService creates a BaseService.
func NewBaseService(name string, deps ...string) *BaseService {
	return &BaseService{
		name:        name,
		dependencies: deps,
		status:      *NewServiceStatus(name),
	}
}

// SetOnStart sets the start hook function.
func (b *BaseService) SetOnStart(fn func() error) { b.onStart = fn }

// SetOnStop sets the stop hook function.
func (b *BaseService) SetOnStop(fn func() error) { b.onStop = fn }

// SetSuspend sets the suspend hook.
func (b *BaseService) SetSuspend(fn func()) { b.suspendFn = fn }

// SetResume sets the resume hook.
func (b *BaseService) SetResume(fn func() error) { b.resumeFn = fn }

func (b *BaseService) Name() string { return b.name }

func (b *BaseService) Dependencies() []string { return b.dependencies }

func (b *BaseService) Status() ServiceStatus {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.status
}

func (b *BaseService) SetBus(bus *EventBus) { b.bus = bus }
func (b *BaseService) SetHub(hub *ServiceHub) { b.hub = hub }

func (b *BaseService) setStatus(state ServiceState, err error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.status.Transition(state, err)
}

// Start executes: REGISTERED -> INIT -> STARTING -> (onStart) -> RUNNING or SUSPENDED.
func (b *BaseService) Start() error {
	b.setStatus(StateInit, nil)
	b.setStatus(StateStarting, nil)
	if b.onStart != nil {
		err := b.onStart()
		if err != nil {
			b.setStatus(StateSuspended, err)
			return err
		}
	}
	b.setStatus(StateRunning, nil)
	return nil
}

// Stop executes: RUNNING -> STOPPING -> DISPOSED.
func (b *BaseService) Stop() error {
	b.setStatus(StateStopping, nil)
	if b.onStop != nil {
		if err := b.onStop(); err != nil {
			b.setStatus(StateDisposed, err)
			return err
		}
	}
	b.setStatus(StateDisposed, nil)
	return nil
}

// Suspend pauses the service.
func (b *BaseService) Suspend() {
	if b.suspendFn != nil {
		b.suspendFn()
	}
	b.setStatus(StateSuspended, nil)
}

// Resume restarts a suspended service.
func (b *BaseService) Resume() error {
	if b.resumeFn != nil {
		return b.resumeFn()
	}
	return b.Start()
}
