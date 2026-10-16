package jsruntime

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/dop251/goja"
)

// Context JS 执行上下文（对齐 Python JsContext 的书源规则子集）。
type Context struct {
	Source  map[string]interface{} // 书源数据（bookSourceUrl/bookSourceName/...）
	BaseURL string                 // 基础 URL（当页地址；空时回落 bookSourceUrl）
	Key     string                 // 搜索关键词 / 模板变量
	Page    int                    // 页码
	// Result 当前结果载荷（多阶段管线的 result 绑定）。字符串按原样绑定；
	// ResultHTML 时按 Jsoup 文档绑定；ResultNodes 时按元素列表绑定。
	Result     interface{}
	ResultHTML bool     // result 以 Jsoup 文档对象绑定（脚本可直接 result.select）
	ResultNodes []string // result 以元素列表绑定（每项一段 HTML；nil=未设置，空切片=空列表）
	Variables  map[string]interface{} // 变量袋（java.get/java.put）
	// Ajax 是 java.ajax 的同步取数回调，由引擎层注入（带源 cookie/UA）。
	// 失败时返回错误文本而不是抛异常——对齐 Python _ajax 的 runCatching 形状，
	// 脚本常把返回值直接喂给 Jsoup.parse/JSON.parse。
	Ajax func(requestValue string, headers map[string]string) (string, error)
}

// defaultTimeout 脚本级看门狗：一条死循环脚本不能挂死整条 DLL 调用线程。
const defaultTimeout = 5 * time.Second

// Runtime JS 运行时（goja VM 池 + 看门狗）。
type Runtime struct {
	mu      sync.Mutex
	vmPool  []*goja.Runtime
	timeout time.Duration
}

// NewRuntime 创建 JS 运行时。
func NewRuntime() *Runtime {
	return &Runtime{timeout: defaultTimeout}
}

// Execute 执行 JS 脚本；ctx 注入 Java 模拟面与 Legado 全局变量。
func (r *Runtime) Execute(script string, ctx *Context) (interface{}, error) {
	if ctx == nil {
		ctx = &Context{}
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	vm := r.getVM()
	var fired atomic.Bool
	timer := time.AfterFunc(r.timeout, func() {
		fired.Store(true)
		vm.Interrupt("script execution timeout")
	})
	defer timer.Stop()

	installGlobals(vm, ctx)
	value, err := vm.RunString(script)
	vm.ClearInterrupt()
	if err != nil {
		r.putVM(vm, false)
		if _, ok := err.(*goja.InterruptedError); ok {
			return nil, fmt.Errorf("脚本执行超时（%s）", r.timeout)
		}
		return nil, err
	}
	r.putVM(vm, !fired.Load())
	return NormalizeExport(value.Export()), nil
}

// getVM 从池中获取 VM。新建时必须挂 UncapFieldNameMapper，否则 Java DOM 面
// 的 Go 方法只能按导出名调用（`doc.Select`），jsoup 脚本写的是 `doc.select`。
func (r *Runtime) getVM() *goja.Runtime {
	if len(r.vmPool) > 0 {
		vm := r.vmPool[len(r.vmPool)-1]
		r.vmPool = r.vmPool[:len(r.vmPool)-1]
		return vm
	}
	vm := goja.New()
	vm.SetFieldNameMapper(goja.UncapFieldNameMapper())
	return vm
}

// putVM 释放 VM 回池；被看门狗打断过的 VM 直接丢弃（中断标志与挂起状态不回收）。
func (r *Runtime) putVM(vm *goja.Runtime, reusable bool) {
	if !reusable || len(r.vmPool) >= 10 {
		return
	}
	r.vmPool = append(r.vmPool, vm)
}

// Close 关闭运行时。
func (r *Runtime) Close() {
	r.vmPool = nil
}

// installGlobals 注入 Java 模拟面与 Legado 全局变量（对齐 Python bootstrap）。
func installGlobals(vm *goja.Runtime, ctx *Context) {
	baseURL := ctx.BaseURL
	if baseURL == "" {
		if source, ok := ctx.Source["bookSourceUrl"].(string); ok {
			baseURL = source
		}
	}
	vm.Set("baseUrl", baseURL)
	vm.Set("page", ctx.Page)
	vm.Set("key", ctx.Key)
	// result 绑定优先级：元素列表（列表管线 suffix 阶段）> Jsoup 文档（整页）
	// > 原样载荷。对齐 Python 引导脚本的 __resultHtmlNodes 分支。
	switch {
	case ctx.ResultNodes != nil:
		vm.Set("result", vm.ToValue(elementListValue(ctx.ResultNodes)))
	case ctx.ResultHTML:
		if markup, ok := ctx.Result.(string); ok {
			if doc, err := parseJsoupDocument(markup); err == nil {
				vm.Set("result", vm.ToValue(doc))
				break
			}
		}
		vm.Set("result", ctx.Result)
	default:
		vm.Set("result", ctx.Result)
	}
	source := ctx.Source
	if source == nil {
		source = map[string]interface{}{}
	}
	vm.Set("source", source)

	variables := ctx.Variables
	if variables == nil {
		variables = map[string]interface{}{}
		ctx.Variables = variables
	}
	vm.Set("variables", variables)

	installJavaShim(vm, ctx, variables)
}
