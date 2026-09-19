# Kimi Coding 协作记录

## 通道
- API: `https://api.kimi.com/coding/v1/chat/completions`
- 模型: `kimi-for-coding`（官方 code-plan）
- 关键参数（踩坑记录）:
  - `temperature` 只允许 `0.6`（0.3/0.5 → 400）
  - `thinking: {"type":"disabled"}`：否则 reasoning 思考链占满 max_tokens 导致 content 为空
  - `max_tokens` ≤ 2048 稳定；更大偶发 400
  - 内容审核：涉及具体站名/敏感词会 400（content_filter）→ 上下文脱敏
- 工具: `tools/kimi_review.py`（读 `~/.dsh/.credentials.yaml` 的 KIMI_CODING_API_KEY）
- 用法: `python3 tools/kimi_review.py <prompt文件> <输出文件> [system提示文件]`

## 已执行任务
| 任务 | 产出 | 落地 |
|---|---|---|
| UI 设计架构审查 | ui_design_review.md | 设计系统 token/状态组件/移动端细化 已实现 |
| 后端框架审查 | framework_review.md | 待分期落地（见文件执行顺序） |
| 代码任务1: JSON 存储线程安全 + 统一错误模型 | 已集成 app.py（_json_lock + code 字段） | ✅ 已验证 |
| 代码任务2: 阅读器工具栏滚动隐藏/单击呼出 | 已集成 manga_reader.html | ✅ 已验证 |

## 20 轮全面优化记录（2026-08-22）

| 轮 | 内容 | 类型 |
|---|---|---|
| R1 | 小说阅读器：正文行高/段距、目录当前章高亮+已读✓、底部导航进度条(滚动%)、移动端底部sticky导航+目录底部抽屉+安全区、减少动效 | Kimi CSS+自实现JS |
| R2 | app.py 模块化第一步：8个纯工具函数+JSON读写/锁 → engine/app_utils.py（2600→2525行），55测试过 | 自行(基于Kimi审查) |
| R3 | 漫画详情页：主操作双列网格46px、次操作紧凑行、继续阅读accent强调、进度提示accent边框、章节头sticky | Kimi CSS |
| R4 | 漫画搜索缓存：q+source+page 5min TTL(后调10min)，_do_manga_search 可缓存，返回cached标记 | 自行 |
| R5 | 首页搜索：输入防抖500ms自动搜、输入框spinner、搜索中覆盖层(深色)、空态引导 | Kimi方案落地 |
| R6 | 任务中心：tr状态色条(蓝/橙/绿/红/灰)、圆点着色、进度条流动高光/完成绿/错误红、按钮手机等分 | Kimi CSS |
| R7 | API参数校验集中：_safe_int_arg/_safe_str_arg，漫画/小说搜索接入(非法回退+限长) | 自行 |
| R8 | 书库状态徽标：漫画(已下载/下载中/异常/在线)、小说(完结/失败/下载中)角标 | Kimi CSS |
| R9 | 请求日志+request-id：before/after_request [req]日志(static跳过)、404/500/兜底带request_id、未捕获异常统一JSON | Kimi产出 |
| R10 | 漫画网格：封面cover-wrap+失败📕占位、悬停上浮、翻页页码组(当前±2+省略)、active高亮 | Kimi方案 |
| R11 | 下载管理器：start幂等(排队/暂停合并章节、重启保留封面、锁外kick)、缓存头校验(JPEG/PNG/GIF/WEBP)+损坏重下+阈值4096 | Kimi产出 |
| R12 | 书源管理：卡片化、导入区虚线框、批量栏(全选+启用/停用/删除选中)，变量映射设计系统 | Kimi CSS+自实现JS |
| R13 | toast/动效统一：滑入动画、底部定位、移动端全宽安全区、详情页补toast替换alert | 自行 |
| R14 | 慢源降级：连续失败退避(冷却×fail_count≤5)、成功mark_ok清除、延迟滑动平均(α0.3)、锁内快照 | Kimi产出 |
| R15 | 详情SWR加固：并发刷新锁(同漫画单SWR线程)、失败2min冷却 | 自行 |
| R16 | 配置分层：WR_PLATFORM/sys.platform检测，android保守并发(搜索16/爬取2-4/超时9s)，手机端同步 | Kimi产出 |
| R17 | 空态/错误态统一：漫画搜索页统一empty/error-state类(空态引导+失败重试) | 自行 |
| R18 | 阅读器图片：img异步解码、前1+后2邻页预载、二次失败重试按钮 | Kimi产出 |
| R19 | 搜索并发调优：实测(小说缓存秒回/漫画6s/CPU0.1%)，漫画缓存TTL 300→600s | 自行 |
| R20 | 全站回归：8页面+5API全200，55测试通过 | 自行 |

**结果**：20 轮全部完成，代码质量与 UI 体验系统性提升；Kimi 协作流程成熟（tools/kimi_review.py）。
