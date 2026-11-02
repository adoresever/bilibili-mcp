import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import secure_store
from secure_store import Vault, VaultCorrupt, VaultError

SECRET = {"sessdata": "very-secret-cookie", "bili_jct": "csrf-token", "dedeuserid": "123"}


def asker(*answers):
    """按顺序返回口令，并记录被问了几次。"""
    it = iter(answers)
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return next(it)
    ask.calls = calls
    return ask


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bili_credential.enc"
        # 测试用小参数，跑得快
        self._n = secure_store.SCRYPT_N
        secure_store.SCRYPT_N = 2 ** 12

    def tearDown(self):
        secure_store.SCRYPT_N = self._n
        self.tmp.cleanup()

    def test_round_trip_and_file_is_ciphertext_with_0600(self):
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        raw = self.path.read_text()
        for v in SECRET.values():
            self.assertNotIn(v, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(Vault(self.path, ask=asker("correct horse")).load(), SECRET)

    def test_wrong_passphrase_never_decrypts(self):
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        ask = asker("nope1234", "nope5678", "nope9999")
        with self.assertRaises(VaultError):
            Vault(self.path, ask=ask).load()
        self.assertEqual(len(ask.calls), secure_store.MAX_TRIES)

    def test_retry_then_success(self):
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        self.assertEqual(Vault(self.path, ask=asker("typo typo", "correct horse")).load(), SECRET)

    def test_key_cached_in_process_asks_once(self):
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        ask = asker("correct horse")
        v = Vault(self.path, ask=ask)
        v.load(); v.load(); v.save({**SECRET, "dedeuserid": "456"}); v.load()
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(Vault(self.path, ask=asker("correct horse")).load()["dedeuserid"], "456")

    def test_new_passphrase_must_match_and_be_long_enough(self):
        with self.assertRaises(VaultError):
            Vault(self.path, ask=asker("short", "longenough1", "different1", "longenough2", "different2")).save(SECRET)
        self.assertFalse(self.path.exists())

    def test_tampered_ciphertext_is_rejected(self):
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        blob = json.loads(self.path.read_text())
        ct = bytearray(secure_store.base64.b64decode(blob["ct"])); ct[0] ^= 1
        blob["ct"] = secure_store._b64(bytes(ct))
        self.path.write_text(json.dumps(blob))
        with self.assertRaises(VaultError):
            Vault(self.path, ask=asker("correct horse", "correct horse", "correct horse")).load()

    def test_plaintext_or_garbage_file_is_corrupt_and_overwritable(self):
        self.path.write_text(json.dumps(SECRET))
        with self.assertRaises(VaultCorrupt):
            Vault(self.path, ask=asker()).load()
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        self.assertEqual(Vault(self.path, ask=asker("correct horse")).load(), SECRET)

    def test_cancelled_dialog_raises(self):
        def cancel(_prompt):
            raise VaultError("已取消输入口令")
        Vault(self.path, ask=asker("correct horse", "correct horse")).save(SECRET)
        with self.assertRaises(VaultError):
            Vault(self.path, ask=cancel).load()


class KeychainTests(unittest.TestCase):
    """口令可以放 macOS 登录钥匙串，定时任务才能无人值守解锁凭证。"""

    def setUp(self):
        self._platform = secure_store.sys.platform

    def tearDown(self):
        secure_store.sys.platform = self._platform

    def _run(self, returncode=0, stdout="", seen=None):
        def fake(cmd, **kwargs):
            if seen is not None:
                seen.append(cmd)
            return type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()
        return fake

    def test_get_reads_login_keychain_and_strips_newline(self):
        seen = []
        with patch.object(secure_store.subprocess, "run", self._run(0, "hunter2\n", seen)):
            self.assertEqual(secure_store.keychain_get(), "hunter2")
        self.assertIn("find-generic-password", seen[0])
        self.assertIn(secure_store.KEYCHAIN_SERVICE, seen[0])

    def test_get_returns_none_when_absent(self):
        with patch.object(secure_store.subprocess, "run", self._run(44, "")):
            self.assertIsNone(secure_store.keychain_get())

    def test_set_uses_update_flag_so_reruns_overwrite(self):
        seen = []
        with patch.object(secure_store.subprocess, "run", self._run(0, "", seen)):
            self.assertTrue(secure_store.keychain_set("hunter2"))
        self.assertIn("add-generic-password", seen[0])
        self.assertIn("-U", seen[0])

    def test_set_rejects_empty(self):
        with patch.object(secure_store.subprocess, "run", self._run(0, "")):
            self.assertFalse(secure_store.keychain_set(""))

    def test_clear(self):
        with patch.object(secure_store.subprocess, "run", self._run(0, "")):
            self.assertTrue(secure_store.keychain_clear())

    def test_non_darwin_never_shells_out(self):
        secure_store.sys.platform = "linux"
        seen = []
        with patch.object(secure_store.subprocess, "run", self._run(0, "x", seen)):
            self.assertIsNone(secure_store.keychain_get())
            self.assertFalse(secure_store.keychain_set("x"))
            self.assertFalse(secure_store.keychain_clear())
        self.assertEqual(seen, [])

    def test_ask_passphrase_prefers_keychain_over_dialog(self):
        def boom(*_a, **_k):
            raise AssertionError("钥匙串有口令时不该再弹对话框")
        with patch.object(secure_store, "keychain_get", lambda: "hunter2"), \
                patch.object(secure_store.subprocess, "run", boom):
            self.assertEqual(secure_store.ask_passphrase("请输入口令"), "hunter2")

    def test_ask_passphrase_falls_back_to_dialog(self):
        with patch.object(secure_store, "keychain_get", lambda: None), \
                patch.object(secure_store.subprocess, "run", self._run(0, "typed\n")):
            self.assertEqual(secure_store.ask_passphrase("请输入口令"), "typed")


if __name__ == "__main__":
    unittest.main()


class RedactTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_output_and_errors_are_scrubbed(self):
        import mcp_server
        secure_store._remember({"sessdata": "SESS-abc%2C123", "bili_jct": "jct-value-xyz"})
        out = secure_store.redact('{"cookie": "SESS-abc%2C123", "csrf": "jct-value-xyz"}')
        self.assertNotIn("SESS-abc", out)
        self.assertNotIn("jct-value-xyz", out)

        @mcp_server.mcp.tool()
        async def _leaky_test_tool(x: int) -> str:
            raise RuntimeError("request failed: csrf=jct-value-xyz")

        tool = mcp_server.mcp._tool_manager.get_tool("_leaky_test_tool")
        self.assertIn("x", tool.parameters["properties"])
        with self.assertRaises(Exception) as ctx:
            await tool.fn(x=1)
        self.assertNotIn("jct-value-xyz", str(ctx.exception))

    def test_upload_tool_keeps_its_parameters(self):
        import mcp_server
        props = mcp_server.mcp._tool_manager.get_tool("bili_upload_video").parameters["properties"]
        for name in ("video_path", "title", "desc", "tid", "tags", "cover_path"):
            self.assertIn(name, props)
