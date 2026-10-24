#!/usr/bin/env python3
"""B站扫码登录（命令行），保存持久凭证供 MCP Server 使用。

与旧版的区别：
- 直接走 passport 接口，兼容新版从 Set-Cookie 下发 cookie 的行为；
- 凭证中额外保存 refresh_token（ac_time_value），支持长期持久登录。
"""

import asyncio
import sys

from bili_login_flow import CRED_FILE, QrCodeLogin, QrCodeLoginEvents, save_credential


async def main() -> int:
    qr = QrCodeLogin()
    try:
        await qr.generate_qrcode()
        print(qr.get_qrcode_terminal())
        print("\n请用B站App扫描上方二维码（180秒内有效）\n")

        while True:
            state = await qr.check_state()
            if state == QrCodeLoginEvents.SCAN:
                print("⏳ 等待扫码...", end="\r", flush=True)
            elif state == QrCodeLoginEvents.CONF:
                print("✅ 已扫码，请在手机上确认...          ")
            elif state == QrCodeLoginEvents.TIMEOUT:
                print("❌ 二维码超时，请重新运行              ")
                return 1
            elif state == QrCodeLoginEvents.DONE:
                break
            await asyncio.sleep(2)

        save_credential(qr.get_credential())
        print(f"\n🎉 登录成功！凭证已保存到 {CRED_FILE}（含 refresh token，可持久登录）")
        return 0
    finally:
        await qr.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
