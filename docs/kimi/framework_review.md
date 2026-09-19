## Flask 单体架构审查清单

---

### 1. 单体拆分建议（模块/蓝图划分）

| 优先级 | 问题 | 改法 |
|:---|:---|:---|
| **P0** | `app.py` 60路由裸奔，循环导入风险 | 按领域拆4个Blueprint：`/api/v1/search`, `/api/v1/crawl`, `/api/v1/rules`, `/api/v1/system`；`app.py` 仅保留工厂函数 `create_app()` |
| **P0** | engine层四合一（规则/爬虫/下载/搜索）强耦合 | 拆4个独立包：`engines/rules/`, `engines/crawlers/`, `engines/download/`, `engines/search/`；统一 `BaseEngine` 抽象基类 |
| **P1** | JSON文件存储无schema，多进程写冲突 | 存储层抽象：`storage/` 包，实现 `JSONStore`/`MemoryStore`/`AndroidSQLiteStore` 三后端；Android环境自动路由到SQLite |
| **P1** | 无依赖注入，单元测试需mock全局 | 引入 `Flask-Injector` 或手写容器；`engine` 实例由工厂注入，禁止模块级单例 |
| **P2** | 爬虫适配器硬编码多源 | `adapters/` 目录 + `BaseAdapter.register` 插件机制；源配置化，新增源无需改代码 |

**目录结构目标：**
```
backend/
├── app/                 # Flask应用壳
│   ├── __init__.py      # create_app()
│   └── blueprints/      # 4个蓝图
├── engines/             # 4大引擎（纯Python，无Flask依赖）
├── storage/             # 存储抽象
├── adapters/            # 爬虫适配器插件
├── tasks/               # 任务/状态机定义
└── config/              # 多环境配置
```

---

### 2. 并发任务模型问题

| 优先级 | 问题 | 改法 |
|:---|:---|:---|
| **P0** | 线程池裸用 `concurrent.futures`，任务状态黑盒 | 显式状态机：`PENDING → RUNNING → SUCCESS/FAILED/CANCELLED`；`TaskManager` 类维护 `WeakValueDictionary[id, TaskState]` |
| **P0** | 下载任务无幂等，重复提交=重复下载 | 任务ID = `hash(url + params)`；`RUNNING` 状态拒绝重复提交；`SUCCESS` 返回缓存结果 |
| **P0** | 线程安全：JSON文件并发写损坏 | 存储层加 `threading.RLock`；或Android切SQLite（文件锁）；关键区最小化 |
| **P1** | 爬虫无背压，线程池队列无限增长 | 有界队列 `maxsize=100`；队列满时 `submit()` 抛 `QueueFull` 或降级同步执行 |
| **P1** | 任务取消机制缺失，Activity销毁泄漏 | `TaskFuture` 包装 `concurrent.futures.Future`；暴露 `cancel()` → 设置 `Event` 标志；爬虫适配器轮询标志位 |
| **P2** | 无任务超时，僵尸线程堆积 | 下载任务 `timeout=30s`；规则引擎 `timeout=5s`；超时强制 `CANCELLED` |

**关键代码模式：**
```python
# 幂等提交
def submit_task(self, task_id: str, fn, *args) -> TaskFuture:
    with self._lock:
        if existing := self._tasks.get(task_id):
            if existing.state in (RUNNING, SUCCESS):
                return existing  # 幂等返回
        # 真正提交...
```

---

### 3. API 设计问题

| 优先级 | 问题 | 改法 |
|:---|:---|:---|
| **P0** | 无统一错误模型，客户端无法解析 | 强制 envelope：`{"code": "SEARCH_TIMEOUT", "message": "...", "data": null, "request_id": "uuid"}`；HTTP状态码仅用于网络层（200/400/500） |
| **P0** | 输入校验散落 `if/else` | 全量接 `Pydantic`/`Marshmallow`；Flask用 `flask-pydantic` 装饰器；校验失败统一 `422` + 字段级错误 |
| **P1** | REST资源混乱，动词嵌在URL | `/crawl` → `POST /api/v1/tasks` (body: `{"type": "crawl"}`)；`GET /api/v1/tasks/{id}` 查状态；`DELETE` 取消 |
| **P1** | 无API版本，Android/网页升级不同步 | URL前缀 `/api/v1/`；`Accept: application/vnd.myapp.v1+json` 备选；废弃API保留2版本 |
| **P1** | 搜索聚合无分页/流式，大数据卡死 | 游标分页 `?cursor=xxx&limit=20`；或SSE `text/event-stream` 流式返回（Chaquopy需评估，优先分页） |
| **P2** | 无请求ID，跨层问题难追踪 | `X-Request-ID` 透传；Flask `g.request_id`；日志/错误全量携带 |

---

### 4. 可维护性（测试/配置/日志）

| 优先级 | 问题 | 改法 |
|:---|:---|:---|
| **P0** | 配置硬编码，Android/网页环境切换困难 | `config/` 下 `BaseConfig`, `WebConfig`, `AndroidConfig`；运行时 `create_app(config_name=os.getenv("APP_ENV", "web"))` |
| **P0** | 无结构化日志，grep困难 | `structlog` 或标准 `logging.JSONFormatter`；必含字段：`timestamp`, `level`, `logger`, `request_id`, `event` |
| **P1** | 零单元测试，引擎层无法独立测 | `engines/` 纯Python零Flask依赖；`pytest` + `pytest-mock`；爬虫适配器用 `responses`/`vcrpy` 录播 |
| **P1** | 集成测试需启完整Flask，慢且脆 | `tests/conftest.py` 提供 `client` fixture；引擎层用 `fakeredis`/`tmp_path` 假存储；仅CI跑全量集成 |
| **P2** | 日志级别全局，生产DEBUG淹没 | 按模块独立：`engines.download=INFO`, `engines.rules=DEBUG`；环境变量覆盖 |

---

### 5. 嵌入式部署约束（Chaquopy/Android）

| 优先级 | 问题 | 改法 |
|:---|:---|:---|
| **P0** | 依赖包体积过大，APK超限 | `requirements.txt` 分 `base.txt`/`web.txt`/`android.txt`；Android排除：`flask[async]`, `gunicorn`, `gevent`；用 `pipdeptree` 审计 |
| **P0** | Chaquopy线程模型与Python GIL冲突 | 禁止 `ProcessPoolExecutor`（Chaquopy不支持）；`ThreadPoolExecutor` max_workers ≤ 4；CPU密集任务抛Java层处理 |
| **P0** | JSON文件存储在Android无权限/性能差 | 运行时检测 `sys.platform == "android"` → 路由到 `SQLiteStore`（Chaquopy可访问Android SQLite）；`storage/` 工厂自动切换 |
| **P1** | 启动耗时，Flask全量导入慢 | `app.py` 延迟导入：`@app.before_first_request` 或首次API调用时初始化引擎；Android优先预加载 `search` 引擎 |
| **P1** | 内存泄漏，Activity旋转重建 | `Application` 级单例持有 `FlaskApp`（非Activity）；`onDestroy()` 仅取消关联任务，不销毁引擎 |
| **P2** | 无Android生命周期感知 | 暴露 `pause()`/`resume()` API 到Java；下载任务前台优先，后台降级为单线程 |

---

## 执行顺序建议

```
Phase 1（2周）: P0全清 → 蓝图拆分 + 状态机 + 错误模型 + 存储抽象 + Android存储切换
Phase 2（1周）: P1高价值 → 依赖注入 + Pydantic + 测试骨架 + 配置重构
Phase 3（1周）: P2优化 → 插件适配器 + 流式API + 生命周期感知
```