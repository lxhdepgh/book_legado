"""RPC 协议面（设计 §4）：Flutter/外部客户端与本进程之间唯一的对话形状。

本包是**纯协议层**：零 flet、零服务实例、零业务逻辑。它只回答三件事——

- 信封长什么样（:mod:`app.gateway.protocol`）；
- 哪些 action 存在、各自映射到哪个服务的哪个方法（:mod:`app.gateway.methods`，
  显式表，不反射扫服务）；
- 出边界的东西怎么变成 JSON 原生值（:mod:`app.gateway.serialize`）。

运行期的 HTTP/WS 服务在 :mod:`app.services.gateway.service`（协议面不许反向依赖它）。
"""
