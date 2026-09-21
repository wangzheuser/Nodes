# -*- coding: utf-8 -*-
"""
ProxyScrape 自动注册 —— 本地打码走协议

思路（最快最省）：
  · 浏览器（DrissionPage + turnstilePatch 扩展）只干一件事：在真实 sign-up 页
    里让 Cloudflare Turnstile 自动过，读出隐藏字段 cf-turnstile-response 的 token。
    token 在真实域名 dashboard.proxyscrape.com 下生成，hostname 天然匹配，
    服务端 siteverify 不会因 hostname 拒绝。
  · 临时邮箱、注册、收验证码、验邮箱 —— 全部走 HTTP 协议，不碰浏览器。

依赖：DrissionPage / requests（用 grok 项目那个 venv 跑即可）
用法：python proxyscrape_register.py
"""

import os
import re
import sys
import time
import json
import random
import string
import threading
import html as _html
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

# ── 配置 ────────────────────────────────────────────────
# YYDS Mail（临时邮箱）
YYDS_BASE = "https://maliapi.215.im/v1"
YYDS_KEY = os.environ.get("YYDS_API_KEY", "").strip()
YYDS_DOMAIN = os.environ.get("YYDS_DOMAIN", "").strip()

_BASE = os.path.dirname(os.path.abspath(__file__))
_PROXY_CONFIG_PATH = os.path.join(_BASE, "proxy_config.json")

MAIL_CHANNELS = (
    ("auto_zero_config", "GoneBox 自动重试（推荐）"),
    ("tempmail_lol", "TempMail.lol（仅账号，无试用节点）"),
    ("fce_areueally", "FreeCustom areueally"),
    ("fce_ditpay", "FreeCustom ditpay"),
    ("gonebox", "GoneBox"),
    ("yyds", "YYDS Mail（需要 YYDS_API_KEY）"),
)
AUTO_MAIL_PROVIDERS = ("gonebox",)

# ProxyScrape dashboard
PS_BASE = "https://dashboard.proxyscrape.com"
PS_REGISTER = f"{PS_BASE}/v2/v4/account/auth/register"
PS_LOGIN = f"{PS_BASE}/v2/v4/account/auth/login"
PS_ME = f"{PS_BASE}/v2/v4/account/auth/me"
PS_VERIFY_EMAIL = f"{PS_BASE}/v2/v4/account/verify-email"
PS_RESEND = f"{PS_BASE}/v2/v4/account/reset-verification-code"
PS_CLAIM_TRIAL = f"{PS_BASE}/v2/v4/account/premium/claim-trial"
PS_SIGNUP_PAGE = f"{PS_BASE}/v2/sign-up"
PS_SITEKEY = "0x4AAAAAAAFWUVCKyusT9T8r"

# turnstilePatch 扩展：环境变量优先，其次使用项目内副本。
_EXTENSION_CANDIDATES = (
    os.environ.get("TURNSTILE_EXTENSION_PATH", "").strip(),
    os.path.join(_BASE, "turnstilePatch"),
)
EXTENSION_PATH = next((path for path in _EXTENSION_CANDIDATES if path and os.path.isdir(path)),
                      _EXTENSION_CANDIDATES[1])

_BROWSER_CANDIDATES = (
    os.environ.get("CHROME_PATH", "").strip(),
    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
)
BROWSER_PATH = next((path for path in _BROWSER_CANDIDATES if path and os.path.isfile(path)), "")
BROWSER_PROXY = ""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# 输出目录：账号 → account/  代理节点 → node/
_ACCOUNT_DIR = os.path.join(_BASE, "account")
_NODE_DIR = os.path.join(_BASE, "node")
os.makedirs(_ACCOUNT_DIR, exist_ok=True)
os.makedirs(_NODE_DIR, exist_ok=True)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": UA,
    "Origin": PS_BASE,
    "Referer": PS_SIGNUP_PAGE,
}


_print_lock = threading.Lock()
_file_lock = threading.Lock()
_tls = threading.local()


def log(msg):
    tag = getattr(_tls, "tag", "")
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}]{tag} {msg}", flush=True)


def _environment_proxy():
    return (os.environ.get("CHROME_PROXY") or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY") or "").strip()


def _normalize_proxy(value):
    """校验并规范化 HTTP/HTTPS 代理地址；host:port 默认按 HTTP 处理。"""
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("端口格式无效") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("仅支持 http:// 或 https:// 代理")
    if not parsed.hostname or port is None or not (1 <= port <= 65535):
        raise ValueError("代理地址必须包含有效的主机和端口")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    return value


def _mask_proxy(proxy):
    """隐藏代理 URL 中的用户名和密码，避免凭据出现在控制台。"""
    if not proxy:
        return "直连"
    prefix, separator, rest = proxy.partition("://")
    if not separator or "@" not in rest:
        return proxy
    credentials, host = rest.rsplit("@", 1)
    masked = "***:***" if ":" in credentials else "***"
    return f"{prefix}://{masked}@{host}"


def _load_proxy_preference():
    """读取本地选择；无有效本地配置时回退到环境变量。"""
    if os.path.isfile(_PROXY_CONFIG_PATH):
        try:
            with open(_PROXY_CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("配置根节点必须是对象")
            if data.get("mode") == "direct":
                return ""
            if data.get("mode") == "proxy":
                proxy = _normalize_proxy(str(data.get("proxy", "")))
                if not proxy:
                    raise ValueError("proxy 模式缺少代理地址")
                return proxy
            raise ValueError("mode 必须是 proxy 或 direct")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log(f"[!] 本地代理配置无效，改用环境变量: {exc}")
    env_proxy = _environment_proxy()
    if not env_proxy:
        return ""
    try:
        return _normalize_proxy(env_proxy)
    except ValueError as exc:
        log(f"[!] 环境变量代理无效，本轮默认直连: {exc}")
        return ""


def _save_proxy_preference(proxy):
    data = {"mode": "proxy", "proxy": proxy} if proxy else {"mode": "direct", "proxy": ""}
    temp_path = f"{_PROXY_CONFIG_PATH}.{os.getpid()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, _PROXY_CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _choose_proxy():
    default = _load_proxy_preference()
    while True:
        shown = _mask_proxy(default)
        value = _ask(f"④ 代理地址 [默认 {shown}；输入 direct 直连]: ", "")
        if not value:
            proxy = default
        elif value.lower() == "direct":
            proxy = ""
        else:
            try:
                proxy = _normalize_proxy(value)
            except ValueError as exc:
                print(f"   代理地址无效: {exc}")
                continue
        _save_proxy_preference(proxy)
        print(f"   使用代理: {_mask_proxy(proxy)}")
        return proxy


def _apply_proxy(proxy):
    """将统一代理应用到浏览器和当前进程内的所有 requests 请求。"""
    global BROWSER_PROXY
    BROWSER_PROXY = proxy
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                  "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    for key in proxy_keys:
        os.environ.pop(key, None)
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = proxy
        for key in ("NO_PROXY", "no_proxy"):
            os.environ[key] = "localhost,127.0.0.1,::1"


def _apply_browser_proxy(options):
    if BROWSER_PROXY:
        options.set_proxy(BROWSER_PROXY)


def _retry(fn, tries=3, delay=2.0, what=""):
    """通用重试：捕获异常，退避后重试；用尽则抛最后一次异常。"""
    last = None
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries:
                log(f"[retry {i}/{tries}] {what or 'op'} 失败: {str(e)[:100]}，{delay:.0f}s 后重试")
                time.sleep(delay)
    raise last


# ── 临时邮箱（YYDS Mail 协议）────────────────────────────
def yyds_create_mailbox():
    if not YYDS_KEY:
        raise RuntimeError("未配置 YYDS_API_KEY 环境变量")

    def _do():
        local = "ps" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        payload = {"localPart": local}
        if YYDS_DOMAIN:
            payload["domain"] = YYDS_DOMAIN
        r = requests.post(f"{YYDS_BASE}/accounts",
                          headers={"X-API-Key": YYDS_KEY, "Content-Type": "application/json"},
                          json=payload, timeout=20)
        r.raise_for_status()
        return r.json()["data"]
    d = _retry(_do, tries=3, what="建邮箱")
    log(f"临时邮箱: {d['address']}")
    return d["address"], d["token"]


def yyds_wait_code(address, timeout=180, interval=5):
    """轮询收件箱，从 HTML 正文抽取 ProxyScrape 的验证码。
    注意：码是 10 位字母数字混合（如 d253ff02f7），不是纯数字；
    且 text 版只写 'open the HTML version'，必须解析 html 字段。"""
    deadline = time.time() + timeout
    hdr = {"X-API-Key": YYDS_KEY}
    while time.time() < deadline:
        lst = requests.get(f"{YYDS_BASE}/messages", headers=hdr,
                           params={"address": address, "limit": 5}, timeout=30).json()
        for m in lst.get("data", {}).get("messages", []):
            d = requests.get(f"{YYDS_BASE}/messages/{m['id']}", headers=hdr,
                             params={"address": address}, timeout=30).json().get("data", {})
            html = " ".join(d.get("html") or [])
            txt = _html.unescape(re.sub(r"<[^>]+>", " ", html))
            mo = re.search(r"verification code:\s*([A-Za-z0-9]{6,})", txt, re.I)
            if mo:
                log(f"收到验证码: {mo.group(1)}  (主题: {d.get('subject')})")
                return mo.group(1)
        time.sleep(interval)
    raise TimeoutError("等验证码超时")


def _load_mail_client(provider):
    from mail_providers import create_mail_client
    return create_mail_client({"mail": {"provider": provider}})


def create_mailbox(provider):
    if provider == "yyds":
        email, token = yyds_create_mailbox()
        return email, token, None, provider
    client = _load_mail_client(provider)
    info = client.create_address(logger=log)
    actual_provider = getattr(client, "provider_id", provider)
    email = info["email"]
    log(f"临时邮箱: {email}（{actual_provider}）")
    return email, info.get("mail_token"), client, actual_provider


def _extract_verification_code(message):
    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    text = _html.unescape(re.sub(r"<[^>]+>", " ", " ".join(strings(message))))
    match = re.search(r"verification code:\s*([A-Za-z0-9]{6,})", text, re.I)
    return match.group(1) if match else None


def wait_mail_code(address, token, client, provider, timeout=180, interval=5):
    if provider == "yyds":
        return yyds_wait_code(address, timeout=timeout, interval=interval)
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        try:
            messages = client.list_messages(token)
            for item in messages:
                if not isinstance(item, dict):
                    continue
                mid = next((item.get(key) for key in
                            ("id", "@id", "messageID", "message_id", "msgid", "uid", "mail_id", "_id")
                            if item.get(key)), None)
                key = str(mid or item)
                if key in seen:
                    continue
                detail = client.get_message(token, mid) if mid else None
                message = detail if isinstance(detail, dict) else item
                code = _extract_verification_code(message)
                seen.add(key)
                if code:
                    log(f"收到验证码: {code}  (主题: {message.get('subject')})")
                    return code
        except Exception as exc:
            log(f"[邮箱 {provider}] 收信失败: {exc}")
        time.sleep(interval)
    raise TimeoutError("等验证码超时")


# ── 本地打码：浏览器只出 token ───────────────────────────
# 触发 widget 挂载：填占位表单（token 不绑定表单内容，随便填合法值即可）
_FILL_JS = r"""
function setVal(el,val){
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(el,val);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
}
var email=document.querySelector('input[type=email]');
var pwds=document.querySelectorAll('input[type=password]');
var chk=document.querySelector('input[type=checkbox]');
if(!email || pwds.length < 2 || !chk) return 'missing-form';
if(email) setVal(email,'warmup'+Date.now()+'@example.com');
if(pwds[0]) setVal(pwds[0],'Warmup123!9');
if(pwds[1]) setVal(pwds[1],'Warmup123!9');
if(chk&&!chk.checked) chk.click();
return 'filled';
"""

# 读 token：优先隐藏字段，其次 turnstile.getResponse()
_GET_TOKEN_JS = r"""
try{
  var v=String((document.querySelector('input[name="cf-turnstile-response"]')||{}).value||'').trim();
  if(v) return v;
  if(window.turnstile && typeof turnstile.getResponse==='function')
    return String(turnstile.getResponse()||'').trim();
  return '';
}catch(e){ return ''; }
"""

# 注入进 turnstile iframe，伪装真实鼠标屏幕坐标（反自动化检测）
_SCREEN_INJECT_JS = r"""
window.dtp=1;
function ri(a,b){return Math.floor(Math.random()*(b-a+1))+a;}
Object.defineProperty(MouseEvent.prototype,'screenX',{value:ri(800,1200)});
Object.defineProperty(MouseEvent.prototype,'screenY',{value:ri(400,700)});
"""


def solve_turnstile(headless=False, timeout=90):
    """打开真实 sign-up 页，用 DrissionPage 的 shadow_root API 逐层钻进
    closed shadow DOM 点 checkbox，拿到 cf-turnstile-response。"""
    from DrissionPage import Chromium, ChromiumOptions

    from browser_tmp import browser_tmp_root

    opts = ChromiumOptions()
    # 必须在 auto_port() 之前设置：auto_port 会立刻用 tmp_path 定位端口数据目录，
    # 该目录随后被当作浏览器 user-data-dir，之后再改就不生效了。
    opts.set_tmp_path(browser_tmp_root())
    opts.auto_port()  # 每个实例独立端口 + 独立临时用户目录（支持并发多开）
    opts.set_load_mode("eager")
    opts.set_timeouts(base=3, page_load=30, script=30)
    if BROWSER_PATH:
        opts.set_browser_path(BROWSER_PATH)
    _apply_browser_proxy(opts)
    for flag in ("--no-first-run", "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-background-networking", "--mute-audio",
                 "--disable-gpu", "--window-size=1280,900"):
        opts.set_argument(flag)
    if headless:
        # 真 headless 过不了 Turnstile（Cloudflare 检测无头）。改用「隐形有头」：
        # 有头浏览器保证过检，窗口挪到屏幕外，启动后再 hide()，用户完全看不见。
        opts.set_argument("--window-position=-32000,-32000")
    if os.path.exists(EXTENSION_PATH):
        opts.add_extension(EXTENSION_PATH)
    else:
        log(f"[!] 找不到 turnstilePatch 扩展: {EXTENSION_PATH}")

    browser = Chromium(opts)
    try:
        tab = browser.new_tab()
        browser.close_tabs(tab, others=True)
        if headless:
            try:
                tab.set.window.hide()   # Windows 下真正隐藏窗口，进程照常渲染，Turnstile 不受影响
            except Exception:
                pass
        log("浏览器打开 sign-up 页…")
        for attempt in range(1, 4):
            tab.get(PS_SIGNUP_PAGE, retry=0, timeout=30)
            if str(tab.url).startswith(PS_BASE):
                break
            log(f"注册页第 {attempt}/3 次加载失败: {tab.url}")
            time.sleep(2)
        else:
            raise RuntimeError(f"注册页加载失败，当前地址: {tab.url}")
        time.sleep(5)

        # 填占位表单触发 widget 挂载（不填不 render）
        fill_state = tab.run_js(_FILL_JS)
        if fill_state != "filled":
            raise RuntimeError(f"注册页表单未就绪: {fill_state} ({tab.url})")
        log("表单已填，预热 Turnstile…")
        time.sleep(2)
        try:
            tab.run_js("try{if(window.turnstile&&turnstile.reset)turnstile.reset()}catch(e){}")
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not str(tab.url).startswith(PS_BASE):
                raise RuntimeError(f"注册页意外离开: {tab.url}")
            token = str(tab.run_js(_GET_TOKEN_JS) or "").strip()
            if len(token) >= 80:
                log(f"Turnstile 通过，token 长度={len(token)}")
                return token

            # 逐层进 shadow DOM 点 checkbox
            ci = tab.ele("@name=cf-turnstile-response", timeout=2)
            if ci:
                try:
                    wrapper = ci.parent()
                    host = wrapper.ele("tag:div", timeout=2)
                    iframe = host.shadow_root.ele("tag:iframe", timeout=2)
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(_SCREEN_INJECT_JS)
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input", timeout=2)
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            time.sleep(1.2)
        try:
            state = tab.run_js(r"""
return {
  url: location.href,
  title: document.title,
  body: String(document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 300),
  hiddenInput: !!document.querySelector('input[name="cf-turnstile-response"]'),
  iframeCount: document.querySelectorAll('iframe').length,
  turnstile: typeof window.turnstile
};
""")
            screenshot = os.path.join(_BASE, "screenshots", f"turnstile_{int(time.time())}.png")
            os.makedirs(os.path.dirname(screenshot), exist_ok=True)
            tab.get_screenshot(path=screenshot)
            log(f"Turnstile 超时状态: {state}；截图: {screenshot}")
        except Exception as exc:
            log(f"Turnstile 超时取证失败: {exc}")
        raise TimeoutError("Turnstile 求解超时")
    finally:
        try:
            browser.quit()
        except Exception:
            pass


# ── 注册（协议）─────────────────────────────────────────
def register(email, password, turnstile_token):
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.post(PS_REGISTER, data={
        "email": email,
        "password": password,
        "cf_turnstile_token": turnstile_token,
    }, timeout=30)
    log(f"注册响应 {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"注册未返回 access_token: {data}")
    log(f"注册成功，access_token 前缀: {token[:24]}…")
    return s, token, data.get("userData", {})


def resend_code(session, access_token):
    """触发发送/重发验证码（注册后不会自动发，必须调一次）。"""
    r = session.post(PS_RESEND, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    log(f"resend 触发: {r.status_code}")
    return r.ok


def verify_email(session, access_token, code):
    # 关键：字段名是 verificationCode（不是 code），且服务端要 multipart/form-data
    r = session.post(PS_VERIFY_EMAIL,
                     headers={"Authorization": f"Bearer {access_token}"},
                     files={"verificationCode": (None, code)}, timeout=30)
    log(f"验邮箱响应 {r.status_code}: {r.text[:200]}")
    return r.ok


def whoami(session, access_token):
    r = session.post(PS_ME, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    if r.ok:
        log(f"/me 校验成功: {json.dumps(r.json(), ensure_ascii=False)[:300]}")
    else:
        log(f"/me 失败 {r.status_code}: {r.text[:200]}")
    return r.ok


# ── 拉取免费 datacenter 代理 ────────────────────────────
def claim_trial(access_token):
    """注册接口只建账号，Premium trial 子账号需在邮箱验证后显式领取
    （dashboard 的 onboarding 向导同样调 claim-trial）。已领取时复用现有子账号。
    返回 trial 子账号 ID，未获得返回空串。"""
    h = {"Authorization": f"Bearer {access_token}", "User-Agent": UA,
         "Origin": PS_BASE, "Referer": f"{PS_BASE}/v2/onboarding"}

    def _claim():
        r = requests.post(PS_CLAIM_TRIAL, headers=h, timeout=30)
        if r.ok and r.json().get("success"):
            log("Premium trial 已激活")
            return r.json().get("account_id") or ""
        if "already claimed" in r.text.lower():
            log("Premium trial 此前已领取")
            return ""
        raise RuntimeError(f"claim-trial {r.status_code}: {r.text[:120]}")

    try:
        aid = _retry(_claim, tries=2, delay=3, what="claim-trial")
    except Exception as exc:
        log(f"[!] claim-trial 失败: {str(exc)[:120]}")
        aid = ""
    if aid:
        return aid

    def _me():
        r = requests.post(PS_ME, headers=h, timeout=25)
        r.raise_for_status()
        subs = r.json().get("associatedSubaccounts") or []
        return subs[0].get("AccountID") if subs else ""

    return _retry(_me, tries=3, delay=3, what="me")


def fetch_proxies(access_token, account_id):
    """trial 子账号自带 100 个 datacenter 共享代理。
    从 overview 拿账密，从 proxy-list 端点拿 ip:port 列表。"""
    h = {"Authorization": f"Bearer {access_token}", "User-Agent": UA, "Origin": PS_BASE}

    def _overview():
        r = requests.get(f"{PS_BASE}/v2/v4/account/{account_id}/services/overview",
                         headers=h, timeout=25)
        r.raise_for_status()
        data = r.json().get("data")
        if not data or "services" not in data:
            raise RuntimeError(f"overview 无数据（trial 可能未激活）: {str(r.text)[:80]}")
        return data

    def _list():
        r = requests.get(f"{PS_BASE}/v2/v4/account/{account_id}/datacenter_shared/proxy-list",
                         headers=h, params={"protocol": "http", "format": "normal"}, timeout=25)
        r.raise_for_status()
        return r.text

    ov = _retry(_overview, tries=3, delay=3, what="overview")
    ds = ov["services"]["datacenter_shared"]
    user, pwd = ds["proxy_username"], ds["proxy_password"]
    txt = _retry(_list, tries=3, delay=3, what="proxy-list")
    lst = [x.strip() for x in txt.split() if ":" in x]
    if not lst:
        raise RuntimeError("proxy-list 为空")
    return user, pwd, lst


def save_proxies(user, pwd, proxies, path):
    """追加写入本轮代理文件，格式 http://user:pass@ip:port（可直接喂给多数工具）。"""
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            for ip in proxies:
                f.write(f"http://{user}:{pwd}@{ip}\n")


# ── 单个账号注册（并发 worker）───────────────────────────
def save_account(rec, path):
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _register_once(headless, node_file, mail_provider):
    """单次尝试：建邮箱→打码→注册→验证→拉代理。返回 rec（不落盘账号）。
    建邮箱/打码/注册任一失败抛异常，交给外层重试。"""
    password = "Ps" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!9"
    email, mail_token, mail_client, actual_mail_provider = create_mailbox(mail_provider)
    token = solve_turnstile(headless=headless)     # 失败抛异常 → 外层重试
    session, access_token, userdata = register(email, password, token)  # 同上

    verified = False
    try:
        resend_code(session, access_token)
        code = wait_mail_code(email, mail_token, mail_client, actual_mail_provider, timeout=180)
        verified = verify_email(session, access_token, code)
    except Exception as e:
        log(f"[!] 邮箱验证环节: {e}（账号已注册，token 有效）")

    # 拉免费 datacenter 代理（Premium trial 自带 100 个）——须先验证邮箱才激活
    p_user = p_pass = ""
    p_count = 0
    if not verified:
        log("邮箱未验证，trial 未激活，跳过拉代理")
    else:
        try:
            subs = userdata.get("associatedSubaccounts") or []
            aid = subs[0].get("AccountID") if subs else None
            if not aid:
                aid = claim_trial(access_token)
            if aid:
                p_user, p_pass, plist = fetch_proxies(access_token, aid)
                save_proxies(p_user, p_pass, plist, node_file)
                p_count = len(plist)
                log(f"拉取代理 {p_count} 个")
            else:
                log("当前邮箱未获试用代理服务")
        except Exception as e:
            log(f"[!] 拉代理失败: {e}")

    return {
        "email": email, "password": password,
        "mail_provider": actual_mail_provider,
        "access_token": access_token, "userData": userdata,
        "verified": verified,
        "proxy_username": p_user, "proxy_password": p_pass, "proxy_count": p_count,
        "ts": int(time.time()),
    }


def register_one(idx, headless, acc_file, node_file, mail_provider, max_attempts=3):
    """账号级重试：任一步异常或没拿到代理，就换新邮箱重来，直到成功或用尽。"""
    _tls.tag = f" #{idx}"
    last = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"—— 第 {attempt}/{max_attempts} 次尝试 ——")
        try:
            attempt_provider = (AUTO_MAIL_PROVIDERS[(attempt - 1) % len(AUTO_MAIL_PROVIDERS)]
                                if mail_provider == "auto_zero_config" else mail_provider)
            if mail_provider == "auto_zero_config":
                log(f"自动邮箱本次使用: {attempt_provider}")
            rec = _register_once(headless, node_file, attempt_provider)
        except Exception as e:
            log(f"[x] 本次尝试失败: {str(e)[:120]}")
            rec = None

        if rec:
            last = rec
            if rec.get("proxy_count", 0) > 0:
                save_account(rec, acc_file)
                log(f"[✓] 完成  verified={rec['verified']}  proxies={rec['proxy_count']}  {rec['email']}")
                return rec
            log("注册成功但未拿到代理，换邮箱重试")

    if last:
        save_account(last, acc_file)
        log(f"[!] 重试用尽，存半成品  verified={last['verified']}  proxies={last.get('proxy_count',0)}  {last['email']}")
    else:
        log(f"[x] {max_attempts} 次尝试均失败，放弃 #{idx}")
    return last


# ── 启动引导 ────────────────────────────────────────────
def _ask(prompt, default):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def guide():
    print("=" * 52)
    print("   ProxyScrape 批量注册机  ·  本地打码走协议")
    print("   浏览器只出 Turnstile token，注册/收信/验证全走 HTTP")
    print("   临时邮箱: 可选渠道    账号→account/  代理→node/")
    print("=" * 52)
    try:
        count = int(_ask("① 注册数量        [默认 5，输 0 退出]: ", "5"))
    except ValueError:
        count = 5
    if count <= 0:
        return count, 1, True, MAIL_CHANNELS[0][0], ""
    try:
        threads = int(_ask("② 并发线程        [默认 3]: ", "3"))
    except ValueError:
        threads = 3
    hl = _ask("③ 隐藏浏览器窗口   [Y/n]: ", "Y").lower()
    headless = not hl.startswith("n")
    proxy = _choose_proxy()
    print("⑤ 邮箱渠道:")
    for index, (_, name) in enumerate(MAIL_CHANNELS, 1):
        print(f"   {index}. {name}")
    while True:
        selected = _ask("   请选择 [默认 1]: ", "1")
        if selected.isdigit() and 1 <= int(selected) <= len(MAIL_CHANNELS):
            mail_provider, mail_name = MAIL_CHANNELS[int(selected) - 1]
            break
        print("   选择无效，请输入菜单编号。")
    threads = max(1, min(threads, count, 8))  # 并发上限 8，别把机器压垮
    print("-" * 52)
    print(f"  → 注册 {count} 个 · 并发 {threads} · {'隐藏窗口' if headless else '显示窗口'} · {mail_name}")
    print("-" * 52)
    return count, threads, headless, mail_provider, proxy


def run_round(count, threads, headless, mail_provider):
    # 每轮独立文件（时间戳命名），不追加旧文件
    ts = time.strftime("%Y%m%d_%H%M%S")
    acc_file = os.path.join(_ACCOUNT_DIR, f"accounts_{ts}.jsonl")
    node_file = os.path.join(_NODE_DIR, f"proxies_{ts}.txt")
    t0 = time.time()
    ok = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(register_one, i + 1, headless, acc_file, node_file, mail_provider): i + 1
                for i in range(count)}
        for fu in as_completed(futs):
            try:
                r = fu.result()
            except Exception as e:
                r = None
                with _print_lock:
                    print(f"[worker error] {e}", flush=True)
            if r:
                ok.append(r)

    dt = time.time() - t0
    total_proxies = sum(r.get("proxy_count", 0) for r in ok)
    success = [r for r in ok if r.get("proxy_count", 0) > 0]
    partial = [r for r in ok if r.get("proxy_count", 0) <= 0]
    failed = count - len(ok)
    print("\n" + "=" * 52)
    print(f"  用时 {dt:.0f}s  ·  成功 {len(success)}  ·  半成品 {len(partial)}  ·  失败 {failed}  /  共 {count}")
    print(f"  代理节点共 {total_proxies} 个")
    print(f"  账号 → account/{os.path.basename(acc_file)}")
    print(f"  代理 → node/{os.path.basename(node_file)}")
    for r in ok:
        print(f"    {r['email']}  |  {r['password']}  |  verified={r['verified']}  |  proxies={r.get('proxy_count',0)}")
    print("=" * 52 + "\n")


def main():
    # 跑完一轮即结束，等用户按键退出（不再回引导循环）
    count, threads, headless, mail_provider, proxy = guide()
    if count <= 0:
        print("已退出。")
        return 0
    _apply_proxy(proxy)
    run_round(count, threads, headless, mail_provider)
    try:
        input("按任意键退出…")
    except EOFError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
