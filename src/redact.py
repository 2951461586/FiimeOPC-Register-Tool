"""脱敏助手 —— **日志 / 输出边界必须过这里**。

🔴 为什么脱敏是硬要求：本项目会打印代理串（`scheme://user:pass@host:port`）、
   API Key（`opc_sk_...`）与账号密码。直接 print / 写日志会把**凭据**一起落盘，
   而 `.workbuddy-ai/tmp/*.log` 经常被贴进对话或 issue 里 —— 泄漏一次就够。

用法：**凡是把配置值 / 凭据写进 stdout / 日志 / 报告的地方，先过这里。**

⚠ 台账文件（`ledger/`）是**唯一的例外** —— 它就是要存明文凭据，
  否则拿到的 key 没法用。该目录已被 `.gitignore` 整目录排除。
"""


def redact_url(url: str) -> str:
    """把 URL 的 userinfo（`user:pass@`）换成 `***`，其余保留。

    http://user:pass@10.0.0.1:8080  →  http://***@10.0.0.1:8080
    http://127.0.0.1:7901           →  原样（没有 userinfo）
    """
    if not url or "://" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    _userinfo, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"


def redact_proxy(url: str) -> str:
    """代理串的**可展示**形式 —— 比 `redact_url` 多抹掉“子域即凭据”的 token。

    · `scheme://user:pass@host:port` → `scheme://***@host:port`
      （凭据在 userinfo）
    · `scheme://<token>.provider.tld:port`（无 userinfo）→
      `scheme://***.provider.tld:port`
      🔴 这种代理**子域 token 本身就是凭据**，只抹 userinfo 会把它漏出去。
    · `IP:port`（如 `127.0.0.1:7901`）原样保留。
    """
    masked = redact_url(url)
    if not masked or "://" not in masked or "***@" in masked:
        return masked
    scheme, _, rest = masked.partition("://")
    hostport, slash, path = rest.partition("/")
    host, colon, port = hostport.partition(":")
    labels = [x for x in host.split(".") if x]
    is_ip = bool(labels) and all(x.isdigit() for x in labels)
    if len(labels) >= 3 and not is_ip:
        host = "***." + ".".join(labels[1:])
    return f"{scheme}://{host}{colon}{port}{slash}{path}"


def redact(raw: str, keep: int = 0) -> str:
    """把凭据串打成 `<N chars>` —— **连前缀都不打**。

    `keep>0` 只用于本地排查。默认 0：连"前 6 位"这类信息也足以在别处做关联。
    """
    if not raw:
        return ""
    if keep and len(raw) > keep:
        return f"{raw[:keep]}…<{len(raw)} chars>"
    return f"<{len(raw)} chars>"


def mask_key(key: str) -> str:
    """API Key 的**可展示**形式：留头 12 位 + 尾 4 位。

    🔴 与 `redact()` 的区别：`redact()` 连前缀都不给，用于**日志边界**；
       `mask_key()` 是站点自己也在用的形式（`GET /model-service` 返回
       `masked: "opc_sk_baef••••••••ae6d"`），用于**报告里让人认出是哪把 key**。
       两者不可互相替代 —— 报告里用 `redact()` 会让人分不清账号，
       日志里用 `mask_key()` 则等于泄漏了 16 位密钥材料。
    """
    if not key:
        return ""
    if len(key) <= 16:
        return "<" + str(len(key)) + " chars>"
    return f"{key[:12]}…{key[-4:]}"
