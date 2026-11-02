"""Credential loading, validation, and persistence helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from bilibili_api import Credential, user

from secure_store import Vault, VaultCorrupt, VaultError


class CredentialStatus(str, Enum):
    NO_CREDENTIAL = "no_credential"
    LOCKED = "credential_locked"
    CORRUPTED = "credential_corrupted"
    INCOMPLETE = "credential_incomplete"
    INVALID = "credential_invalid"
    VALID = "valid"


@dataclass
class CredentialResult:
    status: CredentialStatus
    credential: Credential | None = None
    user_info: dict[str, Any] | None = None


_FIELDS = ("sessdata", "bili_jct", "buvid3", "dedeuserid", "ac_time_value")


def credential_is_complete(credential: Credential | None) -> bool:
    return bool(
        credential
        and getattr(credential, "sessdata", None)
        and getattr(credential, "bili_jct", None)
    )


# 凭证只以密文落盘（见 secure_store.py）；每个文件路径对应一个进程内的 Vault，密钥只在内存里。
_vaults: dict[Path, Vault] = {}


def vault_for(path: Path) -> Vault:
    path = Path(path).resolve()
    if path not in _vaults:
        _vaults[path] = Vault(path)
    return _vaults[path]


def read_credential(path: Path) -> CredentialResult:
    if not Path(path).exists():
        return CredentialResult(CredentialStatus.NO_CREDENTIAL)
    try:
        data = vault_for(path).load()
        if not isinstance(data, dict):
            return CredentialResult(CredentialStatus.CORRUPTED)
        credential = Credential(**{name: data.get(name) or "" for name in _FIELDS})
    except VaultCorrupt:
        return CredentialResult(CredentialStatus.CORRUPTED)
    except VaultError:
        return CredentialResult(CredentialStatus.LOCKED)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return CredentialResult(CredentialStatus.CORRUPTED)
    if not credential_is_complete(credential):
        return CredentialResult(CredentialStatus.INCOMPLETE, credential)
    return CredentialResult(CredentialStatus.VALID, credential)


def save_credential(path: Path, credential: Credential) -> None:
    if not credential_is_complete(credential):
        raise ValueError("credential is incomplete")
    data = {name: getattr(credential, name, None) or "" for name in _FIELDS}
    vault_for(path).save(data)


async def validate_credential(
    credential: Credential | None,
    path: Path | None = None,
) -> CredentialResult:
    if not credential_is_complete(credential):
        return CredentialResult(CredentialStatus.INCOMPLETE, credential)
    try:
        if not await credential.check_valid():
            return CredentialResult(CredentialStatus.INVALID, credential)
        user_info = await user.get_self_info(credential=credential)
    except Exception:
        return CredentialResult(CredentialStatus.INVALID, credential)

    uid = str(user_info.get("mid") or user_info.get("uid") or "")
    if not uid.isdigit() or int(uid) <= 0:
        return CredentialResult(CredentialStatus.INVALID, credential)
    if str(getattr(credential, "dedeuserid", "") or "") != uid:
        credential.dedeuserid = uid
        if path is not None:
            save_credential(path, credential)
    return CredentialResult(CredentialStatus.VALID, credential, user_info)


async def load_credential(path: Path) -> CredentialResult:
    result = read_credential(path)
    if result.status != CredentialStatus.VALID:
        return result
    return await validate_credential(result.credential, path)


async def validate_save_and_reload(
    path: Path,
    credential: Credential | None,
) -> CredentialResult:
    result = await validate_credential(credential)
    if result.status != CredentialStatus.VALID or result.credential is None:
        return result
    save_credential(path, result.credential)
    return await load_credential(path)
