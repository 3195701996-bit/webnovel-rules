# 工具目录

[文档导航](../docs/README.md) · [开发命令](../docs/development.md)

| 类别 | 入口 | 边界 |
|---|---|---|
| 文档链接 | [check_documentation_links.py](check_documentation_links.py) | 检查维护入口的本地文件链接，不联网 |
| 请求契约 | [check_request_contract.py](check_request_contract.py) | Kotlin/JS 写请求与服务端字段对齐 |
| 响应契约 | [check_client_server_contract.py](check_client_server_contract.py) | 用隔离数据验证解析层 |
| HTTP smoke | [smoke_api.py](smoke_api.py) | 自建临时回环服务；不连接用户生产服务 |
| 资源一致性 | [check_android_web_assets.py](check_android_web_assets.py) | 比较 APK 内置 Web 与权威源码 |
| 发布验收 | [verify_android_release.sh](verify_android_release.sh) | 只读核对版本、准确 SHA、签名和摘要 |
| 来源探针 | `probe_*.py`、`verify_sources_live.py` | 可能访问外网；逐脚本阅读参数及输出范围 |
| 设备演练 | `android_*.sh` | 仅匹配的专用模拟器；可能切网络、强杀或制造存储压力 |

文件存在不表示应全部执行。先阅读脚本说明及设备限制，显式选择目标；探针通过不保证外部源长期稳定。`runtime_server.py` 是本地隔离服务辅助，不是生产部署入口。
