package gateway

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"

	"github.com/tidwall/gjson"
)

const (
	MaxBodyBytes    = 1024 * 1024 // 1 MB
	ProtocolVersion = 1
)

// ErrorCode represents a RPC error code.
type ErrorCode string

const (
	CodeUnavailable   ErrorCode = "unavailable"
	CodeTimeout       ErrorCode = "timeout"
	CodeCancelled     ErrorCode = "cancelled"
	CodeBadRequest    ErrorCode = "bad_request"
	CodeEngine        ErrorCode = "engine"
	CodeSchema        ErrorCode = "schema"
)

// RequestEnvelope is the incoming RPC request frame.
// Shape: {"v":1,"id":"c-17","action":"source.list","params":{"keys":["..."]}}
type RequestEnvelope struct {
	Version int                    `json:"v"`
	ID      string                 `json:"id"`
	Action  string                 `json:"action"`
	Params  map[string]interface{} `json:"params,omitempty"`
}

// ResponseEnvelope is the outgoing RPC response frame.
// Shape: {"v":1,"id":"c-17","ok":true/false,"data":...,"error":null|"msg","code":null|"err"}
type ResponseEnvelope struct {
	Version int             `json:"v"`
	ID      string          `json:"id"`
	OK      bool            `json:"ok"`
	Data    interface{}     `json:"data,omitempty"`
	Error   string          `json:"error,omitempty"`
	Code    ErrorCode       `json:"code,omitempty"`
}

// OKResponse creates a success envelope.
func OKResponse(requestID string, data interface{}) *ResponseEnvelope {
	return &ResponseEnvelope{
		Version: ProtocolVersion,
		ID:      requestID,
		OK:      true,
		Data:    data,
	}
}

// ErrorResponse creates an error envelope.
func ErrorResponse(requestID string, code ErrorCode, message string) *ResponseEnvelope {
	return &ResponseEnvelope{
		Version: ProtocolVersion,
		ID:      requestID,
		OK:      false,
		Error:   message,
		Code:    code,
	}
}

// Digest computes a short SHA-256 hash of a payload for integrity checks.
// Aligns with Python gateway/protocol.py digest().
func Digest(payload interface{}) string {
	data, err := toJSON(payload)
	if err != nil {
		return ""
	}
	hash := sha256.Sum256(data)
	return hex.EncodeToString(hash[:])[:12]
}

// VerifyDigest checks if a payload matches a known digest.
func VerifyDigest(payload interface{}, digest string) bool {
	actual := Digest(payload)
	return actual == digest
}

// toJSON converts a value to JSON bytes using stable encoding.
func toJSON(v interface{}) ([]byte, error) {
	switch val := v.(type) {
	case string:
		return []byte(val), nil
	case []byte:
		return val, nil
	default:
		// Use gjson-style sorted encoding for determinism
		s, _ := sortJSON(val)
		return []byte(s), nil
	}
}

// sortJSON produces a deterministic JSON representation by sorting object keys.
func sortJSON(v interface{}) (string, error) {
	switch val := v.(type) {
	case map[string]interface{}:
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			s, _ := sortJSON(val[k])
			parts = append(parts, fmt.Sprintf(`"%s":%s`, k, s))
		}
		return "{" + join(parts, ",") + "}", nil
	case []interface{}:
		parts := make([]string, 0, len(val))
		for _, item := range val {
			s, _ := sortJSON(item)
			parts = append(parts, s)
		}
		return "[" + join(parts, ",") + "]", nil
	default:
		return fmt.Sprintf("%v", val), nil
	}
}

func join(parts []string, sep string) string {
	result := ""
	for i, p := range parts {
		if i > 0 {
			result += sep
		}
		result += p
	}
	return result
}

// ParseRequest attempts to parse a raw JSON string into a RequestEnvelope.
func ParseRequest(raw string) (*RequestEnvelope, error) {
	if len(raw) > MaxBodyBytes {
		return nil, fmt.Errorf("request body exceeds %d bytes", MaxBodyBytes)
	}

	result := gjson.Parse(raw)
	if !result.Exists() {
		return nil, fmt.Errorf("invalid JSON")
	}

	ver := result.Get("v").Int()
	id := result.Get("id").String()
	action := result.Get("action").String()

	var params map[string]interface{}
	paramsArr := result.Get("params").Map()
	for k, v := range paramsArr {
		params[k] = v.Value()
	}

	return &RequestEnvelope{
		Version: int(ver),
		ID:      id,
		Action:  action,
		Params:  params,
	}, nil
}
