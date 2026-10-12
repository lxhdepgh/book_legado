#!/usr/bin/env python3
"""后端交互问题诊断报告。

基于 test_backend_interaction.py 的测试结果，总结当前发现的问题。
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    print("\n" + "=" * 70)
    print("  Legodo Dart 后端交互问题诊断报告")
    print("=" * 70)
    
    print("\n【测试环境】")
    print(f"  Python: {sys.executable}")
    print(f"  Project root: {Path(__file__).resolve().parent.parent}")
    
    print("\n【已通过的测试】")
    print("  [OK] 测试 1: 后端启动 + 握手协议")
    print("    - GatewayService 可以成功启动")
    print("    - 随机端口分配正常 (port > 0)")
    print("    - Token 生成正常 (长度 > 10)")
    print("    - URL 格式正确 (http://127.0.0.1:xxxxx)")
    
    print("\n  [OK] 测试 2: HTTP Action 调用（协议层面）")
    print("    - POST /rpc 端点可达")
    print("    - 响应状态码 200")
    print("    - 信封格式正确: {v, id, ok, data, error, code}")
    
    print("\n  [OK] 测试 3: WebSocket 连接")
    print("    - WS 端点可达 (ws://127.0.0.1:xxxxx/ws?token=...)")
    print("    - Token 认证通过")
    print("    - 连接建立成功")
    
    print("\n【发现的问题】")
    
    print("\n  [FAIL] 问题 1: GatewayService 缺少 ServiceHub")
    print("     现象: action 调用返回 'GatewayService 未找到 ServiceHub'")
    print("     原因: GatewayService.hub 为 None")
    print("     影响: 所有业务 action 都无法执行")
    print("     解决: 需要按以下顺序初始化:")
    print("           1. hub = _build_hub()")
    print("           2. gateway = GatewayService(...)")
    print("           3. hub.register(gateway)")
    print("           4. await hub.start_all()")
    print("     备注: _build_hub() 在 legodo_flet/app/main.py 中定义")
    
    print("\n  [FAIL] 问题 2: Action 表为空或未注册")
    print("     现象: '未知 action：gateway.hello' / 'sources.list'")
    print("     原因: BY_ACTION 表中没有这些动作")
    print("     影响: 客户端无法调用任何功能")
    print("     解决: 检查 app/gateway/methods.py 中的 action 注册")
    
    print("\n  [FAIL] 问题 3: WebSocket 立即关闭")
    print("     现象: 连接成功后立即收到 close frame (code=1000)")
    print("     原因: 可能是 idle timer 触发或没有活跃会话")
    print("     影响: 事件流无法工作")
    print("     解决: 检查 GatewayService._h_ws 的会话管理逻辑")
    
    print("\n【Dart 侧的错误信息分析】")
    print("  用户报告的错误: 'EngineFailure(transport @action=ws): 引擎那头的 /ws 关掉了会话'")
    print("  对应问题: 问题 3 - WebSocket 立即关闭")
    print("  根本原因: GatewayService 没有 ServiceHub，导致所有服务不可用")
    
    print("\n【建议的修复步骤】")
    print("  1. 从 legodo_flet/app/main.py 导入或复制 _build_hub() 函数")
    print("  2. 在测试脚本中正确初始化 ServiceHub + GatewayService")
    print("  3. 确认 action 表中有可用的动作")
    print("  4. 重新运行测试验证所有功能")
    
    print("\n" + "=" * 70)
    print("  诊断完成")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
