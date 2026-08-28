import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bilibili_api import Credential
from bilibili_api.login_v2 import QrCodeLoginChannel, QrCodeLoginEvents

import bili_auth
import mcp_server
from bili_auth import CredentialStatus


class FakePicture:
    content = b"fake-png"


class FakeQr:
    def __init__(self, state=QrCodeLoginEvents.SCAN, credential=None):
        self.state = state
        self.credential = credential

    async def generate_qrcode(self):
        pass

    async def check_state(self):
        return self.state

    def has_done(self):
        return self.state == QrCodeLoginEvents.DONE and self.credential is not None

    def get_credential(self):
        if isinstance(self.credential, Exception):
            raise self.credential
        return self.credential

    def get_qrcode_picture(self):
        return FakePicture()

    def get_qrcode_terminal(self):
        return "terminal-qr"


def make_credential(complete=True, uid="123"):
    return Credential(
        sessdata="test-sessdata" if complete else "",
        bili_jct="test-csrf" if complete else "",
        buvid3="test-buvid" if complete else "",
        dedeuserid=uid,
        ac_time_value="test-refresh" if complete else "",
    )


async def valid(_credential):
    return True


async def self_info(*, credential):
    return {"mid": 24680, "name": "tester"}


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.credential_path = root / "bili_credential.json"
        self.qr_path = root / "qrcode_login.png"
        self.cred_patch = patch.object(mcp_server, "CRED_FILE", self.credential_path)
        self.qr_patch = patch.object(mcp_server, "QR_FILE", self.qr_path)
        self.cred_patch.start()
        self.qr_patch.start()
        mcp_server._login_session = None
        mcp_server._login_session_created_at = None

    def tearDown(self):
        self.cred_patch.stop()
        self.qr_patch.stop()
        mcp_server._login_session = None
        mcp_server._login_session_created_at = None
        self.temp_dir.cleanup()

    async def test_done_with_empty_credential_is_not_saved(self):
        mcp_server._login_session = FakeQr(
            QrCodeLoginEvents.DONE, make_credential(False, "")
        )
        mcp_server._login_session_created_at = time.monotonic()

        result = json.loads(await mcp_server.bili_login_check())

        self.assertEqual(result["status"], "credential_incomplete")
        self.assertFalse(self.credential_path.exists())

    async def test_empty_file_does_not_count_as_logged_in(self):
        self.credential_path.write_text("{}", encoding="utf-8")
        fake_qr = FakeQr()
        platforms = []

        def factory(*, platform):
            platforms.append(platform)
            return fake_qr

        with patch.object(mcp_server, "QrCodeLogin", factory):
            result = json.loads(await mcp_server.bili_login())

        self.assertEqual(result["status"], "qrcode_ready")
        self.assertEqual(platforms, [QrCodeLoginChannel.TV])

    async def test_corrupted_json_does_not_crash(self):
        self.credential_path.write_text("{broken", encoding="utf-8")

        result = json.loads(await mcp_server.bili_login_check())

        self.assertEqual(result["status"], "no_session")
        self.assertEqual(result["credential_status"], "credential_corrupted")

    async def test_missing_uid_is_fetched_without_int_empty(self):
        bili_auth.save_credential(self.credential_path, make_credential(uid=""))
        with patch.object(Credential, "check_valid", valid), patch.object(
            bili_auth.user, "get_self_info", self_info
        ):
            result = json.loads(await mcp_server.bili_check_credential())

        self.assertTrue(result["logged_in"])
        self.assertEqual(result["uid"], "24680")

    async def test_valid_credential_loads(self):
        bili_auth.save_credential(self.credential_path, make_credential())
        with patch.object(Credential, "check_valid", valid), patch.object(
            bili_auth.user, "get_self_info", self_info
        ):
            result = await bili_auth.load_credential(self.credential_path)

        self.assertEqual(result.status, CredentialStatus.VALID)

    async def test_credential_extraction_failure_is_explicit(self):
        session = FakeQr(QrCodeLoginEvents.DONE, RuntimeError("test failure"))
        mcp_server._login_session = session
        mcp_server._login_session_created_at = time.monotonic()

        result = json.loads(await mcp_server.bili_login_check())

        self.assertEqual(result["status"], "credential_unavailable")
        self.assertIs(mcp_server._login_session, session)
        self.assertFalse(self.credential_path.exists())

    async def test_timeout_cleans_session_and_qrcode(self):
        self.qr_path.write_bytes(b"fake-png")
        mcp_server._login_session = FakeQr(QrCodeLoginEvents.TIMEOUT)
        mcp_server._login_session_created_at = time.monotonic()

        result = json.loads(await mcp_server.bili_login_check())

        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(mcp_server._login_session)
        self.assertFalse(self.qr_path.exists())

    async def test_done_with_valid_credential_saves_and_succeeds(self):
        mcp_server._login_session = FakeQr(
            QrCodeLoginEvents.DONE, make_credential(uid="")
        )
        mcp_server._login_session_created_at = time.monotonic()
        with patch.object(Credential, "check_valid", valid), patch.object(
            bili_auth.user, "get_self_info", self_info
        ):
            result = json.loads(await mcp_server.bili_login_check())

        saved = json.loads(self.credential_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "done")
        self.assertEqual(saved["dedeuserid"], "24680")
        self.assertTrue(saved["sessdata"])

    async def test_repeated_login_keeps_active_session(self):
        session = FakeQr(QrCodeLoginEvents.SCAN)
        mcp_server._login_session = session
        mcp_server._login_session_created_at = time.monotonic()

        result = json.loads(await mcp_server.bili_login())

        self.assertEqual(result["status"], "login_in_progress")
        self.assertIs(mcp_server._login_session, session)

