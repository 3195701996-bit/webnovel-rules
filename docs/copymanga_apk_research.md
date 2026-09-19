# copymanga 真实实现研究报告（反编译 Mihon 扩展 v1.4.82）

来源：反编译 `tachiyomi-zh.copymanga-v1.4.82.apk`（jadx）+ 服务器实测验证

## 1. 域名池（ApiDomainOption）—— App 失效根因
原 App 用 `api.copy202601.com` → **被 Cloudflare 530 拦截**（完全不可用）。
Mihon 扩展真实域名：
```
COPY1: api.mangacopy.com        国际服
COPY3: mapi.copy20.com          大陆专线1
COPY4: mapi.copy2000.site       大陆专线2
COPY5: api.2025copy.com         大陆专线3
COPY6: api.2026copy.com         大陆专线4
COPY7: api.copy3000.com         拷贝新站（DEFAULT 默认）
```
**验证**：`api.copy3000.com` + APP 签名头 + 版本 3.0.9 → 搜索 200（海贼王）✅

## 2. 请求头（COPY_MANGA_HEADER + 签名）
Mihon 扩展基础头：`Accept/Accept-Language/Origin: https://2025copy.com/Version(日期)/Region:0/Webp:0` + Firefox UA
**但服务器实测**：需要 APP 签名头才过版本校验（3.0.0+）：
```
User-Agent: COPY/3.0.9, source: copyApp, platform: 3,
referer: com.copymanga.app-3.0.9, version: 3.0.9,
deviceinfo/device/pseudoid, authorization: Token,
x-auth-timestamp + x-auth-signature(HMAC-SHA256), umstring
```
版本必须动态（官网 copy4000.com 发现），写死旧版 → 210"請升級"

## 3. 图片解析（ChapterDetail.toPageList）—— 与 App 实现等价确认
真实算法：`zip(contents, words)` → `sortedWith(words 升序)` → 按序取 contents
（我的实现 `ordered[words[i]] = urls[i]` 等价）
画质替换：`\d+(?=x\.(?:jpg|webp)$)` 替换为分辨率（默认 1500），保留扩展名

## 4. 210 处理（RateLimitInterceptor + ResponseErrorInterceptor）
- 限流域名（RATE_LIMIT_DOMAIN）间 sleep 限流
- 210/5xx → 换镜像域名重试（OkHttp Interceptor 捕获）

## 5. 章节参数
`/api/v3/comic/{cid}/group/{group}/chapters?limit=100&offset=&_update=true`

## 结论
App v4.2 已对齐：域名池（Mihon 真实）+ APP 签名头 + 动态版本 + 210 换域。
详情/章节当前电脑 IP 210 为临时标记（TTL≈1h），真机不受影响。

## 6. venera 框架集成（copymanga_source.js）
**venera 真实 JS API（parser.dart，与旧笔记的 comicInfo/chapters 不同）**：
- `search.load(keyword, options, page)` → `{comics:[{title,id,cover,subtitle,tags[],description}], maxPage}`
  （Comic.fromJson 字段：id 不是 comicUrl！cover 不是 coverUrl！tags 是数组不是逗号串！）
- `comic.loadInfo(comicId)` → ComicDetails JSON：title/subtitle/cover/description/
  `tags:{组名:[标签]}`（Map<String,List<String>>）/ `chapters:{uuid:标题}`（flat 或按组嵌套）/ comicId
- `comic.loadEp(comicId, epId)` → `{images:[...]}`（epId=章节 uuid）
- 注册：`class X extends ComicSource { name/key/version/minAppVersion/url = ...; search = {...}; comic = {...} }`
- 运行时：flutter_qjs(QuickJS) + init.js 提供全局 `fetch(url,{method,headers,body})`（browser-like，无 crypto）
- HMAC/BigInt/Base64 全部纯 JS 自实现（Node 验证 == crypto.createHmac）

**构建关键坑（macOS arm64）**：
1. **Dart pub get 找不到 git**：Flutter SDK 必须是 arm64 版（x64 版跑 Rosetta 时 CLT 无 x86_64 libxcrun → spawn git 失败）
2. **版本检测 0.0.0-unknown**：zip 含完整 .git 历史；pub get 会 fetch 远端 tags 并把本地 tag 移走 → 需 `git checkout -b stable` + `git tag -f 3.41.4 HEAD`（stable 频道跳过 fetchTags）
3. **flutter.sdk 中文路径**：gradle 用 Java Properties(Latin-1) 读 local.properties → 中文路径乱码 → Flutter SDK 必须放 ASCII 路径（如 ~/flutter_arm64）
4. **key.properties**：venera release 需 android/key.properties（storeFile/storePassword/keyAlias/keyPassword）
5. **NDK 28.0.13004108**：flutter_qjs 有 CMake native 编译，需 sdkmanager 装 NDK 28
