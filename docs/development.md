# 开发与验证

[文档导航](README.md) · [架构](architecture.md)

## Web 环境

CI 验证 Python 3.11 / 3.12。以下从仓库根目录执行：

```bash
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-manga.txt
venv/bin/python app.py --host 127.0.0.1 --port 8766
```

打开 `http://127.0.0.1:8766`。核心小说能力不必安装漫画可选依赖；浏览器渲染来源还需按实际能力安装 Playwright Chromium。来源是否可用仍取决于依赖、网络和站点响应，不由依赖安装成功保证。

升级已有 Web 时沿用原数据目录和启动配置；不要覆盖 `data/` 或复制别人的运行状态。安装 APK 不会替电脑重启 Web 服务。

## Android 构建

需要 JDK 17、Android SDK 35、Python 3.13（Chaquopy 构建用）。签名文件只放本机，不提交仓库。

```bash
cd android
./gradlew :app:testDebugUnitTest :app:assembleDebug --no-daemon
```

正式包使用本机 release 签名配置并执行 `:app:assembleRelease`。没有签名材料时不能冒充正式签名，不能用重新签名的包覆盖已有安装。构建入口是 `android/gradlew`，不需要运行旧版手工同步脚本。

## 自动化检查

```bash
venv/bin/pip install -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 WR_TEST=1 WR_DISABLE_BACKGROUND=1 \
  venv/bin/python -m pytest tests/ -o addopts='' -q -p no:cacheprovider --timeout=120
venv/bin/python tools/check_documentation_links.py
venv/bin/python tools/check_request_contract.py
WR_TEST=1 WR_DISABLE_BACKGROUND=1 venv/bin/python tools/check_client_server_contract.py
venv/bin/python tools/smoke_api.py
```

Web JS 回归需要 Node.js，CI 使用 Node 24：

```bash
set -e
for test_file in tests/js/*.test.js; do node "$test_file"; done
```

Android 仪器测试不等于构建成功。合成书库、故障注入、低存储和强杀测试只允许匹配的专用模拟器，禁止在真实用户手机上运行。外部来源探针需要显式选择并控制网络开销，不能加入默认离线测试冒充确定性验证。

## 正式发布门

1. 冻结准确提交并保持构建输入干净；确认版本码递增。
2. 跑对应范围测试、完整 CI，核对内置 Web 资源。
3. 用 [APK 验收器](../tools/verify_android_release.sh) 对比前版：源码 SHA、版本、包名、签名、完整性和摘要。
4. 保留数据覆盖升级，核对真实用户状态；不卸载、不清数据来绕过兼容问题。
5. 先准备草稿与校验文件，回下载验证后再公开，确认标签指向冻结提交。
6. 同步发布说明与首页入口，关闭本次测试工具；不删除用户数据或重写旧 Release。

测试数量随源码变化，以准确提交 CI 为准；截图、单项成功和模拟器结果不能代替完整发布验收。
