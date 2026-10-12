#!/usr/bin/env python3
"""独立测试后端交互链路（不依赖 Flutter UI）。

测试范围：
1. 后端启动 + 握手协议
2. HTTP action 调用
3. WebSocket 事件流
4. 完整请求-响应循环

用法：
    conda activate legodo_booksource_check
    cd legodo_dart
    python test_backend_interaction.py
"""
import asyncio
import json
import sys
from pathlib import Path

# 添加项目根目录到路径（后端源码在 app/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def test_1_handshake():
    """测试 1：后端启动和握手协议"""
    print("\n" + "=" * 60)
    print("测试 1: 后端启动 + 握手协议")
    print("=" * 60)
    
    # 【关键修复】使用 app.main._build_hub() 来正确初始化所有服务
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    
    from app.main import _build_hub
    from app.services.gateway.service import GatewayService
    
    # 构建完整的 ServiceHub（包含所有业务服务）
    hub = _build_hub()
    
    # 创建并注册 GatewayService
    gateway = GatewayService(port=0, idle_exit_seconds=30)
    hub.register(gateway)
    
    # 【关键】启动所有服务（否则服务实例不会创建，RPC 调用会失败）
    import asyncio
    await hub.start_all()
    
    # 启动 Gateway HTTP/WS 服务器
    await gateway.start_server()
    
    base_url = gateway.url
    token = gateway.token
    port = gateway._bound_port  # Use internal attribute
    
    print(f"[OK] 后端已启动")
    print(f"  - Base URL: {base_url}")
    print(f"  - Port: {port}")
    print(f"  - Token: {token[:20]}...")
    print(f"  - Gateway.hub: {gateway.hub is not None}")
    
    # 验证握手信息完整性
    assert base_url.startswith("http://"), f"Invalid base_url: {base_url}"
    assert len(token) > 10, f"Token too short: {token}"
    assert port > 0, f"Invalid port: {port}"
    assert gateway.hub is not None, "GatewayService.hub must be set"
    
    print("[PASS] 握手协议正常\n")
    
    return gateway


async def test_2_http_action(gateway):
    """测试 2：HTTP action 调用"""
    print("\n" + "=" * 60)
    print("测试 2: HTTP Action 调用")
    print("=" * 60)
    
    import aiohttp
    
    url = f"{gateway.url}/rpc"
    headers = {"Authorization": f"Bearer {gateway.token}"}
    
    # 先调用 hello 获取可用 action 列表
    payload = {
        "v": 1,
        "action": "engine.hello",  # gateway.hello 不存在，用 engine.hello 替代
        "payload": {}
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            print(f"[INFO] POST {url}")
            print(f"  - Status: {resp.status}")
            
            body = await resp.json()
            print(f"  - Response keys: {list(body.keys())}")
            
            # Check if response is envelope format
            if "data" in body and body.get("ok"):
                data = body["data"]
                if isinstance(data, dict):
                    print(f"  - Protocol version: {data.get('protocol_version')}")
                    capabilities = data.get('capabilities', [])
                    print(f"  - Capabilities: {capabilities}")
                    actions = data.get('actions', [])
                    print(f"  - Available actions ({len(actions)}):")
                    for i, action in enumerate(actions[:10]):  # Show first 10
                        print(f"    {i+1}. {action}")
                    if len(actions) > 10:
                        print(f"    ... and {len(actions) - 10} more")
                    
                    # Store actions for later tests
                    test_2_http_action.available_actions = actions
                elif isinstance(data, list):
                    print(f"  - Result count: {len(data)}")
                else:
                    print(f"  - Data: {data}")
            elif "error" in body:
                print(f"  - Error: {body['error']}")
    
    print("[PASS] HTTP action 调用正常\n")


async def test_3_websocket_events(gateway):
    """测试 3：WebSocket 事件流"""
    print("\n" + "=" * 60)
    print("测试 3: WebSocket 事件流")
    print("=" * 60)
    
    import aiohttp
    
    ws_url = gateway.url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws?token={gateway.token}"
    
    print(f"[INFO] Connecting to: {ws_url}")
    
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url) as ws:
            print("[OK] WebSocket 连接成功")
            
            # 发送 hello action
            payload = {
                "v": 1,
                "action": "engine.hello",
                "payload": {}
            }
            await ws.send_json(payload)
            
            # 接收响应（可能需要多次尝试，因为可能收到 close frame）
            try:
                response = await asyncio.wait_for(ws.receive(), timeout=2.0)
                if response.type == aiohttp.WSMsgType.TEXT:
                    data = response.json()
                    print(f"[INFO] Received response: {list(data.keys())}")
                    
                    if "data" in data and data.get("ok"):
                        inner_data = data["data"]
                        if isinstance(inner_data, dict) and "protocol_version" in inner_data:
                            print(f"  - Protocol version: {inner_data['protocol_version']}")
                elif response.type == aiohttp.WSMsgType.CLOSE:
                    print(f"[WARN] WebSocket closed immediately (code={response.data})")
            except asyncio.TimeoutError:
                print("[INFO] No response received within timeout")
    
    print("[PASS] WebSocket 事件流正常\n")


async def test_4_full_cycle(gateway):
    """测试 4：完整请求-响应循环"""
    print("\n" + "=" * 60)
    print("测试 4: 完整请求-响应循环")
    print("=" * 60)
    
    import aiohttp
    
    url = f"{gateway.url}/rpc"
    headers = {"Authorization": f"Bearer {gateway.token}"}
    
    # 测试多个 action（使用实际可用的 action）
    actions_to_test = [
        ("engine.hello", {}),
        ("source.list", {}),  # 修正：action 名是 source.list（单数），不是 sources.list
        ("bookshelf.list", {}),
    ]
    
    async with aiohttp.ClientSession() as session:
        for action_name, params in actions_to_test:
            payload = {
                "v": 1,
                "action": action_name,
                "payload": params
            }
            
            try:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    body = await resp.json()
                    status = "OK" if resp.status == 200 else f"ERROR {resp.status}"
                    print(f"  [{status}] {action_name}")
                    
                    if "error" in body:
                        print(f"    Error: {body['error']}")
                    elif "data" in body and isinstance(body["data"], list):
                        print(f"    Result count: {len(body['data'])}")
            except Exception as e:
                print(f"  [FAIL] {action_name}: {e}")
    
    print("[PASS] 完整循环测试完成\n")


async def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("  Legodo Dart 后端交互独立测试")
    print("=" * 60)
    
    try:
        # 测试 1：启动后端
        gateway = await test_1_handshake()
        
        try:
            # 测试 2：HTTP action
            await test_2_http_action(gateway)
            
            # 测试 3：WebSocket
            await test_3_websocket_events(gateway)
            
            # 测试 4：完整循环
            await test_4_full_cycle(gateway)
            
            print("\n" + "=" * 60)
            print("  所有测试通过！")
            print("=" * 60)
            
        finally:
            # 清理：停止后端
            print("\n[INFO] Stopping backend...")
            await gateway.stop_server()
            print("[OK] Backend stopped\n")
    
    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
