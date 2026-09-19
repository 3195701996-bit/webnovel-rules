// copymanga 源脚本（venera ComicSource 格式 v2）
// 基于反编译 Mihon 扩展 + 服务器验证的真实实现：
// 域名 api.copy3000.com + APP 签名头 + 动态版本 3.0.9
// API 形状对齐 venera parser.dart：
//   search.load(keyword, options, page) -> {comics:[{title,id,cover,subtitle,tags,description}], maxPage}
//   comic.loadInfo(comicId) -> ComicDetails JSON（chapters 为 {uuid: 标题} 映射）
//   comic.loadEp(comicId, epId) -> {images:[...]}

// ── 辅助函数（venera 运行时不自带 hmac/base64）──
function base64Decode(b64) {
    // 简单 Base64 解码（标准表）
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    let bytes = []
    b64 = b64.replace(/=+$/, "")
    for (let i = 0; i < b64.length; i += 4) {
        const c1 = chars.indexOf(b64[i]), c2 = chars.indexOf(b64[i+1]),
              c3 = b64[i+2] ? chars.indexOf(b64[i+2]) : 0,
              c4 = b64[i+3] ? chars.indexOf(b64[i+3]) : 0
        const b1 = (c1 << 2) | (c2 >> 4)
        const b2 = ((c2 & 15) << 4) | (c3 >> 2)
        const b3 = ((c3 & 3) << 6) | c4
        bytes.push(b1)
        if (i + 2 < b64.length) bytes.push(b2)
        if (i + 3 < b64.length) bytes.push(b3)
    }
    return bytes
}

function hmacSha256(keyBytes, msg) {
    // BigInt 纯 JS SHA-256 + HMAC（venera JS 运行时无 crypto，BigInt 避免 32 位溢出）
    const K = [0x428a2f98n,0x71374491n,0xb5c0fbcfn,0xe9b5dba5n,0x3956c25bn,0x59f111f1n,0x923f82a4n,0xab1c5ed5n,0xd807aa98n,0x12835b01n,0x243185ben,0x550c7dc3n,0x72be5d74n,0x80deb1fen,0x9bdc06a7n,0xc19bf174n,0xe49b69c1n,0xefbe4786n,0x0fc19dc6n,0x240ca1ccn,0x2de92c6fn,0x4a7484aan,0x5cb0a9dcn,0x76f988dan,0x983e5152n,0xa831c66dn,0xb00327c8n,0xbf597fc7n,0xc6e00bf3n,0xd5a79147n,0x06ca6351n,0x14292967n,0x27b70a85n,0x2e1b2138n,0x4d2c6dfcn,0x53380d13n,0x650a7354n,0x766a0abbn,0x81c2c92en,0x92722c85n,0xa2bfe8a1n,0xa81a664bn,0xc24b8b70n,0xc76c51a3n,0xd192e819n,0xd6990624n,0xf40e3585n,0x106aa070n,0x19a4c116n,0x1e376c08n,0x2748774cn,0x34b0bcb5n,0x391c0cb3n,0x4ed8aa4an,0x5b9cca4fn,0x682e6ff3n,0x748f82een,0x78a5636fn,0x84c87814n,0x8cc70208n,0x90befffan,0xa4506cebn,0xbef9a3f7n,0xc67178f2n]
    const H0 = [0x6a09e667n,0xbb67ae85n,0x3c6ef372n,0xa54ff53an,0x510e527fn,0x9b05688cn,0x1f83d9abn,0x5be0cd19n]
    const MASK = 0xffffffffn
    function sha256(bytes) {
        let H = H0.slice()
        const bitLen = BigInt(bytes.length * 8)
        const m = bytes.slice()
        m.push(0x80)
        while (m.length % 64 !== 56) m.push(0)
        for (let i = 7; i >= 0; i--) m.push(Number((bitLen >> BigInt(i * 8)) & 0xffn))
        for (let off = 0; off < m.length; off += 64) {
            const w = new Array(64).fill(0n)
            for (let i = 0; i < 16; i++) {
                w[i] = BigInt((m[off+i*4]<<24)|(m[off+i*4+1]<<16)|(m[off+i*4+2]<<8)|m[off+i*4+3]) & MASK
            }
            for (let i = 16; i < 64; i++) {
                const s0 = ((w[i-15] >> 7n) | (w[i-15] << 25n)) ^ ((w[i-15] >> 18n) | (w[i-15] << 14n)) ^ (w[i-15] >> 3n)
                const s1 = ((w[i-2] >> 17n) | (w[i-2] << 15n)) ^ ((w[i-2] >> 19n) | (w[i-2] << 13n)) ^ (w[i-2] >> 10n)
                w[i] = (w[i-16] + s0 + w[i-7] + s1) & MASK
            }
            let a=H[0],b=H[1],c=H[2],d=H[3],e=H[4],f=H[5],g=H[6],h=H[7]
            for (let i = 0; i < 64; i++) {
                const S1 = ((e >> 6n) | (e << 26n)) ^ ((e >> 11n) | (e << 21n)) ^ ((e >> 25n) | (e << 7n))
                const ch = (e & f) ^ ((~e) & g)
                const t1 = (h + S1 + ch + K[i] + w[i]) & MASK
                const S0 = ((a >> 2n) | (a << 30n)) ^ ((a >> 13n) | (a << 19n)) ^ ((a >> 22n) | (a << 10n))
                const maj = (a & b) ^ (a & c) ^ (b & c)
                const t2 = (S0 + maj) & MASK
                h=g; g=f; f=e; e=(d+t1)&MASK; d=c; c=b; b=a; a=(t1+t2)&MASK
            }
            H[0]=(H[0]+a)&MASK; H[1]=(H[1]+b)&MASK; H[2]=(H[2]+c)&MASK; H[3]=(H[3]+d)&MASK
            H[4]=(H[4]+e)&MASK; H[5]=(H[5]+f)&MASK; H[6]=(H[6]+g)&MASK; H[7]=(H[7]+h)&MASK
        }
        return H
    }
    function toBytes(str) { const out = []; for (let i = 0; i < str.length; i++) out.push(str.charCodeAt(i) & 0xff); return out }
    function hToBytes(h) { const out = []; for (const x of h) out.push(Number((x>>24n)&0xffn),Number((x>>16n)&0xffn),Number((x>>8n)&0xffn),Number(x&0xffn)); return out }
    function toHex(h) { return h.map(x => x.toString(16).padStart(8,'0')).join('') }
    const blockSize = 64
    let key = keyBytes.slice()
    if (key.length > blockSize) key = hToBytes(sha256(key))
    while (key.length < blockSize) key.push(0)
    const ipad = key.map(b => b ^ 0x36)
    const opad = key.map(b => b ^ 0x5c)
    const inner = hToBytes(sha256(ipad.concat(toBytes(msg))))
    return toHex(sha256(opad.concat(inner)))
}

class CopyMangaSource extends ComicSource {
    name = "拷贝漫画"
    key = "copymanga"
    version = "1.0.0"
    minAppVersion = "1.0.0"
    url = "https://api.copy3000.com"

    // 域名池（Mihon ApiDomainOption 真实域名）
    domains = [
        "api.copy3000.com",
        "api.mangacopy.com",
        "api.2025copy.com",
        "mapi.copy20.com",
        "mapi.copy2000.site"
    ]

    api = "api.copy3000.com"
    versionCode = "3.0.9"
    secret = "M2FmMDg1OTAzMTEwMzJlZmUwNjYwNTUwYTA1NjNhNTM="
    reqid = ""
    reqidTs = 0

    // HMAC-SHA256 签名
    hmacHex(ts) {
        const key = base64Decode(this.secret)
        return hmacSha256(key, ts)
    }

    // 请求头（APP 签名）
    headers() {
        const ts = String(Math.floor(Date.now() / 1000))
        const d = new Date()
        const dt = d.getFullYear() + "." + String(d.getMonth()+1).padStart(2,"0") + "." + String(d.getDate()).padStart(2,"0")
        return {
            "User-Agent": "COPY/" + this.versionCode,
            "source": "copyApp",
            "platform": "3",
            "referer": "com.copymanga.app-" + this.versionCode,
            "version": this.versionCode,
            "Accept": "application/json",
            "region": "0",
            "deviceinfo": "1234567V-1234",
            "dt": dt,
            "device": "AB1C.123456.789",
            "pseudoid": "abcdef1234567890",
            "authorization": "Token",
            "x-auth-timestamp": ts,
            "x-auth-signature": this.hmacHex(ts),
            "umstring": "b4c89ca4104ea9a97750314d791520ac"
        }
    }

    // 请求 API（210/530/网络错误 → 换域名重试）
    async get(path) {
        for (let i = 0; i < this.domains.length; i++) {
            const url = "https://" + this.api + path
            try {
                const resp = await fetch(url, { headers: this.headers() })
                const body = await resp.text()
                if (resp.status == 200 && !body.includes('"code":210')) {
                    return body
                }
                const idx = this.domains.indexOf(this.api)
                this.api = this.domains[(idx + 1) % this.domains.length]
            } catch (e) {
                const idx = this.domains.indexOf(this.api)
                this.api = this.domains[(idx + 1) % this.domains.length]
            }
        }
        throw "copymanga 全部域名失败"
    }

    // request_id（营销 API，5 分钟缓存）
    async getRequestId() {
        const now = Date.now()
        if (this.reqid && now - this.reqidTs < 300000) return this.reqid
        try {
            const resp = await fetch("https://marketing.aiacgn.com/api/v2/adopr/query3/?format=json&ident=200100001",
                { headers: this.headers() })
            const j = await resp.json()
            if (j.results && j.results.request_id) {
                this.reqid = j.results.request_id
                this.reqidTs = now
                return this.reqid
            }
        } catch (e) {}
        return ""
    }

    // ── 搜索 ──
    search = {
        load: async (keyword, options, page) => {
            const kw = encodeURIComponent(keyword)
            const body = await this.get("/api/v3/search/comic?limit=30&offset=" + (page - 1) * 30 + "&q=" + kw + "&q_type=")
            const j = JSON.parse(body)
            const list = (j.results || {}).list || []
            const comics = list.map(item => ({
                title: item.name || item.path_word,
                id: item.path_word,
                cover: item.cover || "",
                subtitle: (item.author && item.author[0]) ? item.author[0].name : "",
                tags: (item.theme || []).map(t => t.name),
                description: item.brief || ""
            }))
            return { comics: comics, maxPage: Math.ceil((j.results && j.results.total || 30) / 30) }
        }
    }

    // ── 详情 + 章节（comic.loadInfo）──
    comic = {
        loadInfo: async (comicId) => {
            const rid = await this.getRequestId()
            const body = await this.get("/api/v3/comic2/" + comicId + "?in_mainland=true&request_id=" + rid + "&platform=3")
            const j = JSON.parse(body)
            const comic = j.results.comic
            // 章节列表（default 分组，接口倒序 → 正序）
            let chapters = {}
            try {
                const cb = await this.get("/api/v3/comic/" + comicId + "/group/default/chapters?limit=100&offset=0&in_mainland=true&request_id=" + rid)
                const cj = JSON.parse(cb)
                const list = (cj.results || {}).list || []
                list.slice().reverse().forEach(ch => {
                    if (ch.uuid) chapters[ch.uuid] = ch.name || ""
                })
            } catch (e) {}
            return {
                title: comic.name,
                subtitle: (comic.author && comic.author[0]) ? comic.author[0].name : "",
                cover: comic.cover || "",
                description: comic.brief || "",
                tags: {
                    "题材": (comic.theme || []).map(t => t.name),
                    "状态": comic.status && comic.status.name ? [comic.status.name] : [],
                    "类型": (comic.type || []).map(t => t.name)
                },
                chapters: chapters,
                comicId: comicId,
                url: "copymanga://comic/" + comicId
            }
        },

        // ── 章节图片（words 重排 + 画质替换）──
        loadEp: async (comicId, epId) => {
            const rid = await this.getRequestId()
            const body = await this.get("/api/v3/comic/" + comicId + "/chapter2/" + epId + "?in_mainland=true&request_id=" + rid)
            const j = JSON.parse(body)
            const chapter = j.results.chapter
            const contents = (chapter.contents || []).map(c => c.url)
            const words = chapter.words || []
            // words 重排：words[i] 是正确页码
            const ordered = new Array(contents.length).fill("")
            for (let i = 0; i < words.length; i++) {
                const pos = words[i]
                if (pos >= 0 && pos < ordered.length) {
                    ordered[pos] = contents[i]
                }
            }
            const images = ordered.filter(u => u).map(u => {
                // 画质替换：c\d+x → c1500x.webp（对齐 Mihon ResolutionOption 1500）
                return u.replace(/([./])c\d+x\.[a-zA-Z]+$/, "$1c1500x.webp")
            })
            return { images: images }
        }
    }
}
