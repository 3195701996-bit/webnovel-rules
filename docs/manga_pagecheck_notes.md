# 漫画缺页批量修复说明

## 背景(R69/R70)
- R60 时代章节图片提取用"向下滚动"触发站点逐张注入——滚动超过列表高 1/3 后
  注入即停, 长章节只能抓到前 ~2/3(实测 37 页章只存 25 张), 导致已下载漫画缺页。
- R69 修复提取: 在页面顶部反复触发 scroll 事件(注入条件恒成立), 直到
  comicCount 总页数全部注入。37 页章 25→37, 且更快(30-60s → 7-11s)。
- R70 缓存版本化: _imgs.json 缓存改为 {"v":2,"urls":[...]}; 旧裸数组缓存
  (R60 半截列表)一律不信任 → images() 会强制用 R69 全量提取重写。

## 全库检查修复
    ./venv/bin/python tools/fix_all_manga_pages.py
    # 可选: --only-source=copymanga_web 只查某源; --dry-run 只报告不补页
- 遍历 data/manga/downloads/* 下所有含 _info.json 的已下载漫画
- 每章: 用 R69 渲染取全量 URL(已有 v2 缓存则直接作基准, 免重复渲染)
  → 与本地文件数对比 → 缺页自动补下缺失序号(保留已有图不重下)
- 进度 data/manga/_cache/_pagecheck_progress.json 断点续跑; 双并发(thread-local 双 Chrome)
- 渲染失败不标记完成, 重跑会自动再试
