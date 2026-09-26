# FiimeOPC-Register-Tool

[opc.fiime.cn](https://opc.fiime.cn/) 账号自动注册 + API Key 提取工具（**纯协议链路，无浏览器**）。

跑通 **注册 → 从 CF Worker 邮箱提取激活链接 → 邮件激活 → 登录 → 创建 API Key → 校验可调用 → 入库** 全链路，
做成**两段式并发流水线**，按**出口 IP** 记账以绕开站点最硬的限制：**IP 维度的每日注册配额**。

实测（2026-09-26，4 个出口 / 4 路并发）：

| 指标 | 值 |
|---|---|
| 单账号（顺序，含收信等待） | **12.7 ~ 17.2s** |
| 批量关键路径（全部 key 建出） | **23.1s / 4 个 = 5.8s / 账号** |
| 注册内部阶段（最慢账号） | 建邮箱 0.67s · 查重 0.70s · **注册请求 1.98s** · **收信 9.26s（5 次轮询）** · 激活 2.29s |
| 注册速率上限 | 每出口 **62s 1 个**（服务端硬限制 60s） |
| 注册日配额上限 | **每出口 IP 5 个 / 天**（实测，非文档） |

> **本仓库是公开的。** 凭据只进 `.env`（代码里一律 `os.getenv()`，缺项由 `config.validate()` 在入口报错）。
> `.env` / `ledger/` / `.workbuddy-ai/` 全部 gitignored —— 台账**必须**存明文（key 只在创建时返回一次），
> 所以整个目录不进仓库。
>
> **基础设施标识按家族占位化**：出口 IP 用 RFC 5737 保留段 `203.0.113.x`，
> 邮箱域名用 `example-mail.test`。提交前过闸门：
>
> ```bash
> python tools/gates/check_leaks.py            # 扫全仓（从 .env 读真实标识，不硬编码清单）
> python tools/gates/check_leaks.py --selftest # 证明闸门**真的会拦**（变异测试）
> ```
>
> 闸门的清单**来自 `.env`** —— 把要保护的字符串硬编码进闸门脚本，等于让闸门自己变成泄漏源。
>
> ⚠ `run.py` 的起飞前预检表**会**打印出口 IP（那是它的用途：按出口看剩余额度）。
> 这段输出**不要粘进任何仓库、issue 或对话**。

---

## 快速开始

```bash
# 1. 依赖（只有 requests —— 见「为什么没有浏览器」）
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

# 2. 配置凭据
cp .env.example .env
#   编辑 .env：至少填 FO_WORKER_BASE / FO_WORKER_ADMIN_TOKEN / FO_WORKER_DOMAIN
#   每个变量的含义与实测值都写在 .env.example 的注释里
#   想挂推荐关系就再加一行 FO_INVITE_CODE=XXXXXXXX（**统一注入**，之后每批
#   注册都自动带；见下面「邀请码」一节）

# 3. 只探测，不动手（推荐第一步）
python run.py probe

# 4. 跑 1 个账号（直连，走本机出口）
python run.py

# 5. 批量 4 个（默认走槽位池，每出口一个）
python run.py --count 4 --workers 4

# 6. 复验台账里所有 key 是否还活着（**不消耗注册配额**）
python tools/ops/check_keys_alive.py --all

# 7. 跑批崩了、key 没落盘？从线上账号捞回来（**不消耗注册配额**，默认干跑）
python tools/ops/recover_keys.py
python tools/ops/recover_keys.py --apply
```

---

## 全链路

| 阶段 | 端点 | 消耗注册配额 | 用槽位 |
|---|---|---|---|
| ① 建临时邮箱 | CF Worker `POST /api/new-address` | — | 否 |
| ② 查用户名可用 | `GET /api/auth/check-username?username=` | — | 否 |
| ③ **注册** | `POST /api/auth/register` | **是（IP 维度）** | **是** |
| ④ 收信取激活链接 | CF Worker `GET /api/inbox?email=` | — | 否 |
| ⑤ 激活 | `POST /api/auth/activate` `{"token": "<hex>"}` | 否 | 否 |
| ⑥ 登录 | `POST /api/auth/login` | **否** | 否 |
| ⑦ 建 API Key | `POST /api/model-service/keys` | 否 | 否 |
| ⑧ 校验可调用 | `GET /api/model-service/v1/models` | 否 | 否 |

**只有 ③ 消耗配额，且只有 ③ 需要独占出口。** 这是整个架构的地基：⑥⑦⑧ 全部**直连**，
不占槽位、不排队，所以流水线能拆成"注册侧串行（受配额约束）+ 登录侧并发（不受约束）"两段。

> 实测证据：账号用 slot3（出口 A）注册、直连（出口 B）登录建 key，全程正常 ——
> 登录/建 key 与注册出口无关。
>
> 完整协议细节（请求体、响应形状、错误码）见 [`docs/protocol.md`](docs/protocol.md)。

---

## 关键设计

### 1. 两段式流水线：生产者持租约，消费者直连

```
生产者池（≤4 并发，每出口一个）        消费者池（workers 路并发，直连）
  mailbox → username → [出口闸门] → register → 收信 → activate
                                                    ↓ 账号入队
                                          login → create_key →（末尾统一）verify
```

- **生产者**持槽位租约，跑完 ③⑤ 立刻归还 —— 租约只覆盖"必须独占出口"的那一段。
- **消费者**从队列取账号，用独立直连客户端跑 ⑥⑦。`OpcClient` 一个实例 = 一个 Session，
  **跨账号复用会静默串号**（服务端只看到"带着 A 的 cookie 去激活 B"，报的错跟 cookie 无关）。
- 校验**挪到流水线末尾统一做**：不挡关键路径，且并发。

### 2. 两层限流，处置**完全相反**（本项目最重要的判据）

站点对注册返回两种 `429`，文案不同、含义不同、正确处置相反：

| 响应体 | 类别 | 含义 | 正确处置 |
|---|---|---|---|
| `注册过于频繁，请一分钟后再试` | `LIMIT_RATE` | 60s/IP 瞬时速率 | 同出口**退避重试** |
| `该 IP 今日注册数量已达上限，请明日再试或联系管理员` | `LIMIT_IP_QUOTA` | **IP 维度日配额** | 标记该出口**死亡**，换 IP |

判据**只有一个入口** `opc.classify_429()`，调用方不去 match 文案 —— 否则文案一改行为就悄悄变了，
而 diff 里看不出来。未知文案归到 `LIMIT_RATE`（代价不对称：误判成 ip_quota 会白晾一个干净出口 6 小时；
误判成 rate 只是多退避一次）。两个关键词同时出现时**配额优先**。

### 3. 槽位代理池：分配单位 ≠ 约束单位

- 一个槽位 = 一个本地 mihomo 监听端口 = 一个**固定出口 IP**。
- 🔴 **记账维度是出口 IP，不是槽位下标**。实测 6 个槽位只有 **4 个不同出口** ——
  按下标记账会把额度算重、并发开高（4 个 IP 只能并发 4 个生产者，开 6 个有两个必然撞墙）。
- 🔴 **同一出口 IP 必须互斥**（规则 4）。池子按"累计租用次数最少"挑选，并排除"出口 IP 已被占用"的槽位。
- 出口被判死 → `report_banned` 指数退避冷却（120s → 6h 上限，落盘持久化）；
  网络类失败 → `report_failed` 只冷却 20s。**混成一种**的后果：要么把健康出口白晾 2 分钟，
  要么把已判死的出口 20s 后重投继续撞墙。
- `settle_lease()` 的判据走**结构化字段** `error_kind_of()`，不搜 `error` 文本 ——
  我们自己拼的守卫文案里也含"已达上限"那几个字，文本匹配会把"一个请求都没发的主动中止"
  读成"服务端确认的封禁"。

### 4. 为什么没有浏览器（与参考项目最大的架构差异）

参考项目 `Intern-Register-Tool` 需要 playwright + cryptography。本链**不需要**，全部实测过：

- 注册**无验证码** —— `GET /api/settings/public` 返回 `captchaEnabled: false`；
- 登录**无验证码、无 2FA** —— `POST /api/auth/login` 直接返回 `user`；
- 密码是**明文 POST** —— 站点前端不做 RSA 加密，请求体里就是原密码。

所以依赖只有 `requests`。代价是**对站点改版脆弱**：一旦开启验证码，本链立刻失效
（`config.validate()` 会在入口检查 `captchaEnabled`，开启时直接报错而不是静默跑失败）。

---

## 邀请码（推荐关系）

一行配置，**统一注入**到之后每一批注册：

```bash
# .env
FO_INVITE_CODE=XXXXXXXX        # 8 位大写字母数字；留空 = 不带码
```

命令行只在**临时换码**时才需要：`python run.py --count 4 --invite-code XXXX`
（传空串 `--invite-code ''` 可临时关掉 `.env` 里的码）。

### 它是怎么进去的

请求体字段名是 **`inviteCode`**（从站点 SPA 的 Register chunk 里读出来的：
`inviteCode: n.inviteCode || void 0`）。空值时**整个键省略**，不是空串 ——
本实现与前端一致。码会先经 `config.normalize_invite_code()`（去空白 + 转大写）
+ 格式校验（≤32 字符、纯字母数字），不合法就**在起飞前**报错退出。

### 🔴 最要紧的一条：服务端不校验码是否存在

| 你发出去的码 | 服务端返回 | 推荐关系 |
|---|---|---|
| 有效码 | `200` + `invitedAt=<时间戳>` | ✅ 成立 |
| 形状合法但不存在（如 `ZZZZZZZZ`） | `200`，账号照建，**零报错** | ❌ 静默不成立 |
| 带码时注册响应的 `tokenReward` | **`0`**（正常！奖励延后到激活） | — |

⇒ **"码写错了"不可能靠注册失败发现**。本工具的判据链：

1. 起飞前打印 `🎟  邀请码：XXXX（来源 FO_INVITE_CODE）`；
2. 每个账号把 `invite_code` / `invited_at` 落进台账；
3. 报告末尾单独一节：

```
邀请码（推荐关系）：3/3 个已确认受邀
  本次使用：ZQ9QEVJJ（3 个账号发出了码，0 个未走完注册+激活、无法判断）
  ✓ 发出的码全部生效。
```

**分母只算走完注册+激活的账号** —— 半路失败（收信超时等）的根本没走到能判断的
那一步，把它们算进去就是**假警报**，会让人去改一个根本没问题的码。

### 实测验证（2026-09-26）

`run.py --count 3 --workers 3`：3 个出口里 1 个成功（另 2 个出口当天配额已满，
属设计内处置）——

```
邀请码（推荐关系）：1/1 个已确认受邀
  本次使用：ZQ9QEVJJ（1 个账号发出了码，0 个未走完注册+激活、无法判断）
  ✓ 发出的码全部生效。
```

台账里该账号：`invite_code = "ZQ9QEVJJ"`、`invited_at = 1790359202672`
（= 2026-09-26 02:00:02，与注册时刻吻合）、`stages["invite"] = "ok"`、
`token_reward = 100000000`（延后到激活才发的那个奖励）。

### 相关端点（都**不消耗**注册配额）

| 端点 | 要不要登录 | 用途 |
|---|---|---|
| `GET /api/model-service/campaign` | 否 | 活动参数：`inviteEnabled` / `tokensPerInvite` / 注册窗口起止 |
| `GET /api/invite` | 是 | 查**当前账号自己**的码：`{"code": "...", "url": "...", "invitedCount": 0}` |

> ⚠ 没有独立的"校验某个码"端点（`/api/invite/<code>` 等一律 404）——
> 校验只在注册那一刻发生，而且**服务端也不校验**（见上表）。

---

## 配额模型（先读这一节再调参数）

```
服务端（权威，不可协商）
  ├─ 速率层：每出口 IP 每 60s 1 次注册        → 本地闸门 FO_REG_MIN_INTERVAL=62
  └─ 配额层：每出口 IP 每天 5 次注册（实测）   → 本地计数 FO_IP_QUOTA_MAX=5

本地（保守估计，只为"别白跑"）
  ├─ 按出口 IP 分文件记账（JSONL，append-only）
  ├─ 窗口 24h（服务端恢复时间未知，取偏保守值）
  └─ 触顶时把记录标 skipped（**不发请求**），不是 failed
```

**结论：能注册几个 = 有几个不同出口 IP × 5。**

| 出口数 | 本轮上限 | 理论上限（受 62s 闸门） |
|---|---|---|
| 1（直连） | 5 | 58 个/小时（但日配额先到） |
| 4 | 20 | 232 个/小时 |

调 `--workers` **不会**突破它（`workers` 只是登录侧并发）。
调 `FO_REG_MIN_INTERVAL` 只影响速率层，**对日配额无效**。
换邮箱域名 / 换用户名 / 换 UA **全部实测无效** —— 唯一的解法是换出口 IP。

---

## 台账

```
ledger/
  runs/<YYYY-MM-DD>/results-<YYYYMMDD-HHMMSS>.json   ← 合并后的**全量**快照 = 读源
  latest.json                                        ← **本批**结果（含失败/跳过）
```

- 🔴 **读源是最新那份全量快照，不是 `latest.json`。** 后者只含本批那几条；
  拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、每次跑批覆盖上一次
  （参考项目栽过两次，第二次是 `--count 1` 把 53 条覆盖成 1 条）。
- 🔴 `snapshot_paths()` **用正则筛**，不用 `glob("*.json")` —— `runs/` 是个人工可写的目录，
  一个 `notes.json` 落进去，按"最新"就会被当成读源，于是缩水护栏的分母、`--count` 的取样池
  全跟着变，而且**全程不报错**。
- 🔴 先写快照、再刷本批。万一第 3 步挂了，读源仍是上一份完整台账，新快照已躺在 `runs/` 可人工捞回；
  反过来会变成"读源已更新、快照没留下"，这次落盘就没有回滚点。
- 合并规则：**rank + 字段并集**。同级取并集而不是整条替换 —— 下游交回的是**增量字段**时
  （只有 `verify`、没有 `status`），rank 恒为 0 而旧记录 rank=2，`rank(new) > rank(old)` 永远为假，
  字段一个都写不进去，而打印出来的一切都"正常"。**数据静默缩水、指标全绿**是最坏的一类失败。
- 防静默缩水护栏：合并后条数 < 原有就抛错退出（`save(p, recs)` 会**自动读取**当前台账，
  唯一逃生口是 `existing=[]`）。

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 本地配额触顶（`quota.QuotaExceeded`） |
| 3 | 防静默缩水护栏触发（与 `ledger.EXIT_COVERAGE_GAP` 同码） |
| 4 | 槽位池全死（`proxypool.AllSlotsDead`） |
| **5** | **key 建出来了但校验不过 ⇒ 交付不完整** |

`5` 是后来补的：`status == "success"` 只说明"key 建出来了"，**不代表它能调用**。
本项目就栽过一次（见下面的坑 #1），当时 3 个账号全是"建出成功、校验失败"，
而退出码是 **0** —— 任何脚本化调用都会把这一批读成"成功"。

---

## 坑清单（都是本项目实测踩到的，不是"可能"）

### 1. 🔴 SPA 站点：HTTP 200 不能作为"端点存在"的判据

`GET https://opc.fiime.cn/model-service/v1/models`（**漏了 `/api`**）返回
**HTTP 200 + 6707 字节的 index.html**（前端路由兜底），状态码看着完全正常，实际根本没打到 API。

```
GET /model-service/v1/models      → 200, HTML, 6707B   ✗
GET /api/model-service/v1/models  → 200, JSON, 1880B   ✓
```

- 代价：一次批量跑完 3 个账号的校验**全部**"失败"，而我一开始误判成"网关传播延迟"。
- 修法：`verify_key` 判据改成 **`content-type` 含 `json` + 能解析出 `data` 列表**，
  并把**字节数**打进说明里 —— 6707 就是识别"被 SPA 兜底"的指纹。
- `tests/test_opc.py::TestVerifyKeySemantics` 钉了这条，含"URL 必须带 `/api`"的回归钉子。

### 2. 🔴 邮件正文里 URL 后面跟中文标点时，`rstrip` 一个字都剪不掉

老正则用排除法 `[^\s"'<>()]`，会把 `。谢谢` 整串吞进匹配；而 `rstrip` 只剪**末尾**、
末尾是「谢」⇒ 剪不掉 ⇒ token 变成 `abc123。谢谢` ⇒ 激活 400，且报错完全指不到原因。

**修法**：改成按 **RFC 3986 字符集**显式匹配 `[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+`，
中文标点从源头就进不来。

### 3. 🔴 密码生成器宣称"保证三类齐全"，实际是假保证

老实现 `"Lz#" + random(letters+digits, 9)`，9 位里一个数字都没有的概率约 **21.6%**
（(52/62)⁹），而 docstring 写着"保证大小写字母/数字/符号三类齐全"。
测试跑 50 次必然撞上。**修法**：先钉死必需字符、再补位、最后 `shuffle` 打散位置特征。

### 4. 🔴 `models` 是**对象列表**，不是字符串列表

`GET /api/model-service` 返回的 `service.models` 元素形如
`{"id":"opc-txt-v1","object":"model","type":"text","enabled":true,"rateMultiplier":2}`。
按字符串用会静默拿到 dict 的 repr。且 `type` 有 `text`/`image`/`video` 三类 ——
把 image/video 模型塞进 `/v1/chat/completions` 的示例里，读者照着抄就报错。

### 5. 🔴 台账里不存 `base_url` = 没交付

下游拿到 key 却不知道往哪打。而且这个值曾经出过错（漏 `/api`），
不存进记录就事后无从判断"当时用的地址对不对"。
**修法**：`AccountRecord.base_url` 落台账（优先用**服务端上报的** `apiBasePath`，不是本地常量）。

### 6. 🔴 `verify_keys` 要用**记录自己的** `base_url`

台账里可能是历史批次用的地址；拿今天的配置去解释昨天的 key 会得出错误结论，
而且**不报错**。老记录（`base_url` 为空）才回落到配置默认值。

### 7. 🔴 挂了代理必须同时关 `trust_env`

本机有沙箱代理的环境变量（`HTTP_PROXY` 等）。不关 `trust_env` 时，
`requests` 会用环境变量**静默盖掉**你指定的代理 —— 请求成功、不报错、不告警，
很容易误判成"代理已生效"。`config.apply_proxy()` 里强制 `session.trust_env = False`。

### 8. 🔴 `finditer` 不能再传 flags

`_URL_RE` 已经 `re.compile(..., re.I)`，再 `re.finditer(_URL_RE, text, re.I)` 会抛
`ValueError: cannot process flags argument with a compiled pattern`。
（写这段修复时我自己踩的，测试当场抓住。）

### 9. 🔴 `existing=None` 不等于"跳过检查"

`_guard_no_shrink` 的 docstring 曾写"`existing=None` 表示不检查"，与实现相反 ——
`save()` / `save_snapshot()` 在入口把 `None` 转成"从磁盘自动读取当前台账"，**护栏生效**。
这个方向是刻意的：如果 `None` 代表跳过，那**最自然的写法 `save(p, recs)` 反而没有护栏**。
那条写反的 docstring 还带偏了一条测试用例。

### 10. ⚠ 单账号模式刻意不带配额守卫与速率闸门

`run_one()` 不调 `quota.check_or_raise`、不接 `_ScopeRateLimiter`。理由：单账号模式的用途是
调试/探测**真实服务端行为**，本地计数是保守估计，拿它挡住请求会让"站点到底还能不能注册"
这个最想知道的问题永远答不上来。
**代价**：`--count 1` 循环跑**会**触发服务端 60s 速率限制。

### 11. ⚠ `--out` 不填时**必须**读台账读源

若退化成读"本次要写的那个新文件"，合并就等于没做 —— 每次运行都从零开始，
正是 `--count 1` 把 53 条覆盖成 1 条那条路。

### 12. ⚠ CF Worker 的 `extracted_json` 对本站返回空数组

发件人是 `rockssr@163.com`（不是 Worker 熟悉的那几家），所以必须自己从 `body` 正则捞链接。
另外收信必须用 `GET /api/inbox?email=`，**不要**用 `/admin/all`（D1 读配额 + 窗口截断）。

### 13. 🔴 `GET /api/model-service/keys` 不存在，而 `list_keys()` 把 404 静默成"没有 key"

实测那个路径返回 **404 + text/html**（服务端只在同一路径注册了 POST）。
key 列表在 `GET /api/model-service` 的 `keys` 字段里。

早期实现打的就是 404 路径，且 `if not r.ok: return []` —— 于是"端点打错了"
被翻译成"这个账号没有 key"。症状：恢复工具报告"没有缺口"，而实际有。
**修法**：改打 `/model-service`，且 `list_keys()` 默认 `strict=True`（拿不到就抛）。

### 14. 🔴 站点 UI 说 key「只显示一次」，但 API 每次都返回明文

`GET /api/model-service` 的 `keys[].secret` 是**明文且可重复读取**
（6 个账号逐一核对，与台账**逐字节一致**）。所以：
- key 丢了能**捞回来**（`tools/ops/recover_keys.py`），不必删掉重建、更不必重新注册；
- ⚠ 任何拿到会话 cookie 的人都能读走全部 key —— 这个端点的响应只该进 `ledger/`。

### 15. 🔴🔴 邀请码**服务端不校验存在性** —— 写错了完全静默

这是本项目**最隐蔽**的一个失败模式：拿一个形状合法但必然不存在的码
`ZZZZZZZZ` 去注册，服务端返回 **HTTP 200，账号正常建出，零报错**。

⇒ 后果：**"整批用了一个拼错的码"在成功率、退出码、错误分类里一个信号都没有。**
任何脚本化调用都会把这批读成"成功"。

**唯一判据是响应里的 `user.invitedAt`**（0 = 推荐关系未成立），所以本工具：
- 把它落进台账（`AccountRecord.invite_code` / `invited_at`）；
- 在报告末尾单独一节报出来，分母只算**走完注册+激活**的账号
  （半路失败的根本没走到能判断的那一步，算进去就是**假警报**）；
- 起飞前把生效的码**打印出来**（`🎟  邀请码：…（来源 …）`）。

**连带发现：带码时注册响应的 `registrationTokenReward` 是 0** —— 奖励延后到
**激活**才发（有效码激活后给 100000000；不带码则注册时直接给）。
所以「注册时 reward=0」**不是**错误信号，别拿它判码的有效性（早期就是这么误读的）。

> 判定过程花了 3 个注册额度（乱填码 1 个 + 有效码 1 个 + 枚举自有码），
> 结论记在 `src/config.py` 的「注册邀请码」一节。

### 16. 🔴 邀请码必须**每一次重试都带上**

`_register_with_retry` 里码是**循环外层**的参数，不是某一次调用的参数。
若只在首次尝试里传，那么"被 429 限流过一次"的账号会**不带码注册成功** ——
注册成功、激活成功、退出码 0，只有 `invitedAt=0` 孤零零摆在那里，
而且它会被读成"这个码不灵"，而不是"我们的代码把码丢了"。
回归钉子见 `tests/test_opc.py::TestInviteCodeSurvivesRetry`。

---

## 目录结构

```
run.py                       CLI 入口（单账号 / 批量 / probe）
src/
  config.py                  全局配置 + .env 加载 + 代理接线（proxy_slots/slot_scope/apply_proxy）
  opc.py                     站点客户端：429 分类、注册/激活/登录/建 key/校验、密码与 URL 提取
  tempmail.py                CF Worker 邮箱客户端（建箱 / 收信 / 轮询等待）
  mailbox.py                 邮箱源协议 + 工厂（可插拔，默认 worker）
  proxypool.py               槽位池：租约、同出口互斥、指数退避冷却、状态持久化
  quota.py                   按出口 IP 的 JSONL 配额计数 + 触顶判断
  pipeline.py                两段式流水线 + 账号记录 + 槽位结算
  ledger.py                  台账快照/合并/防静默缩水/覆盖率护栏
  report.py                  收尾报告（依赖全部注入，测试链不拉 requests）
  cli.py                     三个入口共用的骨架（退出码总表 / help 常量 / 参数）
  fsutil.py redact.py        原子写 / 脱敏
tools/
  gates/check_leaks.py             泄漏闸门：`.env` 里的标识不许出现在提交物里（含自检）
  probes/probe_slots.py            量出真实出口 IP（= 并发上限）
  probes/probe_ip_daily_limit.py   量出每 IP 每日注册上限（实测 = 5）
  ops/check_keys_alive.py          复验台账里所有 key（**不消耗注册配额**）
  ops/recover_keys.py              从线上账号把 key 捞回台账（**不消耗注册配额**，默认干跑）
tests/                       离线测试，全部可跑（不含任何真实网络访问）
```

### 两个运维工具的边界

| | 用途 | 会写盘吗 | 依据 |
|---|---|---|---|
| `check_keys_alive.py` | **复验**：台账里的 key 现在还能调吗 | **不会**（纯读） | 独立 `requests.get`，不带 cookie —— 模拟第三方调用方 |
| `recover_keys.py` | **恢复**：把线上账号的 key 捞回台账 | 默认**不会**，要 `--apply` | 登录后读 `GET /api/model-service` 的 `keys[].secret` |

两者都**不消耗注册配额**（那是注册阶段的专属成本，5 个/IP/天）。
分工的理由：跑批崩掉时最急的是"key 还在不在"，而查 key 是纯读 ——
把它和 `run.py` 混在一起，就等于"每次查 key 都可能烧掉一个配额"。

---

## 测试

```bash
python -m pytest          # 全部离线，~1s
python -m ruff check .
```

**测试链里没有任何真实网络访问。** 真实站点交互放在 `tools/probes/` ——
CI 上跑真实注册会**烧掉一个出口当天的额度**（上限只有 5 个/天）。

---

## 与参考项目 `Intern-Register-Tool` 的关系

沿用同一套架构模式（两段式流水线 / 槽位池租约 / 台账快照 / 依赖注入的报告层 /
"判据只有唯一入口"），三处刻意不同：

| | 参考项目 | 本项目 |
|---|---|---|
| 依赖 | playwright + cryptography | **只有 requests** |
| 注册约束 | 账号维度 | **出口 IP 维度**（所以槽位池是必需品，不是优化项） |
| 限流层数 | 1 | **2**（速率层 + 日配额层，处置相反） |

---

## License

MIT
