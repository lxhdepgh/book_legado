package gateway

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/legodo-booksource/legodo_go/gateway"
	"github.com/legodo-booksource/legodo_go/internal/services"
	"github.com/legodo-booksource/legodo_go/internal/models"
)

// Gateway manages WebSocket + HTTP RPC connections.
type Gateway struct {
	hub          *services.ServiceHub
	server       *http.Server
	upgrader     websocket.Upgrader
	clients      map[*websocket.Conn]bool
	clientsMu    sync.RWMutex
	requestCount int
	methods      gateway.MethodRegistry
}

// NewGateway creates a new RPC gateway connected to the service hub.
func NewGateway(hub *services.ServiceHub) *Gateway {
	return &Gateway{
		hub: hub,
		clients: make(map[*websocket.Conn]bool),
		upgrader: websocket.Upgrader{
			CheckOrigin: func(r *http.Request) bool {
				return true // TODO: restrict origin in production
			},
		},
		methods: gateway.NewMethodRegistry(),
	}
}

// Start starts the HTTP server on the given address.
func (g *Gateway) Start(addr string) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/rpc", g.handleRPCHTTP)
	mux.HandleFunc("/rpc/ws", g.handleWebSocket)
	mux.HandleFunc("/health", g.handleHealth)

	g.server = &http.Server{Addr: addr, Handler: mux}
	log.Printf("[Gateway] listening %s", addr)
	return g.server.ListenAndServe()
}

// Stop gracefully shuts down the gateway.
func (g *Gateway) Stop() {
	if g.server != nil {
		g.server.Close()
	}
}

func (g *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("OK"))
}

func (g *Gateway) handleRPC(w http.ResponseWriter, r *http.Request) {
	var raw gateway.RequestEnvelope
	if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
		resp := gateway.ErrorResponse("", gateway.CodeBadRequest, "invalid JSON")
		writeJSON(w, resp)
		return
	}

	result := g.dispatch(raw.Action, raw.Params)
	writeJSON(w, result)
}

func (g *Gateway) handleRPCHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	g.handleRPC(w, r)
}

func (g *Gateway) handleWebSocket(w http.ResponseWriter, r *http.Request) {
	conn, err := g.upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[Gateway] upgrade failed: %v", err)
		return
	}
	defer conn.Close()

	g.clientsMu.Lock()
	g.clients[conn] = true
	g.clientsMu.Unlock()

	log.Printf("[Gateway] client connected (%d total)", len(g.clients))

	for {
		var msg json.RawMessage
		if err := conn.ReadJSON(&msg); err != nil {
			break
		}

		envelope, parseErr := gateway.ParseRequest(string(msg))
		if parseErr != nil {
			response := gateway.ErrorResponse("", gateway.CodeBadRequest, parseErr.Error())
			conn.WriteJSON(response)
			continue
		}

		g.requestCount++
		response := g.handleEnvelope(envelope, g.requestCount)
		conn.WriteJSON(response)
	}

	g.clientsMu.Lock()
	delete(g.clients, conn)
	g.clientsMu.Unlock()
	log.Printf("[Gateway] client disconnected (%d total)", len(g.clients))
}

func (g *Gateway) handleEnvelope(envelope *gateway.RequestEnvelope, id int) *gateway.ResponseEnvelope {
	actionID := envelope.ID
	if actionID == "" {
		actionID = fmt.Sprintf("req-%d", id)
	}

	result := g.dispatch(envelope.Action, envelope.Params)
	result.ID = actionID
	return result
}

// dispatch routes an action name + params to the appropriate handler.
func (g *Gateway) dispatch(action string, params map[string]interface{}) *gateway.ResponseEnvelope {
	if _, exists := g.methods.GetAction(action); !exists {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, fmt.Sprintf("unknown action: %s", action))
	}

	switch {
	case action == "engine.hello":
		return g.actionEngineHello(params)
	case action == "engine.status":
		return g.actionEngineStatus(params)
	case action == "engine.shutdown":
		return g.actionEngineShutdown(params)
	case action == "source.list":
		return g.actionSourceList(params)
	case action == "source.get":
		return g.actionSourceGet(params)
	case action == "source.save":
		return g.actionSourceSave(params)
	case action == "source.delete":
		return g.actionSourceDelete(params)
	case action == "source.enable":
		return g.actionSourceEnable(params)
	case action == "source.export":
		return g.actionSourceExport(params)
	case action == "check.start":
		return g.actionCheckStart(params)
	case action == "check.snapshot":
		return g.actionCheckSnapshot(params)
	case action == "check.cancel":
		return g.actionCheckCancel(params)
	case action == "settings.read":
		return g.actionSettingsRead(params)
	case action == "settings.write":
		return g.actionSettingsWrite(params)
	default:
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, fmt.Sprintf("unimplemented: %s", action))
	}
}

// --- Engine actions ---

func (g *Gateway) actionEngineHello(params map[string]interface{}) *gateway.ResponseEnvelope {
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"status": "running"})
}

func (g *Gateway) actionEngineStatus(params map[string]interface{}) *gateway.ResponseEnvelope {
	report := g.hub.StatusReport()
	statuses := make([]interface{}, len(report))
	for i, s := range report {
		statuses[i] = map[string]interface{}{"id": s.ServiceID, "state": s.State}
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"services": statuses})
}

func (g *Gateway) actionEngineShutdown(params map[string]interface{}) *gateway.ResponseEnvelope {
	go func() {
		log.Println("[Gateway] shutdown requested")
		time.Sleep(2 * time.Second)
		os.Exit(0)
	}()
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"shutting_down": true})
}

// --- Source actions ---

func (g *Gateway) actionSourceList(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	sources := sourceSvc.List()
	data := make([]interface{}, len(sources))
	for i, s := range sources {
		data[i] = BookSourceToMap(s)
	}
	return gateway.OKResponse(getReqId(params), data)
}

func (g *Gateway) actionSourceGet(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	url := getStringParam(params, "url")
	if url == "" {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "missing url")
	}
	src, ok := sourceSvc.Get(url)
	if !ok {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "source not found")
	}
	return gateway.OKResponse(getReqId(params), BookSourceToMap(*src))
}

func (g *Gateway) actionSourceSave(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	sourceData, ok := params["source"].(map[string]interface{})
	if !ok {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "source object required")
	}
	jsonData, err := json.Marshal(sourceData)
	if err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, fmt.Sprintf("marshal: %v", err))
	}
	var src models.BookSource
	if err := json.Unmarshal(jsonData, &src); err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, fmt.Sprintf("parse: %v", err))
	}
	if err := sourceSvc.Save(&src); err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeEngine, err.Error())
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"saved": true, "url": src.BookSourceURL})
}

func (g *Gateway) actionSourceDelete(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	url := getStringParam(params, "url")
	if url == "" {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "missing url")
	}
	if err := sourceSvc.Delete(url); err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeEngine, err.Error())
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"deleted": true})
}

func (g *Gateway) actionSourceEnable(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	url := getStringParam(params, "url")
	enabled := params["enabled"] != nil && params["enabled"] == true
	if url == "" {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "missing url")
	}
	if err := sourceSvc.Enable(url, enabled); err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeEngine, err.Error())
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"enabled": enabled})
}

func (g *Gateway) actionSourceExport(params map[string]interface{}) *gateway.ResponseEnvelope {
	sourceSvc := g.hub.SourceService()
	if sourceSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "source service unavailable")
	}
	jsonData, err := sourceSvc.ExportAll()
	if err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeEngine, err.Error())
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"json": jsonData})
}

// --- Check actions ---

func (g *Gateway) actionCheckStart(params map[string]interface{}) *gateway.ResponseEnvelope {
	checkSvc := g.hub.CheckService()
	if checkSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "check service unavailable")
	}
	go func() {
		if _, err := checkSvc.RunAll(nil); err != nil {
			log.Printf("[Gateway] check run failed: %v", err)
		}
	}()
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"started": true})
}

func (g *Gateway) actionCheckSnapshot(params map[string]interface{}) *gateway.ResponseEnvelope {
	checkSvc := g.hub.CheckService()
	if checkSvc == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeUnavailable, "check service unavailable")
	}
	snap := checkSvc.Snapshot()
	return gateway.OKResponse(getReqId(params), CheckSummaryToMap(snap))
}

func (g *Gateway) actionCheckCancel(_ map[string]interface{}) *gateway.ResponseEnvelope {
	checkSvc := g.hub.CheckService()
	if checkSvc == nil {
		return gateway.ErrorResponse(getReqId(nil), gateway.CodeUnavailable, "check service unavailable")
	}
	checkSvc.Cancel()
	return gateway.OKResponse(getReqId(nil), map[string]interface{}{"cancelled": true})
}

// --- Settings actions ---

func (g *Gateway) actionSettingsRead(params map[string]interface{}) *gateway.ResponseEnvelope {
	settingsSvc := g.hub.SettingsService()
	keysRaw := params["keys"]
	keyList := []string{}
	switch arr := keysRaw.(type) {
	case []interface{}:
		for _, k := range arr {
			keyList = append(keyList, fmt.Sprintf("%v", k))
		}
	}
	values, _ := settingsSvc.ReadKeys(keyList)
	return gateway.OKResponse(getReqId(params), values)
}

func (g *Gateway) actionSettingsWrite(params map[string]interface{}) *gateway.ResponseEnvelope {
	settingsSvc := g.hub.SettingsService()
	key := getStringParam(params, "key")
	value := params["value"]
	if key == "" || value == nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeBadRequest, "key and value required")
	}
	if err := settingsSvc.Set(key, value); err != nil {
		return gateway.ErrorResponse(getReqId(params), gateway.CodeEngine, err.Error())
	}
	return gateway.OKResponse(getReqId(params), map[string]interface{}{"set": true, "key": key})
}

// --- Helper functions ---

func writeJSON(w http.ResponseWriter, resp interface{}) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func getStringParam(params map[string]interface{}, key string) string {
	if v, ok := params[key]; ok {
		return fmt.Sprintf("%v", v)
	}
	return ""
}

func getReqId(params map[string]interface{}) string {
	if params == nil {
		return ""
	}
	if id, ok := params["id"]; ok {
		return fmt.Sprintf("%v", id)
	}
	if id, ok := params["__id"]; ok {
		return fmt.Sprintf("%v", id)
	}
	return ""
}

// --- Conversion helpers ---

// BookSourceToMap converts a BookSource to a map for JSON serialization.
func BookSourceToMap(src models.BookSource) map[string]interface{} {
	return map[string]interface{}{
		"bookSourceUrl":   src.BookSourceURL,
		"bookSourceName":  src.BookSourceName,
		"bookSourceGroup": src.BookSourceGroup,
		"enabled":         src.Enabled,
		"customOrder":     src.CustomOrder,
		"respondTime":     src.RespondTime,
	}
}

// CheckSummaryToMap converts a CheckSummary to a map.
func CheckSummaryToMap(snap *services.CheckSummary) map[string]interface{} {
	if snap == nil {
		return map[string]interface{}{}
	}
	results := make([]interface{}, len(snap.Results))
	for i, r := range snap.Results {
		results[i] = map[string]interface{}{
			"sourceUrl":  r.SourceURL,
			"state":      r.State,
			"duration":   r.Duration.Milliseconds(),
			"error":      r.Error,
		}
	}
	return map[string]interface{}{
		"results":   results,
		"total":     snap.Total,
		"done":      snap.Done,
		"updatedAt": snap.UpdatedAt.Format(time.RFC3339),
	}
}
