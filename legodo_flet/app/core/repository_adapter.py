"""Repository 适配器（渐进式迁移方案）。

设计思路：
- 不立即改造所有 Controller，而是提供一个适配器层
- Controller 仍通过 self.service() 获取服务
- 适配器在 Service 和 Repository 之间桥接
- 新代码直接使用 Repository，旧代码逐步迁移

用法：
    # 在 main.py 中
    from app.core.repository_adapter import create_repository
    
    repo = create_repository(hub, use_fake=False)  # 生产环境
    # repo = create_repository(hub, use_fake=True)  # 测试环境
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.hub import ServiceHub
from app.core.repository import EngineRepository

if TYPE_CHECKING:
    from app.gateway.client import GatewayClient


def create_repository(
    hub: ServiceHub,
    use_fake: bool = False,
    fake_data: dict | None = None,
) -> EngineRepository:
    """创建 Repository 实例（工厂函数）。
    
    Parameters
    ----------
    hub : ServiceHub
        服务中枢
    use_fake : bool
        是否使用假仓库（测试用）
    fake_data : dict, optional
        假仓库的自定义数据
    
    Returns
    -------
    EngineRepository
        Repository 实例
    """
    if use_fake:
        from app.testing.fake_repository import FakeEngineRepository
        
        kwargs = {}
        if fake_data:
            kwargs.update(fake_data)
        
        return FakeEngineRepository(**kwargs)
    
    # 生产环境：RPC Repository
    from app.gateway.client import GatewayClient
    from app.gateway.rpc_repository import RpcEngineRepository
    
    client = GatewayClient(hub)
    return RpcEngineRepository(client)


class ServiceToRepositoryAdapter:
    """Service → Repository 适配器（让旧 Controller 无缝使用 Repository）。
    
    用法：
        adapter = ServiceToRepositoryAdapter(hub)
        repo = adapter.create_repository()
        
        # 或者直接在 Controller 中使用
        class MyController(BaseController):
            def __init__(self, hub, repository=None):
                super().__init__(hub)
                self._repo = repository or ServiceToRepositoryAdapter(hub).create_repository()
    """
    
    def __init__(self, hub: ServiceHub):
        self._hub = hub
    
    def create_repository(self, use_fake: bool = False) -> EngineRepository:
        """创建 Repository 实例。"""
        return create_repository(self._hub, use_fake=use_fake)
