# 📚 webnovel_rules — 书源驱动的小说 / 漫画系统（Web + 独立 APK）

一套以 **legado（阅读 App）书源规则**驱动的内容系统：导入书源 → 自动校验 → 搜索 → 爬取/下载 → 阅读。
所有站点解析全部由书源 JSON 规则驱动，**不硬编码任何网站**。

同一套引擎，两种使用形态：

| 形态 | 说明 | 适用 |
|---|---|---|
| **Web 桌面端** | Flask 服务端 + 浏览器界面，在本机/局域网使用 | 电脑上看书、管理书库与任务 |
| **独立 APK** | 引擎以 Chaquopy 打包进安卓 App，**不连电脑、不连云端**，装上即用 | 手机在线/离线阅读 |

> 当前版本：**APK 1.2.4-demo（versionCode 126）** ｜ 桌面全量测试 **1561 passed / 2 skipped** ｜
> JVM 单测 **38/38** ｜ 客户端↔服务端双向契约守卫 **0 问题**

---

## 系统全景

```
┌──────────────────────────── 同一套 Python 引擎 ───────────────────────────┐
│  engine/      书源规则执行器（搜索/详情/目录/正文/净化） + 漫画适配器      │
│  server/      Flask API：小说、漫画、任务、存储、网络、诊断                │
│  sources/     内置书源（小说 legado JSON / 漫画源配置）                   │
└──────────────────────────────────────────────────────────────────────────┘
        ▲ 浏览器直接访问                       ▲ 127.0.0.1 本机回环（App 私有）
        │                                       │
┌───────┴────────┐                    ┌─────────┴──────────────┐
│ Web 前端        │                    │ Android App（Kotlin +   │
│ templates/ +    │                    │ Jetpack Compose，原生   │
│ static/         │                    │ 界面，引擎随包内置）    │
└────────────────┘                    └─────────────────────────┘
```

APK 的独立性：引擎运行在 App 私有服务进程里，UI 通过本机回环 HTTP（带令牌）调用；
**不需要电脑、不需要云端账号、不经过任何第三方服务器**（除目标书源站点本身）。

---

## 核心能力

### 小说
- **书源管理**：导入 legado 格式书源（单个 / 数组 / `{"data":[...]}` 包装）；导入时**自动实测校验**
  （默认关键词"剑来"，可用 `ruleSearch.checkKeyWord` 指定），逐源标注 ✅/❌ 与失败原因
- **搜索**：按书名/作者跨源搜索（流式逐源上屏），结果按书名+作者分组合并，卡片内切换来源
- **爬取任务**：整书爬取、暂停/恢复/取消/删除、失败章节优先重试、断点续爬
- **阅读器**：三套阅读主题（白 / 护眼 / 夜间）、字号行距可调、进度实时记忆、净化规则
  （L7 补段落 / L8 整块去重 / L9 章尾清理，均有测试钉住）

### 漫画
- **内置漫画源**：拷贝漫画（copymanga）、JM 等多个适配器，逐源能力/分类/实测结论可查
- **搜索 / 分类浏览**：跨源流式搜索、单源搜索与排序（jm 支持 mr/mv/mp 等排序）
- **下载**：整话/选话下载、批量暂停恢复、断点续下、本地图片缺失检测与修复
- **阅读器**：纵向连续 / 横向翻页、双指缩放、整话自 pacing 预取、下一话零等待、
  页级失败自动退避重试

### 双端一致的体验
- **书架**：「最近阅读 / 已缓存」双页；最近阅读是**全量历史**（含在线读过未下载的漫画）；
  已缓存按「最近下载 / 最近阅读」排序（偏好本地持久化，双端语义一致）
- **进度互通**：小说 `book_progress.json`、漫画 `_history.json` 双端共用，Web 与 APK 互相续读
- **离线模式**：引擎未就绪时书架退化为本地只读索引，已下载内容照读不误
- **图片缓存**：占用统计 / 上限可调（256MB–不限）/ 一键清除（存储管理页）
- **备份恢复**：书源配置 + 阅读进度打包导出（不含正文与图片）

---

## 性能机制（为什么快）

在线加载不走"加大并发"的笨路（源站会风控），而是**按页分流 + 缓存命中**：

1. **`/urls` 批量接口**：打开一话一次返回每页加载方式——
   已下载 → 本地直出（磁盘毫秒级）；jm 等混淆源 → 服务器懒下载通道并**自动流水线预热**
  （你看第 1 页时后续页已在后台入库）；普通源 → **客户端直连源站 CDN，不经引擎中转**
2. **CDN 直连失败自动降级**服务器代理中继（带防盗链头），单图失败递进退避自动重试
3. **双通道图片加载**：可见页走交互通道（4 并发），后台预取走慢速通道（2 并发），
   预取永远抢不到交互请求的引擎线程
4. **搜索缓存跨端对齐**：请求参数与 Web 端归一，命中同一套服务端搜索缓存（重复搜索秒开）

---

## 设计原则：诚实性 UI

这套系统宁可"看起来笨"，也不在界面上撒谎（每条都有事故教训与测试/守卫）：

- 书源状态分**三轴**（人工校验 / 功能验证 / 移动可用性），不合成一个"可用/不可用"
- 断网时显示「本机当前没有网络」，而不是"没有结果"（归因错误曾让人以为书源全坏）
- 有记录但磁盘无内容时如实写「本机没有内容（长按可删除记录）」，不说"未读"
- 错误提示最多 3 行 + 逐源原因，不再整屏刷报错
- 恢复书源一次只恢复**一批**（避免把用户故意删掉的源带回来）
- 「重新净化」先干跑报"会清掉多少行"，确认后才动用户的书

---

## 仓库结构

```
app.py                 Web 端入口（Flask）
engine/                书源规则执行器 + 漫画适配器 + 净化器 + 下载器
server/                Flask API 蓝图（小说/漫画/任务/存储/网络/诊断）
templates/ + static/   Web 前端
sources/               内置书源（用户运行期新增/修改不入库）
android/               独立 APK（Kotlin + Compose，引擎经 Chaquopy 内置）
  app/src/main/java/com/webnovel/mobile/
    WnTheme.kt         设计体系（暖墨暗色 + 组件库）
    EngineData.kt      客户端解析层（契约守卫盯着，键名不可乱改）
tests/                 桌面全量测试（1561 项）
tools/                 契约守卫 / 探针（分页审计、目录完整性、章尾污染、本地计数核对）
docs/                  设计文档与调研笔记
```

---

## 快速开始

### Web 端

```bash
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py --port 8766
# 浏览器打开 http://127.0.0.1:8766
```

### APK 构建

```bash
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home \
  ./gradlew :app:assembleRelease        # 或 :app:assembleDebug
# 产物：android/app/build/outputs/apk/{release,debug}/*.apk
# 也可以直接用 Android Studio 打开本目录，Sync 后 Run
```

要求：Android SDK 35、JDK 17、Python 3.12（引擎经 Chaquopy 打包进 APK）。

---

## 验证体系（全部必须通过）

```bash
# ① 客户端逻辑单测（无需设备）
cd android && ./gradlew :app:testDebugUnitTest          # 38 项

# ② 桌面全量（引擎/服务端/契约/净化，约 2 分钟）
PYTHONDONTWRITEBYTECODE=1 WR_TEST=1 WR_DISABLE_BACKGROUND=1 \
  venv/bin/python -m pytest tests/ -o addopts='' -q -p no:cacheprovider
# 1561 passed / 2 skipped

# ③ 客户端↔服务端双向契约守卫（字段漂移防线）
venv/bin/python tools/check_client_server_contract.py   # 22 接口 / 0 问题
venv/bin/python tools/check_request_contract.py         # 14 接口 / 0 问题

# ④ 可复跑探针（数据质量自查）
venv/bin/python tools/probe_pagination_audit.py         # 多页正文
venv/bin/python tools/probe_toc_integrity.py            # 全目录
venv/bin/python tools/probe_chapter_tail.py             # 章尾污染
venv/bin/python tools/probe_local_counts.py             # API 数字 vs 磁盘
```

---

## 书源格式（legado 兼容子集）

```json
{
  "bookSourceName": "示例源",
  "bookSourceUrl": "https://m.example.org",
  "searchUrl": "/search.php?searchkey={{key}}",
  "ruleSearch": {"bookList": "div.result p", "name": "a@text",
                 "author": "span.author@text", "bookUrl": "a@href"},
  "ruleBookInfo": {"name": "h1@text", "tocUrl": "{{$.}}"},
  "ruleToc": {"chapterList": "ul.list li", "chapterName": "a@text", "chapterUrl": "a@href"},
  "ruleContent": {"content": "#content@text", "replaceRegex": "##广告[^\\n]*"}
}
```

支持：CSS 选择器、`@json:` JSONPath、`@正则:`、模板 `{{key}}/{{page}}`、`##pat##repl` 清洗、
`||` 多规则、`@@` 链式、`@text/@href/@src/@html/@attr:x/@ownText/@textNodes`、`!N` 去首 N、
`tocSort:"url"` 目录按 URL 数字排序。详见 [docs/书源制作指南.md](docs/书源制作指南.md)。

书源合集参考：[liufuyou/read](https://github.com/liufuyou/read)、[aoaostar/legado](https://github.com/aoaostar/legado)。
开源合集时效性差，导入后系统会**自动校验并标记不可用**。

---

## 合规与边界

- 本系统只是**规则驱动的下载与阅读工具**，不内置任何受版权保护的内容；
  书源指向的站点与内容由用户自行选择与负责，请仅用于个人学习用途
- 用户运行数据（书库、缓存、阅读进度、代理配置）全部留在本机 `data/`（不入库、不上传）
- APK 不连电脑、不连云端，除目标书源站点外不与任何第三方服务器通信

---

## ☕ 赞赏支持

如果这个项目对你有帮助，可以请作者喝杯咖啡：

<img src="docs/images/wechat_reward_qrcode.png" alt="微信赞赏码" width="260">
