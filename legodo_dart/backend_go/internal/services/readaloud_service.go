package services

import (
	"github.com/legodo-booksource/legodo_go/core"
)

// ReadAloudProfile represents a TTS voice profile.
type ReadAloudProfile struct {
	ID         int64  `json:"id"`
	Name       string `json:"name"`
	URL        string `json:"url"`
	Headers    string `json:"headers"`
	EngineType string `json:"engineType"`
}

// ReadAloudState tracks current playback state.
type ReadAloudState int

const (
	StateStopped ReadAloudState = iota
	StatePlaying
	StatePaused
)

// ReadAloudService manages TTS read-aloud profiles and playback state.
type ReadAloudService struct {
	base    *core.BaseService
	storage *core.StorageService
	bus     *core.EventBus
	state   ReadAloudState
	profile *ReadAloudProfile
}

// NewReadAloudService creates a new read-aloud service.
func NewReadAloudService(storage *core.StorageService, bus *core.EventBus) *ReadAloudService {
	return &ReadAloudService{
		base:    core.NewBaseService("ReadAloudService", "StorageService"),
		storage: storage,
		bus:     bus,
		state:   StateStopped,
	}
}

// Start initializes profiles from DB.
func (s *ReadAloudService) Start() error { return s.base.Start() }
func (s *ReadAloudService) BaseStop() error {
	s.state = StateStopped
	s.bus.Publish(&core.ReadAloudStateEvent{State: string(StateStopped)})
	return s.base.Stop()
}

// Speak starts TTS speaking for text.
func (s *ReadAloudService) Speak(text string) error {
	s.state = StatePlaying
	s.bus.Publish(&core.ReadAloudStateEvent{State: string(StatePlaying)})
	return nil
}

// Pause pauses playback.
func (s *ReadAloudService) Pause() error {
	s.state = StatePaused
	s.bus.Publish(&core.ReadAloudStateEvent{State: string(StatePaused)})
	return nil
}

// Resume resumes paused playback.
func (s *ReadAloudService) Resume() error {
	s.state = StatePlaying
	s.bus.Publish(&core.ReadAloudStateEvent{State: string(StatePlaying)})
	return nil
}

// AddProfile saves a new TTS profile.
func (s *ReadAloudService) AddProfile(name, url, headers, engineType string) (int64, error) {
	result, err := s.storage.Execute(
		`INSERT INTO tts_profiles (name, url, headers, engine_type) VALUES (?, ?, ?, ?)`,
		name, url, headers, engineType,
	)
	if err != nil {
		return 0, err
	}
	id, _ := result.LastInsertId()
	s.profile = &ReadAloudProfile{ID: id, Name: name, URL: url, Headers: headers, EngineType: engineType}
	return id, nil
}

// RemoveProfile deletes a TTS profile by ID.
func (s *ReadAloudService) RemoveProfile(id int64) error {
	_, err := s.storage.Execute("DELETE FROM tts_profiles WHERE id=?", id)
	return err
}

// ListProfiles returns all TTS profiles.
func (s *ReadAloudService) ListProfiles() ([]*ReadAloudProfile, error) {
	rows, err := s.storage.Query("SELECT id, name, url, headers, engine_type FROM tts_profiles")
	if err != nil || rows == nil {
		return nil, err
	}
	defer rows.Close()

	var profiles []*ReadAloudProfile
	for rows.Next() {
		var p ReadAloudProfile
		if scanErr := rows.Scan(&p.ID, &p.Name, &p.URL, &p.Headers, &p.EngineType); scanErr != nil {
			continue
		}
		profiles = append(profiles, &p)
	}
	return profiles, rows.Err()
}

// SetActiveProfile sets the currently active TTS profile.
func (s *ReadAloudService) SetActiveProfile(id int64) {
	row := s.storage.QueryRow("SELECT id, name, url, headers, engine_type FROM tts_profiles WHERE id=?", id)
	var p ReadAloudProfile
	if err := row.Scan(&p.ID, &p.Name, &p.URL, &p.Headers, &p.EngineType); err == nil {
		s.profile = &p
	}
}

// GetState returns current playback state.
func (s *ReadAloudService) GetState() ReadAloudState {
	return s.state
}
