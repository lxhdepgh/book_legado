package httpclient

import (
	"encoding/json"
	"net/http"
	"strings"

	"golang.org/x/net/html"
)

// Response wraps an HTTP response with helper methods.
type Response struct {
	Status     string
	StatusCode int
	Header     http.Header
	Body       string // already decompressed
	Encoding   string // detected encoding
}

// parseResponse converts a *http.Response into our Response type, handling
// gzip/deflate decompression and charset detection.
func parseResponse(resp *http.Response) (*Response, error) {
	bodyRaw, encoding, err := ReadAllWithDecompression(resp)
	if err != nil {
		return nil, err
	}

	return &Response{
		Status:     resp.Status,
		StatusCode: resp.StatusCode,
		Header:     resp.Header,
		Body:       bodyRaw,
		Encoding:   encoding,
	}, nil
}

// IsSuccess reports whether the status code indicates success (2xx).
func (r *Response) IsSuccess() bool {
	return r.StatusCode >= 200 && r.StatusCode < 300
}

// HasError reports whether the status code indicates an error.
func (r *Response) HasError() bool {
	return r.StatusCode >= 400
}

// BodyString returns the response body as a string.
func (r *Response) BodyString() string {
	return r.Body
}

// CookieString returns Set-Cookie headers as a semicolon-separated string.
func (r *Response) CookieString() string {
	cookies := r.Header.Values("Set-Cookie")
	if len(cookies) == 0 {
		return ""
	}
	return strings.Join(cookies, "; ")
}

// ExtractCookieJar updates the session's cookie jar from response Set-Cookie headers.
func (r *Response) ExtractCookieJar(session *ClientSession, host string) {
	for _, raw := range r.Header.Values("Set-Cookie") {
		parts := strings.SplitN(raw, "=", 2)
		if len(parts) == 2 {
			name := strings.TrimSpace(parts[0])
			value := strings.TrimSpace(strings.SplitN(parts[1], ";", 2)[0])
			session.SetSingleCookie(host, name, value)
		}
	}
}

// BodyJSON decodes the body as JSON into v.
func (r *Response) BodyJSON(v interface{}) error {
	return json.Unmarshal([]byte(r.Body), v)
}

// SelectCSS runs a CSS selector on the body HTML and returns matching text nodes.
func (r *Response) SelectCSS(selector string) []string {
	doc, err := html.Parse(strings.NewReader(r.Body))
	if err != nil {
		return nil
	}
	var results []string
	var walk func(*html.Node)
	walk = func(n *html.Node) {
		if n.Type == html.ElementNode && matchesSelector(n, selector) {
			if text := getTextContent(n); text != "" {
				results = append(results, text)
			}
		}
		for c := n.FirstChild; c != nil; c = c.NextSibling {
			walk(c)
		}
	}
	walk(doc)
	return results
}

func matchesSelector(n *html.Node, selector string) bool {
	if n.Type != html.ElementNode {
		return false
	}
	tag := n.Data
	if strings.HasPrefix(selector, ".") {
		className := selector[1:]
		for _, attr := range n.Attr {
			if attr.Key == "class" {
				for _, class := range strings.Fields(attr.Val) {
					if class == className {
						return true
					}
				}
			}
		}
		return false
	}
	if strings.HasPrefix(selector, "#") {
		id := selector[1:]
		for _, attr := range n.Attr {
			if attr.Key == "id" && attr.Val == id {
				return true
			}
		}
		return false
	}
	return tag == selector
}

func getTextContent(n *html.Node) string {
	var parts []string
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		if c.Type == html.TextNode {
			parts = append(parts, c.Data)
		} else {
			t := getTextContent(c)
			if t != "" {
				parts = append(parts, t)
			}
		}
	}
	return strings.Join(parts, "")
}
