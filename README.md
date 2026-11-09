# OCS AI Tiku

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

把 [OCS 网课助手](https://github.com/ocsjs/ocsjs) 的搜题请求转发给 AI，
返回脚本能**精确命中**的答案。不用维护本地题库，不依赖任何第三方题库网站。

---

## 为什么需要它

OCS 脚本的答案匹配是**字符串相似度检索**，不是语义理解：

- 答案必须和页面选项文字对齐，否则 Dice 相似度 `<= 0.6` 直接判"不匹配"
- 字母答案（A/B/C/D）只是最低优先级的兜底
- 填空题要求**答案数量 == 空位数**，差一个就整题放弃

所以本服务**不会把 AI 说的话原样返回**：

```
AI 只返回选项字母  letters: ["B"]
        ↓
服务端映射回「页面上那一行选项的原始文本」  "B. 叶绿体"
        ↓
脚本 answerNormalizedMatch 拿到 level=2 精确匹配
```

映射在服务端完成，**绕开 0.6 阈值**。这是整套方案能用的前提，也是 `normalize.py` 存在的唯一理由。

---

## 快速开始

```bash
git clone https://github.com/point-zip/ocsai.git
cd ocsai

# 用 uv（推荐）
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt   # Windows: .venv/Scripts/python.exe

# 或 pip
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

配置：

```bash
cp .env.example .env
```

编辑 `.env`，至少填这三项：

```ini
OCS_API_KEY=sk-xxxxxxxx
OCS_BASE_URL=https://api.openai.com/v1
OCS_MODEL=gpt-4.1-mini
```

启动：

```bash
.venv/bin/python server.py     # Windows: .venv\Scripts\python.exe server.py
```

看到下面这行就成功了：

```
Uvicorn running on http://127.0.0.1:6767
```

> 默认只监听回环，本机脚本即可使用。要让别的机器访问，**先设 `OCS_API_TOKEN`**，
> 再把 `OCS_HOST` 改成 `0.0.0.0`。启动日志会对此给出警告。

验证：

```bash
curl http://127.0.0.1:6767/health
```

---

## 接进脚本

打开 OCS 面板 →「通用 - 全局设置」→「题库配置」→ 粘贴 `ocs-config.json`：

```json
[
  {
    "name": "AI 搜题",
    "homepage": "http://127.0.0.1:6767/",
    "url": "http://127.0.0.1:6767/solve",
    "method": "post",
    "type": "GM_xmlhttpRequest",
    "contentType": "json",
    "data": {
      "title": "${title}",
      "options": "${options}",
      "type": "${type}",
      "images": { "handler": "return (env) => env.images || []" }
    },
    "handler": "return (res) => { if (!res || !res.ok || !res.answer) return []; return [[res.question, res.answer]]; }"
  }
]
```

底部应显示 **连接成功（200）**。

### 三个必须注意的地方

**1. `images` 必须用 `handler` 形式**

```json
"images": { "handler": "return (env) => env.images || []" }
```

不能写 `"${images}"`。脚本的 `resolvePlaceHolder` 是 `String.replace`，
数组会被 `toString()` 拼成逗号串（一堆 base64 挤成一行），服务端必然解析失败。

**2. 必须用 `GM_xmlhttpRequest`，不能用 `fetch`**

智慧树是 HTTPS 页面，请求本地 HTTP 服务时：

| 方式 | 结果 |
|---|---|
| `fetch` | 混合内容拦截 + CORS 双重失败 |
| `GM_xmlhttpRequest` | 走脚本管理器，绕过限制 ✅ |

脚本 `@connect` 已含 `127.0.0.1` / `localhost`，开箱可用。
**换成自己的域名**时需要在该脚本头部 `@connect` 加上域名，或改用 `fetch`（本服务已开 CORS）。

**3. 服务必须同时响应 GET/HEAD**

OCS 的连接探测 `probeAnswerer` 先发 **HEAD**，遇到 `405/501` 才退回 **GET**，
且只认 `200-399`。只注册 `POST /solve` 会让卡片一直显示"连接失败（405）"——
虽然答题（POST）其实正常。本服务已用 `@app.api_route("/solve", methods=["GET","HEAD"])` 处理。

---

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `OCS_API_KEY` | — | **必填**，AI 的 key |
| `OCS_BASE_URL` | `https://api.openai.com/v1` | 服务根地址，末尾不带 `/responses` |
| `OCS_MODEL` | `gpt-4.1-mini` | 需支持 Responses API；图片题需支持视觉输入 |
| `OCS_REASONING_EFFORT` | `low` | 思考等级：`low` / `medium` / `high` |
| `OCS_AI_TIMEOUT` | `45` | 单次 AI 超时（秒），**必须 < 58** |
| `OCS_USE_JSON_SCHEMA` | `true` | 网关不支持结构化输出时设 `false` |
| `OCS_HOST` | `127.0.0.1` | 监听地址，默认仅本机 |
| `OCS_PORT` | `6767` | 端口 |
| `OCS_API_TOKEN` | 空 | 访问令牌，对外开放时**必须**设置 |
| `OCS_MIN_CONFIDENCE` | `0.55` | 低于该置信度直接 `ok:false`，宁可不填也不填错 |
| `OCS_MAX_IMAGES` | `6` | 单题最多图片数，超限整体拒绝 |
| `OCS_MAX_IMAGE_KB` | `4096` | 单张图片大小上限（KB），超限整体拒绝 |
| `OCS_CACHE_ENABLED` | `true` | 答案缓存（SQLite） |
| `OCS_CACHE_PATH` | `cache.sqlite3` | 缓存文件路径，相对路径基于项目目录 |
| `OCS_CACHE_TTL_DAYS` | `30` | 缓存有效期 |
| `OCS_CACHE_MAX_ROWS` | `50000` | 缓存上限，超出按命中数淘汰 |

### 缓存与选项乱序

缓存键由 **题目 + 选项归一化指纹 + 题型 + 图片内容摘要** 组成。

**选项指纹与顺序无关**：归一化（去 `A.` 前缀、去标点、转小写）后排序再算哈希。
所以页面把 ABCD 打乱不会导致缓存失效——同一道题打乱前后命中同一条缓存。

**命中也不会答错**：缓存里存的是选项原文（如 `B. 叶绿体`），而脚本
`resolveSingle` 是拿文本归一化比对，与字母位置无关。打乱后页面变成
`A.丙 B.甲 C.叶绿体`，脚本依然精确落到「叶绿体」那一行。

**图片参与键**：同一题干、不同配图的题不会共享答案，避免错误答案被固化一整个 TTL。

已知边界：位置相关选项（「以上都是」「A 和 B 均正确」）打乱后语义本身就会变，
这类题命中缓存可能返回与原顺序绑定的答案。

### 关于 60 秒硬超时

脚本里 `AnswerWrapperHandlerConfig.timeout_seconds = 60` 是**硬编码**的
（`ocs.user.js` 第 4385 行）。超过 60 秒脚本直接放弃这道题。

所以 `OCS_AI_TIMEOUT` 必须留足余量。`reasoning_effort=low` 一般 3-8 秒返回，45 秒够用。
若需更长推理，只能改脚本那一行，或把超时压到 20 秒让脚本跳过（下一轮靠缓存补）。

---

## 题型处理

| 题型 | 返回格式 | 脚本侧 |
|---|---|---|
| 单选 `single` | 选项原文，如 `B. 叶绿体` | `resolveSingle` 精确匹配 |
| 多选 `multiple` | 选项原文用 `===` 连接 | `resolveMultiple` 逐个匹配 |
| 判断 `judgement` | `对` / `错` | `resolveJudgement` 词表匹配 |
| 填空 `completion` | **JSON 数组** `["线粒体","叶绿体"]` | `splitAnswer` 先试 `JSON.parse` |

**填空题的已知限制**：脚本在填空题时把 `options` 置成空串
（`optionsTransform` 里 `if (ctx.type === "completion") return ""`），
服务端**拿不到空位数**，只能靠 AI 从题干数 `____` 推断。
数错了脚本判 `finish:false`，该题留空，不影响其他题。

---

## 部署到服务器 + nginx

```ini
OCS_HOST=127.0.0.1
OCS_PORT=6767
OCS_API_TOKEN=换成随机串
```

只监听回环，由 nginx 对外：

```nginx
location /ocs-solve {
    proxy_pass         http://127.0.0.1:6767/solve;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_read_timeout 65s;
    proxy_send_timeout 65s;
}
```

> `proxy_read_timeout` 必须 > 60s，nginx 默认值会和脚本超时撞车。

带 token 时脚本配置加请求头：

```json
{
  "url": "https://你的域名/ocs-solve",
  "method": "post",
  "type": "GM_xmlhttpRequest",
  "contentType": "json",
  "headers": { "X-API-Token": "你的token" },
  "data": { "...": "..." }
}
```

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/solve` | 脚本调用 |
| GET/HEAD | `/solve` | 连接探测（返回 200） |
| GET | `/health` | 健康检查 + 统计 |
| GET | `/stats` | 缓存与调用统计 |

`POST /solve` 请求：

```json
{
  "title": "题干",
  "options": "A. 甲\nB. 乙",
  "type": "single",
  "images": ["data:image/png;base64,..."]
}
```

响应：

```json
{
  "ok": true,
  "question": "题干",
  "answer": "B. 乙",
  "type": "single",
  "confidence": 0.95,
  "cached": false
}
```

`ok:false` 时脚本显示"题库没搜索到答案"，**不会乱填**。

---

## 从旧版本升级（缓存迁移）

缓存键的算法变过一次：

| 版本 | 缓存键 |
|---|---|
| 旧 | `sha256(题目 + 原始选项列表 + 题型)` |
| 现在 | `sha256(题目 + 选项**顺序无关**归一化指纹 + 题型 + **图片摘要**)` |

所以**旧库里的行在新代码下永远命中不了**——它们不是数据，是死行：
算不出同样的键，却照占 `OCS_CACHE_MAX_ROWS` 配额；而且旧行的 `hits`
可能很高，会长期赖在淘汰队列（`ORDER BY hits ASC`）尾部清不掉。

不迁移也能正常跑（新库从零开始），只是那批旧题会被重新问一遍 AI。
要保留就把键重算一遍：

```bash
# 1. 先干跑，只看统计
python migrate_cache.py --src /path/to/old/cache.sqlite3 --dry-run

# 2. 生成新库，旧库原封不动
python migrate_cache.py --src /path/to/old/cache.sqlite3 --verify
#    -> 写出同目录下 cache.migrated.sqlite3，并抽样走一遍 cache.get 自检

# 3. 确认后替换，或改 OCS_CACHE_PATH 指过去
mv cache.migrated.sqlite3 cache.sqlite3
```

原地迁移也可以（会自动备份成 `cache.sqlite3.bak-<时间戳>`）：

```bash
python migrate_cache.py --src cache.sqlite3 --in-place --verify
```

几个要点：

- **脚本默认只读打开源库**。读写模式会让 SQLite 在关闭时做 autocheckpoint，
  把 `-wal` 合并进主库——等于顺手改了你的旧库。只读能读到 WAL 里的数据，且不动文件。
- **同题不同选项顺序的旧行会合并成一条**（这本来也是新键的意义所在）。
  冲突时保留 `created_at` 较新的 payload，`hits` 取较大值，重跑幂等。
- **旧键不含图片信息**，迁移后所有行都按「无图」记录。旧库里若混有图片题的缓存，
  会以无图身份存在，理论上可能与同题干的纯文字题撞键。保守做法是加
  `--skip-image-suspects`，按题干关键词（如图/下图/图中…）跳过这批行。

---

## 测试

```bash
python test_resolve.py      # 答案归一化（7 用例，纯逻辑，不需要网络）
python test_e2e.py          # 端到端（自动起 mock AI，16 项断言）
python test_script_sim.py   # 全链路：模拟脚本占位符替换 + handler + resolveSingle
```

三个都应是 `0 failed`。

`test_script_sim.py` 会用 `node` 真实执行脚本里的 `Function(handler)()`，
确保配置不是"看起来对"而是真能跑通。**需要装 Node.js**。

---

## 排错

| 现象 | 原因 |
|---|---|
| 卡片显示"连接失败（405）" | 服务没注册 GET/HEAD 探测路由 |
| 卡片显示"连接失败（401）" | `OCS_API_TOKEN` 设了但脚本没带 `X-API-Token` 头 |
| 提示"你还没设置题库" | 配置没粘进去，或 JSON 格式错 |
| 一直"题库没搜索到答案" | `/solve` 返回 `ok:false`，看服务端日志 |
| 答案错位（选了别的选项） | 选项含特殊字符，检查 `normalize.py` 的 `strip_prefix` |
| 填空题全空 | AI 数错空位数，看题干 `____` 的写法 |
| 请求 60s 超时 | `OCS_AI_TIMEOUT` 太大或模型太慢 |
| 一直返回 `index_out_of_range` | AI 给出了页面不存在的选项字母，看日志里的 `letters=` |
| 一直返回 `low_confidence` | AI 对该题没把握，可调低 `OCS_MIN_CONFIDENCE` |
| 返回 `too_many_images` / `image_too_large` | 图片超限，调 `OCS_MAX_IMAGES` / `OCS_MAX_IMAGE_KB` |
| 图片题搜不到 | 模型不支持图片，或脚本「图片题优化」没开 |
| `Mixed Content` 报错 | 用了 `fetch` 而不是 `GM_xmlhttpRequest` |

服务端日志打印每题的类型、选项数、图片数和题干前 60 字，对着看基本能定位。

---

## 项目结构

```
ocs-ai-tiku/
├── server.py           FastAPI 入口：/solve /health /stats
├── ai_client.py        OpenAI Responses API 调用
├── normalize.py        答案归一化（核心）
├── cache.py            SQLite 答案缓存
├── config.py           .env 配置加载
├── migrate_cache.py    旧缓存库 key 迁移
├── ocs-config.json     粘贴进脚本的题库配置
├── test_resolve.py     归一化单元测试
├── test_e2e.py         端到端测试（mock AI）
├── test_script_sim.py  脚本全链路模拟
├── requirements.txt
├── pyproject.toml
├── .env.example
└── LICENSE
```

---

## 说明

本项目只做**搜题协议适配**，不含任何题库数据，也不修改 `ocs.user.js`。

AI 返回的答案不保证正确，尤其是需要推理的题目。请自行判断结果是否可用。

## License

[AGPL-3.0](LICENSE)