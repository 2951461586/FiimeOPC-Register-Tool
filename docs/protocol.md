# 站点协议（逆向记录）

**目标**：`https://opc.fiime.cn/`（FiimeOPC 社区）。
**方法**：下载 6.4MB 前端 bundle + 懒加载 chunk（`Register-*.js` / `Activate-*.js` /
`Login-*.js` / `ModelService-*.js`）逐段读，再用真实请求逐个验证。
**记录日期**：2026-09-26。**以下每一条都实测过**，未实测的会标 ⚠ 推测。

> ⚠ 站点改版会让本文档失效。判据是行为不是文档 —— 出问题先跑
> `python run.py probe` 看实时状态。

---

## 0. 站点形态：SPA（**最重要的一条前置认知**）

前端路由兜底：请求**不存在的路径**返回 **HTTP 200 + 6707 字节 index.html**。

```
GET /model-service/v1/models      → 200, text/html, 6707B    ✗ 假成功
GET /api/model-service/v1/models  → 200, application/json, 1880B  ✓
```

⇒ **状态码不是"端点存在"的判据。** 必须同时检查 `content-type`。
`6707` 这个字节数就是"被 SPA 兜底"的指纹（见 `README.md` 坑 #1）。

---

## 1. 公共

### `GET /api/csrf-token`

```json
{"token": "<hex>"}
```

写操作（POST / DELETE）需要把 token 放进请求头。`OpcClient._csrf_headers()` 负责。

### `GET /api/settings/public`

```json
{"registration": {"closed": false, "notice": "", "blockGmailAliases": true,
                  "captchaEnabled": false},
 ...}
```

🔴 `registration.captchaEnabled` 是本链**能否用纯 HTTP 跑通**的总开关。
实测 `false`。一旦为 `true`，整条链立刻失效 —— `config.validate()` 在入口就报错，
不静默跑失败。

`blockGmailAliases: true` ⇒ Gmail 的 `+别名`/点号变体被拒，所以邮箱源用自建域名。

---

## 2. 注册链路

### `GET /api/auth/check-username?username=<name>`

```json
{"available": true, "reason": ""}          // reason: "" | "taken" | "format"
```

🔴 **必须真的查一次**，不能只靠本地随机。用户名撞车时服务端返回
`{"available":false,"reason":"taken"}`，而**注册接口对"已占用"和"格式错"报的是同一句文案**
（前端也是靠 `check-username` 提前拦的）—— 事后分不清是撞车还是格式问题。

### `POST /api/auth/register`

请求体（`Content-Type: application/json`）：

```json
{
  "username": "lz451074",
  "email": "oai-xxxx@example-mail.test",
  "password": "Lz#a7b2c9d4e1",
  "agree": true,
  "accountType": "personal",
  "website": ""
}
```

- `accountType` 取值：`personal` / `enterprise` / `community` / `alliance`。
- 🔴 **`website` 是蜜罐字段，必须传空串。**
  传任何非空值 → `400 {"error":"注册失败，请刷新页面后重试"}`。
  省略它是否安全**未验证**（取决于服务端把缺失当 `None` 还是 `""`），所以固定传 `""`。
- 🔴 **密码必须含字母。** 纯数字 `12345678` → `400 {"error":"密码必须包含字母"}`。
  前端校验"含两类以上"，所以本工具生成的四类齐全密码（`Lz#` + 字母数字）稳定通过。
- 🔴 **密码是明文 POST**，站点前端不做 RSA 加密。请求体里就是原密码。
  ⇒ 这是本链不需要 `cryptography` 依赖的原因，也是**必须走 HTTPS** 的原因。
- 可选的 `inviteCode` —— 见下一节。

#### 邀请码 `inviteCode`（可选）

字段名来自站点 SPA 的 Register chunk：
`xe("/auth/register", {username, email, password, agree: !0, accountType,
inviteCode: n.inviteCode || void 0, website})`。

```json
{"username": "...", "email": "...", "password": "...", "agree": true,
 "accountType": "personal", "inviteCode": "R5AZGYNU", "website": ""}
```

- 🔴 空值时前端发的是 **`void 0` —— 整个键省略**，不是空串。本实现一致
  （`opc.register()` 只在非空时才写这个键）。
- 前端输入框 `maxLength={32}`，标签文案「邀请码（可选）」。
- 形态：**8 位大写字母数字**（`GET /api/invite` 返回形如 `R5AZGYNU`）。
  前端**不做**格式校验，本工具在本地做（`config.normalize_invite_code()`）。
- 另有 `?invite=<code>` 的 URL 参数写法（前端 `u.get("invite")`），本工具不用。

🔴🔴 **服务端不校验邀请码是否存在**（2026-09-26 实测）：

| 发出去的码 | 响应 |
|---|---|
| 有效码 | `200`，`user.invitedAt = <毫秒时间戳>` |
| `ZZZZZZZZ`（形状合法、必然不存在） | **`200`，账号正常建出，零报错** |

⇒ 判据只能是 **`user.invitedAt`**（`0` = 未记录为受邀），状态码给不出任何信息。

🔴 **带码时 `registrationTokenReward` 在注册阶段恒为 0** —— 奖励**延后到激活**
才发（有效码激活后返回 `100000000`）。不带码时注册就直接给 `100000000`。
⇒ 「注册时 reward=0」**不是**错误信号，不能拿它判断码的有效性。

#### `GET /api/invite`（需登录）

返回**当前账号自己**的码：

```json
{"code": "R5AZGYNU", "url": "https://opc.fiime.cn/register?invite=R5AZGYNU",
 "uses": 0, "invitedCount": 0, "paidCount": 0,
 "tokenRewardCount": 0, "tokenRewardTotal": 0}
```

⚠ **没有**"校验某个码是否存在"的独立端点（`/api/invite/<code>`、
`/api/invite/lookup` 等一律 404）。校验只在注册那一刻发生 —— 而且服务端也不校验。

#### `GET /api/model-service/campaign`（公开，无需登录）

活动参数：

```json
{"active": true, "registrationConfigured": true, "registrationEnabled": true,
 "tokensPerRegistration": 100000000,
 "registrationStartsAt": 1788181200000, "registrationEndsAt": 1796734800000,
 "inviteEnabled": true, "tokensPerInvite": 100000000}
```

注册窗口实测为 **2026-08-31 → 2026-12-08**。

成功响应：

```json
{"user": {"id": "...", "opcId": "OPC-14964", "username": "lz451074"},
 "needActivation": true,
 "registrationTokenReward": 100}
```

失败响应：

| 状态 | body | 含义 |
|---|---|---|
| 429 | `{"error":"注册过于频繁，请一分钟后再试"}` | 速率层：60s/IP |
| 429 | `{"error":"该 IP 今日注册数量已达上限，请明日再试或联系管理员"}` | **配额层：IP 日上限** |
| 400 | `{"error":"密码必须包含字母"}` | 密码规则 |
| 400 | `{"error":"注册失败，请刷新页面后重试"}` | 蜜罐触发 / 用户名已占用 |

🔴 **两种 429 的处置完全相反**（退避重试 vs 换 IP），分类逻辑只能有一处：
`opc.classify_429()`。

🔴 **邀请码错误不在上表里** —— 它**不产生任何错误响应**。别指望在失败表里找它。

### 激活邮件

- 发件人：**`rockssr@163.com`**
- 主题：**`激活你的 FiimeOPC 账号`**
- 正文里同一个链接出现 **4 次**（纯文本版 + HTML 版 + 按钮 href + "或复制"行），去重后 1 条。
- 链接形态：`https://opc.fiime.cn/activate?token=<64 位 hex>`

🔴 CF Worker 的 `extracted_json` **对本站返回空数组 `[]`** ——
发件人不在 Worker 熟悉的那几家模板里。必须自己从 `body` 正则捞。
🔴 正则必须按 **RFC 3986 字符集**匹配（见 `README.md` 坑 #2），
否则正文里 URL 后跟的中文标点会污染 token。

### `POST /api/auth/activate`

```json
{"token": "7d88622ae13c558c9534f949468311e2b811df5b2b57f88d239f4730d13930a2"}
```

🔴 收的是 **token 字符串**，不是完整 URL。只认 query 里的 `token` 参数。
成功返回 `{"user": {...}, "registrationTokenReward": 100}`。

⚠ 带邀请码注册的账号，**奖励是在这一步才发的**（`100000000`），注册响应里是 0。
所以这个响应的 `user.invitedAt` 也是"推荐关系是否成立"的判据之一 ——
本工具在注册和激活两处都取，取到非零就留下。

---

## 3. 登录与 API Key

### `POST /api/auth/login`

```json
{"account": "oai-xxxx@example-mail.test", "password": "Lz#a7b2c9d4e1"}
```

- `account` 可以是**邮箱或用户名**（实测邮箱可用）。
- 无验证码、无 2FA。
- 成功：`{"user": {"id": "...", "username": "..."}}`
- 未激活：`401 {"error":"邮箱未激活","code":"EMAIL_NOT_ACTIVATED","email":"..."}`
- ⚠ 开了两步验证的账号返回 `{"needTotp": true, "challenge": "..."}` ——
  **本工具不支持**（TOTP secret 在注册流程里拿不到），如实报错，不假装成功。

### `GET /api/model-service`（需登录 cookie）

**这一个端点返回全部账号侧信息。** 顶层键实测：

| 键 | 内容 |
|---|---|
| `service` | 服务开关 / `apiBasePath` / `dailyTokenLimit` / `maxApiKeysPerUser` / `models` |
| `wallet` | `{balance, totalEarned, totalSpent}` |
| `usage` | `{date, usedToday, dailyLimit, remainingToday}` |
| **`keys`** | 🔴 **该账号全部 API Key，含明文 `secret`** |
| `invite` / `giftPool` / `giftLeaderboard` / `benefits` / `partnership` / `ranking` | 其余模块 |

```json
{"service": {"enabled": true,
             "apiBasePath": "/api/model-service/v1",
             "dailyTokenLimit": 500000000,
             "maxApiKeysPerUser": 5,
             "models": [{"id": "opc-txt-v1", "object": "model", "type": "text",
                         "enabled": true, "debug": false, "rateMultiplier": 2},
                        {"id": "opc-image-v1", "type": "image", "enabled": true},
                        {"id": "opc-video-v1", "type": "video", "enabled": true},
                        {"id": "sensenova-6-8-flash-lite", "type": "text", ...},
                        ... 共 19 个]},
 "wallet": {"balance": 100000000, "totalEarned": 100000000, "totalSpent": 0},
 "usage": {"date": "2026-09-26", "usedToday": 0,
           "dailyLimit": 500000000, "remainingToday": 500000000},
 "keys": [{"id": "msk_muh8n5364c1f46", "name": "default",
           "masked": "opc_sk_5aa2••••••••2d3a",
           "secret": "opc_sk_5aa233312cea7d8c08a864dc54dda7212398209607292d3a",
           "status": "active", "createdAt": 1790357521795, "lastUsedAt": 0}]}
```

- 🔴 `models` 是**对象列表**，元素有 `type`（`text`/`image`/`video`）和 `enabled`。
- `dailyTokenLimit` = **5 亿** token/天/账号。
- `maxApiKeysPerUser` = **5**。
- 🔴 `apiBasePath` **带 `/api`**。这是权威值，`AccountRecord.base_url` 优先用它。

#### 🔴 `keys[].secret` 是**明文，且可重复读取**

站点 UI 写着「出于安全原因，这串密钥只显示一次」，**但那是前端的说法** ——
后端每次 `GET /api/model-service` 都把 `secret` 原样吐回来（实测 6 个账号逐一核对，
与台账里的值**逐字节一致**）。

两个后果：

- **好消息**：key 丢了可以**捞回来**（`tools/ops/recover_keys.py`），不必删掉重建、
  更不必重新注册（注册要烧 IP 日配额）。
- ⚠ **安全面**：任何拿到会话 cookie 的人都能读走该账号的全部 key。
  所以这个端点的响应**只该进 `ledger/`**（整目录 gitignored），不要贴到任何地方。

#### 🔴 `GET /api/model-service/keys` **不存在**

实测返回 **404 + text/html**。服务端只在同一路径上注册了 **POST**。
key 列表必须从上面那个端点的 `keys` 字段取。

⚠ 踩过的坑：早期实现打的正是这个 404 路径，并且 `if not r.ok: return []`
把 404 **静默**变成"这个账号没有 key"。症状是"恢复工具说没缺口"而实际有 ——
**报错被翻译成了正常结果**。现在 `list_keys()` 默认 `strict=True`：拿不到就抛。

### `POST /api/model-service/keys`

```json
// 请求体
{"name": "default"}

// 成功响应
{"key": {"id": "msk_muh8n5364c1f46", "name": "default",
         "masked": "opc_sk_5aa2••••••••2d3a", "status": "active"},
 "secret": "opc_sk_<64 hex>"}
```

🔴 建完**立刻**落台账 —— 理由不是"丢了就没了"（见上，能捞回来），
而是少一次往返、少一个"跑到一半崩了"的窗口。

### `DELETE /api/model-service/keys/<key_id>`

删除 key。需要 CSRF 头。

---

## 4. OpenAI 兼容推理面

**base_url**：`https://opc.fiime.cn/api/model-service/v1`
**鉴权**：`Authorization: Bearer opc_sk_<64 hex>`
**key 前缀**：`opc_sk_`（**不是** `sk-`）

### `GET /v1/models`

```json
{"object": "list", "data": [{"id": "opc-txt-v1", "object": "model"}, ...]}
```

🔴 **判据是 `content-type` 含 `json` + 能解析出 `data` 列表，不是状态码。**
- `401` → key 无效（确定失败，**不重试**）
- `2xx` + `text/html` + 6707B → 打到了 SPA 兜底页 ⇒ **地址错了**（多半漏 `/api`）

### `POST /v1/chat/completions`

```json
{"model": "opc-txt-v1",
 "messages": [{"role": "user", "content": "ping"}],
 "max_tokens": 8}
```

🔴 `model` 必须是 **text 类**。`opc-image-v1` / `opc-video-v1` 塞进 chat 接口会报错。
`run.py --probe-key` 会真发一次（**消耗 Token**），默认关闭。

---

## 5. 配额模型（实测）

| 层 | 限制 | 维度 | 恢复 | 实测方法 |
|---|---|---|---|---|
| 速率 | **60s / 1 次** | 出口 IP | 60s | 间隔 62s 连打，稳定通过 |
| 配额 | **5 次 / 天** | 出口 IP | 未知（推测次日） | `tools/probes/probe_ip_daily_limit.py`：5 次成功后第 6 次稳定 429 |

**验证"维度是 IP 而不是账号/邮箱"的方法**：
直连（本机 IP）触发 429 的同时，4 个槽位 IP 并发注册全部 200 ——
同一时刻、不同出口、不同结果 ⇒ 约束维度是出口 IP。

**已验证无效的绕过手段**：换邮箱域名、换用户名、换 UA、换 `accountType`。
**唯一有效的解法**：换出口 IP。

⚠ 服务端的"次日"重置时间**未实测**（本地窗口取保守的 24h）。

---

## 6. 未实测 / 存疑

- ⚠ 服务端日配额的**具体重置时刻**（UTC 0 点？本地 0 点？滚动窗口？）—— 未知。
- ⚠ `website` 蜜罐字段**省略**（而非传空串）的行为 —— 未验证，代码固定传 `""`。
- ⚠ 用户名格式规则（`reason: "format"` 的触发条件）—— 未逐条测；
  本工具用 `lz` + 6 位数字，实测稳定通过。
- ⚠ 站点是否对同一出口的**登录**也有限流 —— 未观测到，但批量场景仍建议 `--workers` 别开太高。
- ⚠ `user.invitedAt` 是**注册时就写**还是**延后到激活才写** —— 证据指向**注册时**，
  但不是秒级确证（见下）。本工具**两处都取**，并且**只在激活之后**才下
  "码没生效"的结论（注册阶段看到 0 就报，会发假警报）。

  > 实测样本（2026-09-26 02:00）：该账号注册阶段共 19.3s（POST 本身 7.2s、
  > 收信 7.0s、激活 4.0s），台账落盘于 02:00:24，而
  > **`invitedAt = 2026-09-26 02:00:02.672`** —— 落在 POST 附近，
  > 比激活早约 10s。**激活时写入可以排除**（时间差远大于重建误差）；
  > 但"到底是 POST 发出前还是后"没有秒级精度，故仍保守按两处都取处理。
- ⚠ **自邀**（用自己的码给自己注册）的行为 —— 未实测，猜测会被判无效
  （`invitedAt` 保持 0）。本工具不阻止，只如实报告。
- ⚠ 邀请码是否**区分大小写** —— 未实测。本地统一转大写（与前端"8 位大写"的
  实际形态一致），若服务端其实不区分则无影响。
