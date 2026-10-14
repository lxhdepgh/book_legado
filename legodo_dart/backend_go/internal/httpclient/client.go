package httpclient

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// ClientSession manages an HTTP client session with cookie jar, proxy support,
// connection pooling, and charset encoding detection.
type ClientSession struct {
	client         *http.Client
	baseCookies    map[string]string // host -> encoded cookie string
	mu             sync.RWMutex
	defaultHeaders map[string]string
}

// NewClient creates a new HTTP client session with pooling and cookie jar.
func NewClient() *ClientSession {
	transport := &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 10,
		IdleConnTimeout:     90 * time.Second,
	}

	return &ClientSession{
		client: &http.Client{
			Transport: transport,
			Timeout:   30 * time.Second,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				if len(via) >= 10 {
					return fmt.Errorf("too many redirects")
				}
				return nil
			},
		},
		baseCookies:    make(map[string]string),
		defaultHeaders: map[string]string{
			"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
		},
	}
}

// SetTimeout configures the request timeout.
func (c *ClientSession) SetTimeout(d time.Duration) {
	c.client.Timeout = d
}

// SetHeader adds a default header for all requests.
func (c *ClientSession) SetHeader(key, value string) {
	c.defaultHeaders[key] = value
}

// RemoveHeader removes a default header.
func (c *ClientSession) RemoveHeader(key string) {
	delete(c.defaultHeaders, key)
}

// SetCookieJar sets cookies for a specific host as `key=value` pairs.
func (c *ClientSession) SetCookieJar(host string, cookies map[string]string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.baseCookies[host] = encodeCookieMap(cookies)
}

// SetSingleCookie sets a single cookie for a host.
func (c *ClientSession) SetSingleCookie(host, name, value string) {
	c.mu.RLock()
	existing := decodeCookies(c.baseCookies[host])
	c.mu.RUnlock()
	existing[name] = value
	c.mu.Lock()
	c.baseCookies[host] = encodeCookieMap(existing)
	c.mu.Unlock()
}

// GetCookies returns cookies for a host.
func (c *ClientSession) GetCookies(host string) map[string]string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return decodeCookies(c.baseCookies[host])
}

// Close releases all resources.
func (c *ClientSession) Close() {
	c.client.CloseIdleConnections()
}

// Do performs an HTTP request with cookie injection and automatic decompression.
func (c *ClientSession) Do(ctx context.Context, method, url string, headers map[string]string, body io.Reader) (*Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	for k, v := range c.defaultHeaders {
		if req.Header.Get(k) == "" {
			req.Header.Set(k, v)
		}
	}

	for k, v := range headers {
		req.Header.Set(k, v)
	}

	host := extractHost(url)
	if cookieStr := c.getHostCookies(host); cookieStr != "" {
		req.Header.Set("Cookie", cookieStr)
	}

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}

	return parseResponse(resp)
}

// Get performs a GET request.
func (c *ClientSession) Get(ctx context.Context, url string, headers map[string]string) (*Response, error) {
	return c.Do(ctx, http.MethodGet, url, headers, nil)
}

// Post performs a POST request with form body.
func (c *ClientSession) Post(ctx context.Context, url string, headers map[string]string, formValues map[string]string) (*Response, error) {
	formData := strings.NewReader(encodeForm(formValues))
	return c.Do(ctx, http.MethodPost, url, headers, formData)
}

// PostJSON performs a POST request with JSON body.
func (c *ClientSession) PostJSON(ctx context.Context, url string, headers map[string]string, body interface{}) (*Response, error) {
	jsonBody, _ := json.Marshal(body)
	if headers == nil {
		headers = make(map[string]string)
	}
	headers["Content-Type"] = "application/json"
	return c.Do(ctx, http.MethodPost, url, headers, strings.NewReader(string(jsonBody)))
}

// getHostCookies returns the Cookie header value for a host.
func (c *ClientSession) getHostCookies(host string) string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.baseCookies[host]
}

func extractHost(urlStr string) string {
	idx := strings.Index(urlStr, "://")
	if idx == -1 {
		return urlStr
	}
	rest := urlStr[idx+3:]
	slash := strings.Index(rest, "/")
	if slash == -1 {
		return rest
	}
	return rest[:slash]
}

// detectEncoding detects the character encoding from Content-Type header.
// Returns "utf-8" as default fallback.
func detectEncoding(contentType string) string {
	lower := strings.ToLower(contentType)
	if idx := strings.Index(lower, "charset="); idx != -1 {
		cs := lower[idx+len("charset="):]
		end := strings.IndexAny(cs, "; \t\n\r")
		if end != -1 {
			cs = cs[:end]
		}
		switch cs {
		case "gbk", "gb2312", "gb18030":
			return "gbk"
		case "iso-8859-1", "latin1":
			return "latin1"
		default:
			return cs
		}
	}
	return "utf-8"
}

// Helper functions
func encodeCookieMap(m map[string]string) string {
	pairs := make([]string, 0, len(m))
	for k, v := range m {
		pairs = append(pairs, k+"="+v)
	}
	return strings.Join(pairs, "; ")
}

func decodeCookies(s string) map[string]string {
	result := make(map[string]string)
	if s == "" {
		return result
	}
	for _, pair := range strings.Split(s, "; ") {
		if idx := strings.Index(pair, "="); idx != -1 {
			result[pair[:idx]] = pair[idx+1:]
		}
	}
	return result
}

func encodeForm(values map[string]string) string {
	data := make([]string, 0, len(values))
	for k, v := range values {
		data = append(data, k+"="+v)
	}
	return strings.Join(data, "&")
}

// ReadAllWithDecompression reads response body with gzip/deflate handling.
func ReadAllWithDecompression(resp *http.Response) (string, string, error) {
	var reader io.Reader = resp.Body
	defer resp.Body.Close()

	contentEncoding := strings.ToLower(resp.Header.Get("Content-Encoding"))
	if contentEncoding == "gzip" {
		gz, err := gzip.NewReader(resp.Body)
		if err != nil {
			return "", "", fmt.Errorf("create gzip reader: %w", err)
		}
		defer gz.Close()
		reader = gz
	}

	rawBytes, err := io.ReadAll(reader)
	if err != nil {
		return "", "", fmt.Errorf("read body: %w", err)
	}

	encoding := detectEncoding(resp.Header.Get("Content-Type"))
	return string(rawBytes), encoding, nil
}
