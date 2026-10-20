# 辅助脚本

[文档导航](../docs/README.md) · [当前启动与构建方式](../docs/development.md)

推荐显式运行 `venv/bin/python app.py --host 127.0.0.1 --port 8766`，停止自己启动的进程后再重启，避免按端口误停其他服务。

| 文件 | 状态 |
|---|---|
| [start.sh](start.sh) | 历史本机启动脚本：会按8766端口强停进程并清理缓存；本次保留，不推荐作为通用启动入口，也未执行 |
| [sync_android.sh](sync_android.sh) | 旧入口兼容封装，转到当前 Gradle 快照任务，不写废弃 android_app/ |
| [sync_templates.sh](sync_templates.sh) | 旧模板同步名，兼容转发到同一流程 |

`sync_android.sh --check <APK>` 仅只读比较内置 Web 资源；缺少 APK 参数会明确失败，不再假装检查已不存在的手工镜像。它不替代全部 Python 或运行时验收。

构建输出属于 `android/app/build/`，可以生成但不提交；用户数据和签名文件不在同步范围内。
