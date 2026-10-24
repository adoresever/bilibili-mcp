"""B站网页扫码登录（修复新版 passport 接口凭证丢失问题）。

背景
----
B 站新版 passport 扫码接口在手机端确认登录后，`SESSDATA` / `bili_jct` /
`DedeUserID` 等登录 cookie 主要通过**响应头 `Set-Cookie`** 下发，
`data.url` 可能为空；而 `bilibili-api` 的 `QrCodeLogin` 只解析
`data.url` 的查询参数，导致出现「登录成功但凭证为空」——表现为扫码确认后
所有工具仍提示未登录。

本模块直接请求 passport 接口，同时解析：
1. 响应头 `Set-Cookie`；
2. 响应 JSON 中 `data.url` 的查询参数（兼容旧版接口行为）；
并在登录成功后保存 `refresh_token`（`ac_time_value`），用于后续自动续期，
实现真正意义上的「持久登录」。

用法
----
```python
from bili_login_flow import QrCodeLogin, QrCodeLoginEvents, save_credential

qr = QrCodeLogin()
await qr.generate_qrcode()
print(qr.get_qrcode_terminal())        # 终端二维码
png: bytes = qr.get_qrcode_picture()   # PNG 图片
while (state := await qr.check_state()) not in (
    QrCodeLoginEvents.DONE,
    QrCodeLoginEvents.TIMEOUT,
):
    await asyncio.sleep(2)
if state == QrCodeLoginEvents.DONE:
    save_credential(qr.get_credential())
```
"""

from __future__ import annotations

import asyncio
import enum
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import aiohttp
import qrcode
import qrcode_terminal

CRED_FILE = Path(__file__).parent / "bili_credential.json"

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BUVID_URL = "https://api.bilibili.com/x/frontend/finger/spi"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


class QrCodeLoginEvents(enum.Enum):
    """扫码状态（含义与 B 站 passport 接口返回码一致）"""

    SCAN = "scan"  # 86101：未扫码
    CONF = "conf"  # 86090：已扫码，等待手机确认
    TIMEOUT = "timeout"  # 86038：二维码已失效
    DONE = "done"  # 0：登录成功


@dataclass
class QrCodeCredential:
    """扫码登录成功后得到的凭证"""

    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    buvid4: str = ""
    dedeuserid: str = ""
    ac_time_value: str = ""  # refresh_token，用于持久登录/自动续期

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "QrCodeCredential":
        return cls(
            sessdata=data.get("sessdata", ""),
            bili_jct=data.get("bili_jct", ""),
            buvid3=data.get("buvid3", ""),
            buvid4=data.get("buvid4", ""),
            dedeuserid=data.get("dedeuserid", ""),
            ac_time_value=data.get("ac_time_value", ""),
        )


class QrCodeLogin:
    """网页扫码登录会话。

    一次实例对应一次扫码流程：generate_qrcode() 生成二维码，
    check_state() 轮询状态，登录成功后 get_credential() 取凭证。
    用完请调用 close() 释放连接（MCP Server 中为进程级长连接，务必在
    成功/超时后关闭，避免连接泄漏）。
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._qrcode_key = ""
        self._qr_link = ""
        self._qr_picture: bytes = b""
        self._qr_terminal = ""
        self._credential: Optional[QrCodeCredential] = None

    # ---------- 内部工具 ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS)
        return self._session

    @staticmethod
    def _cookies_from_headers(raw_headers: list[str]) -> dict[str, str]:
        """把原始 Set-Cookie 列表解析成 {name: value}，仅用于兜底。"""
        cookies: dict[str, str] = {}
        for raw in raw_headers:
            pair = raw.split(";", 1)[0]
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookies[name.strip()] = value
        return cookies

    async def _fetch_buvid(self) -> tuple[str, str]:
        """补全 buvid3/buvid4（部分接口需要）。"""
        try:
            session = await self._get_session()
            async with session.get(BUVID_URL) as resp:
                data = (await resp.json()).get("data") or {}
            return data.get("b_3", ""), data.get("b_4", "")
        except Exception:
            return "", ""

    # ---------- 对外接口 ----------

    def has_qrcode(self) -> bool:
        return bool(self._qr_link)

    def has_done(self) -> bool:
        return self._credential is not None

    async def generate_qrcode(self) -> None:
        """申请二维码并生成本地 PNG / 终端二维码。"""
        session = await self._get_session()
        async with session.get(
            GENERATE_URL, params={"source": "main-fe-header"}
        ) as resp:
            payload = await resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"申请登录二维码失败: {payload.get('code')} {payload.get('message')}"
            )

        data = payload["data"]
        self._qrcode_key = data["qrcode_key"]
        self._qr_link = data["url"]

        image = qrcode.make(self._qr_link)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self._qr_picture = buffer.getvalue()
        self._qr_terminal = qrcode_terminal.qr_terminal_str(self._qr_link)

    def get_qrcode_picture(self) -> bytes:
        """二维码 PNG 图片（bytes）。"""
        return self._qr_picture

    def get_qrcode_terminal(self) -> str:
        """二维码终端文本版。"""
        return self._qr_terminal

    def get_qrcode_url(self) -> str:
        """二维码内容 URL。"""
        return self._qr_link

    def get_credential(self) -> QrCodeCredential:
        if self._credential is None:
            raise RuntimeError("尚未登录成功，无法获取凭证")
        return self._credential

    async def check_state(self) -> QrCodeLoginEvents:
        """轮询一次扫码状态。"""
        session = await self._get_session()
        async with session.get(
            POLL_URL, params={"qrcode_key": self._qrcode_key, "source": "main-fe-header"}
        ) as resp:
            payload = await resp.json()
            raw_set_cookie = resp.headers.getall("Set-Cookie", [])

        data = payload.get("data") or {}
        code = data.get("code")
        if code == 86101:
            return QrCodeLoginEvents.SCAN
        if code == 86090:
            return QrCodeLoginEvents.CONF
        if code == 86038:
            return QrCodeLoginEvents.TIMEOUT

        # code == 0：登录成功。新版接口 cookie 在 Set-Cookie，旧版在 data.url 查询参数，两边都取。
        cookies: dict[str, str] = {c.key: c.value for c in session.cookie_jar}
        cookies.update(self._cookies_from_headers(raw_set_cookie))
        if data.get("url"):
            cookies.update(
                {k: v[0] for k, v in parse_qs(urlsplit(data["url"]).query).items()}
            )

        credential = QrCodeCredential(
            sessdata=cookies.get("SESSDATA", ""),
            bili_jct=cookies.get("bili_jct", ""),
            buvid3=cookies.get("buvid3", ""),
            buvid4=cookies.get("buvid4", ""),
            dedeuserid=cookies.get("DedeUserID", ""),
            ac_time_value=data.get("refresh_token", ""),
        )
        if not credential.sessdata:
            raise RuntimeError(
                "登录成功但未解析到 SESSDATA：请检查 B 站 passport 接口返回格式是否再次变更"
            )

        if not credential.buvid3 or not credential.buvid4:
            buvid3, buvid4 = await self._fetch_buvid()
            credential.buvid3 = credential.buvid3 or buvid3
            credential.buvid4 = credential.buvid4 or buvid4

        self._credential = credential
        return QrCodeLoginEvents.DONE

    async def close(self) -> None:
        """释放底层 HTTP 连接。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def load_credential(path: Path = CRED_FILE) -> Optional[QrCodeCredential]:
    """从文件加载凭证，不存在时返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return QrCodeCredential.from_dict(data)


def save_credential(credential: QrCodeCredential, path: Path = CRED_FILE) -> None:
    """保存凭证到文件（包含 refresh_token，供持久登录使用）。"""
    path.write_text(
        json.dumps(credential.to_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
