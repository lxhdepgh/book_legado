package jsruntime

import (
	"fmt"
	"sync"

	"github.com/dop251/goja"
)

// Context JS 执行上下文（对齐 Python JsContext）
type Context struct {
	Source    map[string]interface{} // 书源数据
	BaseURL   string                 // 基础 URL
	Page      int                    // 页码
	Variables map[string]interface{} // 变量袋
}

// Runtime JS 运行时（对齐 Python LegadoJsRuntime）
type Runtime struct {
	mu     sync.Mutex
	vmPool []*goja.Runtime
}

// NewRuntime 创建 JS 运行时
func NewRuntime() *Runtime {
	return &Runtime{
		vmPool: make([]*goja.Runtime, 0),
	}
}

// Execute 执行 JS 脚本
func (r *Runtime) Execute(script string, ctx *Context) (interface{}, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	// 获取或创建 VM
	vm := r.getVM()
	defer r.releaseVM(vm)

	// 注入全局变量
	r.injectGlobals(vm, ctx)

	// 执行脚本
	value, err := vm.RunString(script)
	if err != nil {
		return nil, fmt.Errorf("JS 执行失败: %w", err)
	}

	return value.Export(), nil
}

// injectGlobals 注入全局变量（对齐 Legado API）
func (r *Runtime) injectGlobals(vm *goja.Runtime, ctx *Context) {
	// java.baseUrl
	vm.Set("java", map[string]interface{}{
		"baseUrl": ctx.BaseURL,
		"source":  ctx.Source,
	})

	// page
	vm.Set("page", ctx.Page)

	// result (初始为空)
	vm.Set("result", "")

	// variables
	if ctx.Variables == nil {
		ctx.Variables = make(map[string]interface{})
	}
	vm.Set("variables", ctx.Variables)
}

// getVM 从池中获取 VM
func (r *Runtime) getVM() *goja.Runtime {
	if len(r.vmPool) > 0 {
		vm := r.vmPool[len(r.vmPool)-1]
		r.vmPool = r.vmPool[:len(r.vmPool)-1]
		return vm
	}
	return goja.New()
}

// releaseVM 释放 VM 回池
func (r *Runtime) releaseVM(vm *goja.Runtime) {
	// 清空全局变量，避免污染
	vm.Set("java", nil)
	vm.Set("page", nil)
	vm.Set("result", nil)
	vm.Set("variables", nil)

	if len(r.vmPool) < 10 { // 池上限 10
		r.vmPool = append(r.vmPool, vm)
	}
}

// Close 关闭运行时
func (r *Runtime) Close() {
	r.vmPool = nil
}
