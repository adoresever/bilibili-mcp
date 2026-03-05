#!/usr/bin/env python3
"""B站扫码登录，保存凭证供 MCP Server 使用"""

import asyncio
import json
from pathlib import Path
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

CRED_FILE = Path(__file__).parent / "bili_credential.json"

async def main():
    qr = QrCodeLogin()
    await qr.generate_qrcode()
    print(qr.get_qrcode_terminal())
    print("\n请用B站App扫描上方二维码（180秒内有效）\n")

    while True:
        state = await qr.check_state()
        if state == QrCodeLoginEvents.SCAN:
            print("✅ 已扫码，请在手机上确认...")
        elif state == QrCodeLoginEvents.CONF:
            print("✅ 已确认...")
        elif state == QrCodeLoginEvents.TIMEOUT:
            print("❌ 二维码超时，请重新运行")
            return
        elif state == QrCodeLoginEvents.DONE:
            break
        await asyncio.sleep(2)

    cred = qr.get_credential()
    with open(CRED_FILE, "w") as f:
        json.dump({
            "sessdata": cred.sessdata,
            "bili_jct": cred.bili_jct,
            "buvid3": cred.buvid3,
            "dedeuserid": cred.dedeuserid,
        }, f)
    print(f"\n🎉 登录成功！凭证已保存到 {CRED_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
