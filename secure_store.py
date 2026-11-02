"""加密保存 B 站登录凭证。

凭证（SESSDATA 等 cookie）只以密文形式落盘：AES-256-GCM 加密，密钥由你的口令经 scrypt 派生。
口令通过 macOS 原生对话框输入（osascript 隐藏输入），只在本 MCP 进程内存里换成密钥，
不写进任何文件、环境变量或日志，也不会出现在 MCP 工具的返回值里——
所以调用这些工具的 AI 助手只能"使用"登录态，读不到凭证本身。

每个 MCP 进程第一次需要凭证时弹一次对话框；之后同一进程内复用内存里的密钥。

文件格式（JSON，权限 600）：
  {"v": 1, "kdf": "scrypt", "n": ..., "r": 8, "p": 1, "salt": b64, "nonce": b64, "ct": b64}
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1
AAD = b"bilibili-mcp credential v1"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1   # 约 128 MB 内存、单次约 0.3 秒，增加暴力破解成本
MAX_TRIES = 3


class VaultError(Exception):
    """解密失败、口令错误次数过多或用户取消。消息里不含任何凭证内容。"""


class VaultCorrupt(VaultError):
    """文件不是本工具写出的密文（损坏、空文件或旧版明文）。可以被新的登录覆盖。"""


KEYCHAIN_SERVICE = os.environ.get("BILI_MCP_KEYCHAIN_SERVICE", "bilibili-mcp-vault")


def keychain_available() -> bool:
    """只有 macOS 才有 security 命令。"""
    return sys.platform == "darwin"


def keychain_get() -> str | None:
    """读登录钥匙串里的口令（定时任务无人值守上传用）；没有或不在 macOS 上返回 None。

    口令只落在钥匙串里：不写文件、不进环境变量、不进日志，AI 助手依然读不到凭证内容。
    """
    if not keychain_available():
        return None
    proc = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n") or None


def keychain_set(passphrase: str) -> bool:
    """把口令写进登录钥匙串（能建就建，能改就改）。由用户显式调用，不会自动保存。"""
    if not passphrase or not keychain_available():
        return False
    proc = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                           "-a", os.environ.get("USER", "bilibili-mcp"), "-w", passphrase],
                          capture_output=True, text=True)
    return proc.returncode == 0


def keychain_clear() -> bool:
    if not keychain_available():
        return False
    proc = subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
                          capture_output=True, text=True)
    return proc.returncode == 0


def ask_passphrase(prompt: str) -> str:
    """取口令：优先用登录钥匙串里存的那份（这样定时任务可以无人值守上传），
    没有才弹 macOS 原生对话框。取消时抛 VaultError。"""
    saved = keychain_get()
    if saved:
        return saved
    script = (
        f'display dialog "{prompt}" default answer "" with hidden answer '
        'with title "bilibili-mcp 凭证保险箱" with icon caution '
        'buttons {"取消", "确定"} default button "确定" cancel button "取消"\n'
        'text returned of result'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise VaultError("已取消输入口令")
    return proc.stdout.rstrip("\n")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)


def _write_private(path: Path, text: str) -> None:
    """原子写入，文件权限 600（只有你的用户能读写）。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# 本进程解密过的秘密值（cookie / token）。MCP 工具的返回值和报错在交给 AI 之前会把它们替换掉。
_SECRET_FIELDS = ("sessdata", "bili_jct", "buvid3", "ac_time_value")
_secrets: set[str] = set()


def _remember(data: dict) -> None:
    for k in _SECRET_FIELDS:
        v = data.get(k)
        if isinstance(v, str) and len(v) >= 6:
            _secrets.add(v)


def redact(text: str) -> str:
    """把任何出现在文本里的凭证值（含 URL 编码形式）替换成 [已隐藏]。"""
    from urllib.parse import quote
    for v in sorted(_secrets, key=len, reverse=True):
        for form in {v, quote(v, safe=""), quote(v)}:
            text = text.replace(form, "[已隐藏]")
    return text


class Vault:
    """一个加密文件。密钥只缓存在本对象（即本进程）的内存里。"""

    def __init__(self, path: Path, ask: Callable[[str], str] = ask_passphrase):
        self.path = Path(path)
        self._ask = ask
        self._key: bytes | None = None
        self._salt: bytes | None = None
        self._params = (SCRYPT_N, SCRYPT_R, SCRYPT_P)

    def exists(self) -> bool:
        return self.path.exists()

    def _unlock(self, blob: dict) -> bytes:
        salt = base64.b64decode(blob["salt"])
        n, r, p = blob["n"], blob["r"], blob["p"]
        nonce, ct = base64.b64decode(blob["nonce"]), base64.b64decode(blob["ct"])
        if self._key is not None and self._salt == salt:
            try:
                return AESGCM(self._key).decrypt(nonce, ct, AAD)
            except InvalidTag:
                self._key = None
        for i in range(MAX_TRIES):
            hint = "" if i == 0 else f"（口令不对，还可以再试 {MAX_TRIES - i} 次）"
            key = _derive(self._ask(f"请输入 B 站凭证口令{hint}"), salt, n, r, p)
            try:
                plain = AESGCM(key).decrypt(nonce, ct, AAD)
            except InvalidTag:
                continue
            self._key, self._salt, self._params = key, salt, (n, r, p)
            return plain
        raise VaultError("口令错误次数过多，凭证保持加密")

    def load(self) -> dict | None:
        """返回解密后的 dict；文件不存在返回 None。"""
        if not self.path.exists():
            return None
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
            if blob.get("v") != VERSION or not all(k in blob for k in ("salt", "nonce", "ct", "n", "r", "p")):
                raise VaultCorrupt("不认识的凭证文件")
        except (OSError, ValueError, AttributeError) as e:
            raise VaultCorrupt(f"凭证文件损坏（{type(e).__name__}）") from None
        data = json.loads(self._unlock(blob).decode("utf-8"))
        if not isinstance(data, dict):
            raise VaultError("凭证内容格式不对")
        _remember(data)
        return data

    def save(self, data: dict) -> None:
        """加密保存。第一次保存（还没有密钥）时让你设置口令并确认一遍。"""
        if self._key is None:
            existing = self.path.exists()
            if existing:
                try:
                    self.load()  # 已有文件：先用原口令解锁，沿用同一把密钥
                except VaultCorrupt:
                    existing = False  # 损坏的文件直接用新口令覆盖
            if not existing:
                for _ in range(MAX_TRIES):
                    first = self._ask("第一次登录：请为 B 站凭证设置一个口令（至少 8 位）")
                    if len(first) < 8:
                        continue
                    if self._ask("请再输入一遍口令确认") == first:
                        self._salt = os.urandom(16)
                        self._params = (SCRYPT_N, SCRYPT_R, SCRYPT_P)
                        self._key = _derive(first, self._salt, *self._params)
                        break
                else:
                    raise VaultError("口令设置失败（太短或两次不一致），凭证未保存")
        _remember(data)
        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, json.dumps(data, ensure_ascii=False).encode("utf-8"), AAD)
        _write_private(self.path, json.dumps({
            "v": VERSION, "kdf": "scrypt", "n": self._params[0], "r": self._params[1], "p": self._params[2],
            "salt": _b64(self._salt), "nonce": _b64(nonce), "ct": _b64(ct),
        }))

    def forget(self) -> None:
        """清掉内存里的密钥（之后再用需要重新输入口令）。"""
        self._key = None
