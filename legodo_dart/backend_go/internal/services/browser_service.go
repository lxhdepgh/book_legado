package services

import (
	"sync"

	"github.com/legodo-booksource/legodo_go/core"
)

// BrowserTab represents an active web page session.
type BrowserTab struct {
	URL       string `json:"url"`
	Title     string `json:"title"`
	Content   string `json:"content"`
	SessionID string `json:"sessionId"`
}

// BrowserService manages browser sessions for WebView content loading.
type BrowserService struct {
	base    *core.BaseService
	httpC   interface{} // placeholder for HTTP client abstraction
	mu      sync.RWMutex
	sessions map[string]*BrowserTab
	activeID string
}

// NewBrowserService creates a new browser service.
func NewBrowserService() *BrowserService {
	return &BrowserService{
		base:     core.NewBaseService("BrowserService"),
		sessions: make(map[string]*BrowserTab),
	}
}

// Start initializes.
func (s *BrowserService) Start() error { return s.base.Start() }
func (s *BrowserService) Stop() error  { return s.base.Stop() }

// OpenPage opens a new tab and navigates to URL.
func (s *BrowserService) OpenPage(url string) (*BrowserTab, error) {
	tab := &BrowserTab{
		URL:       url,
		SessionID: generateSessionID(),
	}
	s.mu.Lock()
	s.sessions[tab.SessionID] = tab
	s.activeID = tab.SessionID
	s.mu.Unlock()
	return tab, nil
}

// Navigate changes the URL of the active tab.
func (s *BrowserService) Navigate(sessionID string, url string) (*BrowserTab, error) {
	s.mu.RLock()
	tab, ok := s.sessions[sessionID]
	s.mu.RUnlock()
	if !ok {
		return nil, nil
	}
	tab.URL = url
	s.mu.Lock()
	s.activeID = sessionID
	s.mu.Unlock()
	return tab, nil
}

// GetContent returns the last fetched content for a tab.
func (s *BrowserService) GetContent(sessionID string) string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if tab, ok := s.sessions[sessionID]; ok {
		return tab.Content
	}
	return ""
}

// Close closes a specific tab.
func (s *BrowserService) Close(sessionID string) error {
	s.mu.Lock()
	delete(s.sessions, sessionID)
	if s.activeID == sessionID {
		var next string
		for k := range s.sessions {
			next = k
			break
		}
		s.activeID = next
	}
	s.mu.Unlock()
	return nil
}

// SetContent stores response content for a tab (called by reverse JS bridge).
func (s *BrowserService) SetContent(sessionID, content string) {
	s.mu.Lock()
	if tab, ok := s.sessions[sessionID]; ok {
		tab.Content = content
	}
	s.mu.Unlock()
}

// ActiveID returns the current active session ID.
func (s *BrowserService) ActiveID() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.activeID
}

// Status returns basic browser status.
func (s *BrowserService) Status() map[string]interface{} {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return map[string]interface{}{
		"activeID":    s.activeID,
		"totalTabs":   len(s.sessions),
		"running":     true,
	}
}

var _idCounter int

func generateSessionID() string {
	_idCounter++
	return "browser-" + string(rune('0'+_idCounter%10))
}
