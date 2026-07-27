import html
import random
import re
from urllib.parse import quote, urlencode

import requests


ZERO_CONFIG_PROVIDERS = (
    "tempmail_lol",
    "fce_areueally",
    "fce_ditpay",
    "gonebox",
)

MAIL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def generate_local_name():
    return f"mail{random.randint(100000, 9999999999)}"


def decode_messages(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("hydra:member", "messages", "messageData", "emails", "items", "data", "msgs"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                messages = decode_messages(value)
                if messages:
                    return messages
    return []


def nested_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return "\n".join(filter(None, map(nested_text, value.values())))
    if isinstance(value, list):
        return "\n".join(filter(None, map(nested_text, value)))
    return str(value)


def with_text(message):
    result = dict(message or {})
    result.setdefault("text", nested_text(result))
    return result


def with_provider_metadata(info, provider_id, state=None):
    result = dict(info or {})
    result.setdefault("email_password", "")
    result["mail_provider"] = provider_id
    if state is not None:
        result["mail_state"] = state
    return result


class ZeroConfigMailClient:
    provider_id = "zero_config"
    display_name = "ZeroConfig"

    def __init__(self, _config=None):
        self.session = requests.Session()
        self.address = ""
        self.mail_token = ""

    def headers(self, **extra):
        headers = {
            "User-Agent": MAIL_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        headers.update({key: value for key, value in extra.items() if value})
        return headers


class TempMailLOLClient(ZeroConfigMailClient):
    provider_id = "tempmail_lol"
    display_name = "TempMail.lol"
    api_base = "https://api.tempmail.lol"

    def create_address(self, logger=None, stop_event=None):
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        response = self.session.get(
            f"{self.api_base}/v2/inbox/create", headers=self.headers(), timeout=12
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"创建 TempMail.lol 邮箱 HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        self.address = str(payload.get("address") or "").strip().lower()
        self.mail_token = str(payload.get("token") or "").strip()
        if not self.address or not self.mail_token:
            raise RuntimeError("TempMail.lol 创建响应缺少 address/token")
        return with_provider_metadata(
            {"email": self.address, "mail_token": self.mail_token},
            self.provider_id,
            {"token": self.mail_token},
        )

    def list_messages(self, mail_token=None):
        token = mail_token or self.mail_token
        response = self.session.get(
            f"{self.api_base}/v2/inbox?{urlencode({'token': token})}",
            headers=self.headers(),
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"获取 TempMail.lol 邮件列表 HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        if payload.get("expired"):
            raise RuntimeError("TempMail.lol 邮箱已过期")
        return decode_messages(payload)

    def get_message(self, _mail_token, _message_id):
        return None


class FreeCustomClient(ZeroConfigMailClient):
    provider_id = "freecustom"
    display_name = "FreeCustom"
    api_base = "https://www.freecustom.email"
    domain = ""

    def api_url(self, path, params=None):
        url = self.api_base.rstrip("/") + "/" + path.lstrip("/")
        return url + ("?" + urlencode(params) if params else "")

    def fce_headers(self, auth=False):
        headers = self.headers(
            Referer="https://www.freecustom.email/en",
            **{"x-fce-client": "web-client"},
        )
        if auth and self.mail_token:
            headers["Authorization"] = f"Bearer {self.mail_token}"
        return headers

    def ensure_token(self):
        if self.mail_token:
            return self.mail_token
        response = self.session.post(self.api_url("/api/auth"), headers=self.fce_headers(), timeout=45)
        if response.status_code != 200:
            raise RuntimeError(f"FreeCustom auth HTTP {response.status_code}: {response.text[:200]}")
        self.mail_token = str(response.json().get("token") or "").strip()
        if not self.mail_token:
            raise RuntimeError("FreeCustom auth 响应缺少 token")
        return self.mail_token

    def create_address(self, logger=None, stop_event=None):
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        self.ensure_token()
        local = re.sub(r"[^a-z0-9._-]+", "", generate_local_name().lower()).strip("._-")
        self.address = f"{local}@{self.domain}"
        self.list_messages(self.mail_token)
        if logger:
            logger(f"[{self.display_name}] 邮箱生成成功: {self.address}")
        return with_provider_metadata(
            {"email": self.address, "mail_token": self.mail_token},
            self.provider_id,
            {"token": self.mail_token, "domain": self.domain},
        )

    def list_messages(self, mail_token=None):
        if mail_token:
            self.mail_token = mail_token
        self.ensure_token()
        if not self.address:
            raise RuntimeError("FreeCustom 邮箱未创建")
        response = self.session.get(
            self.api_url("/api/public-mailbox", {"fullMailboxId": self.address}),
            headers=self.fce_headers(auth=True),
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"FreeCustom 邮件列表 HTTP {response.status_code}: {response.text[:200]}")
        return [with_text(item) for item in decode_messages(response.json().get("data"))]

    def get_message(self, mail_token, message_id):
        if mail_token:
            self.mail_token = mail_token
        if not self.address or not message_id:
            return None
        response = self.session.get(
            self.api_url(
                "/api/public-mailbox",
                {"fullMailboxId": self.address, "messageId": str(message_id).strip()},
            ),
            headers=self.fce_headers(auth=True),
            timeout=45,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        return with_text(data if isinstance(data, dict) else {"text": nested_text(payload)})


class FreeCustomAreueallyClient(FreeCustomClient):
    provider_id = "fce_areueally"
    display_name = "FreeCustom areueally"
    domain = "areueally.info"


class FreeCustomDitpayClient(FreeCustomClient):
    provider_id = "fce_ditpay"
    display_name = "FreeCustom ditpay"
    domain = "ditpay.info"


class GoneBoxClient(ZeroConfigMailClient):
    provider_id = "gonebox"
    display_name = "GoneBox"
    api_base = "https://api.gonebox.email/api/v1"

    def url(self, path):
        return self.api_base.rstrip("/") + "/" + path.lstrip("/")

    def create_address(self, logger=None, stop_event=None):
        if stop_event and stop_event.is_set():
            raise RuntimeError("任务已停止")
        response = self.session.post(
            self.url("/inboxes"),
            json={"domain": "gonebox.email"},
            headers=self.headers(Referer="https://gonebox.email/"),
            timeout=45,
        )
        if response.status_code // 100 != 2:
            raise RuntimeError(f"GoneBox 创建邮箱 HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        self.address = str((data or {}).get("address") or "").strip().lower()
        if not payload.get("success") or "@" not in self.address:
            raise RuntimeError("GoneBox 未返回邮箱地址")
        self.mail_token = self.address
        if logger:
            logger(f"[{self.display_name}] 邮箱生成成功: {self.address}")
        return with_provider_metadata(
            {"email": self.address, "mail_token": self.address},
            self.provider_id,
            {"address": self.address},
        )

    def list_messages(self, mail_token=None):
        if mail_token and "@" in str(mail_token):
            self.address = str(mail_token).strip().lower()
        if not self.address:
            raise RuntimeError("GoneBox 邮箱未创建")
        response = self.session.get(
            self.url("/inboxes/" + quote(self.address) + "/messages"),
            headers=self.headers(Referer="https://gonebox.email/"),
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"GoneBox 列表 HTTP {response.status_code}: {response.text[:200]}")
        return [with_text(item) for item in decode_messages(response.json())]

    def get_message(self, _mail_token, message_id):
        if not message_id:
            return None
        response = self.session.get(
            self.url("/messages/" + quote(str(message_id).strip())),
            headers=self.headers(Referer="https://gonebox.email/"),
            timeout=45,
        )
        if response.status_code != 200:
            return None
        try:
            return with_text(response.json())
        except ValueError:
            return {"id": message_id, "text": response.text, "html": response.text}


ZERO_CONFIG_PROVIDER_FACTORIES = {
    "tempmail_lol": TempMailLOLClient,
    "fce_areueally": FreeCustomAreueallyClient,
    "fce_ditpay": FreeCustomDitpayClient,
    "gonebox": GoneBoxClient,
}


def create_mail_client(config):
    mail = (config or {}).get("mail") or {}
    provider = str(mail.get("provider") or "").strip().lower().replace("-", "_")
    factory = ZERO_CONFIG_PROVIDER_FACTORIES.get(provider)
    if not factory:
        raise RuntimeError(f"未知邮箱渠道: {provider}")
    return factory(config)
