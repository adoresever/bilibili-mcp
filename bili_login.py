#!/usr/bin/env python3
"""B站扫码登录，保存凭证供 MCP Server 使用"""

import asyncio
from pathlib import Path
from bilibili_api.login_v2 import (
    QrCodeLogin,
    QrCodeLoginChannel,
    QrCodeLoginEvents,
)

from bili_auth import CredentialStatus, validate_save_and_reload

CRED_FILE = Path(__file__).parent / "bili_credential.json"

async def main():
    qr = QrCodeLogin(platform=QrCodeLoginChannel.TV)
    await qr.generate_qrcode()
    print(qr.get_qrcode_terminal())
    print("\n请用B站App扫描上方二维码（180秒内有效）\n")

    while True:
        state = await qr.check_state()
        if state == QrCodeLoginEvents.SCAN:
            print("等待扫码...")
        elif state == QrCodeLoginEvents.CONF:
            print("已扫码，请在手机上确认...")
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("❌ 二维码超时，请重新运行")
            return
        elif state == QrCodeLoginEvents.DONE:
            break
        await asyncio.sleep(2)

    try:
        credential = qr.get_credential()
    except Exception as error:
        print(f"凭证提取失败（{type(error).__name__}），未保存凭证")
        return

    result = await validate_save_and_reload(CRED_FILE, credential)
    if result.status != CredentialStatus.VALID:
        print(f"登录态验证失败（{result.status.value}），未保存有效凭证")
        return
    print(f"\n登录成功！凭证已验证并保存到 {CRED_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
