"""凭证口令 → 登录钥匙串的命令行入口（macOS）。

口令只存在钥匙串（generic password, service=bilibili-mcp-vault），
不写文件、不进环境变量、不进日志；凭证本体仍是 AES-256-GCM 密文。
存过之后，MCP 进程（含定时任务）解锁凭证时不再弹对话框。

  python vault_cli.py store    # 弹一次对话框，验证能解密后存进钥匙串
  python vault_cli.py check    # 看钥匙串有没有、能不能解密
  python vault_cli.py clear    # 从钥匙串删除（回到手动输口令）
"""
import subprocess
import sys
from pathlib import Path

from secure_store import (
    KEYCHAIN_SERVICE, Vault, VaultCorrupt, VaultError,
    keychain_clear, keychain_get, keychain_set,
)

CRED_FILE = Path(__file__).with_name("bili_credential.enc")


def _ask_dialog(prompt: str) -> str:
    script = (
        f'display dialog "{prompt}" default answer "" with hidden answer '
        'with title "bilibili-mcp 凭证保险箱" with icon caution '
        'buttons {"取消", "确定"} default button "确定" cancel button "取消"\n'
        'text returned of result'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit("已取消")
    return proc.stdout.rstrip("\n")


def _unlocks(passphrase: str) -> bool:
    try:
        return bool(Vault(CRED_FILE, ask=lambda _p: passphrase).load())
    except (VaultError, VaultCorrupt):
        return False


def cmd_store() -> int:
    if not CRED_FILE.exists():
        raise SystemExit(f"找不到 {CRED_FILE}，先在 MCP 里扫码登录一次")
    passphrase = _ask_dialog("输入凭证口令（只存进登录钥匙串，供定时任务自动解锁）")
    if not _unlocks(passphrase):
        raise SystemExit("口令不对，凭证解不开")
    if not keychain_set(passphrase):
        raise SystemExit("写入钥匙串失败")
    print("✓ 口令已存进登录钥匙串，之后解锁凭证不再弹窗")
    return 0


def cmd_check() -> int:
    if not keychain_get():
        print(f"钥匙串（{KEYCHAIN_SERVICE}）：没有口令 → 想无人值守就先 store 一次")
        return 1
    ok = _unlocks(keychain_get())
    print(f"钥匙串（{KEYCHAIN_SERVICE}）：有口令，能解密凭证：{'是' if ok else '否（口令可能换过，重新 store）'}")
    return 0 if ok else 1


def cmd_clear() -> int:
    print("已从钥匙串删除口令" if keychain_clear() else "钥匙串里本来就没有口令")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    sys.exit({"store": cmd_store, "check": cmd_check, "clear": cmd_clear}.get(action, cmd_check)())
