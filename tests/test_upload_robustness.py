"""上传鲁棒性：超时放大、分块并发、停滞看门狗。"""
import asyncio
import unittest

import mcp_server


class EventLabelTests(unittest.TestCase):
    def test_keeps_name_and_useful_fields(self):
        page = type("Page", (), {"path": "/tmp/x.mp4"})()
        label = mcp_server._event_label({"name": "CHUNK_FAILED",
                                         "data": ({"page": page, "offset": 10485760, "chunk_number": 1},)})
        self.assertIn("CHUNK_FAILED", label)
        self.assertIn("10485760", label)
        self.assertNotIn("/tmp/x.mp4", label)

    def test_tolerates_non_dict(self):
        self.assertEqual(mcp_server._event_label("WHAT"), "WHAT")


class TuningTests(unittest.TestCase):
    def test_timeout_restored_after_exit(self):
        from bilibili_api import request_settings
        from bilibili_api.video_uploader import VideoUploader
        before_timeout = request_settings.get_timeout()
        before_preupload = VideoUploader._preupload
        with mcp_server._upload_tuning():
            self.assertEqual(request_settings.get_timeout(), mcp_server.UPLOAD_REQUEST_TIMEOUT)
            self.assertIsNot(VideoUploader._preupload, before_preupload)
        self.assertEqual(request_settings.get_timeout(), before_timeout)
        self.assertIs(VideoUploader._preupload, before_preupload)

    def test_preupload_raises_chunk_threads_only_upwards(self):
        from bilibili_api.video_uploader import VideoUploader

        async def fake_preupload(self, page):
            return {"threads": 4}

        original = VideoUploader._preupload
        VideoUploader._preupload = fake_preupload
        try:
            with mcp_server._upload_tuning():
                got = asyncio.run(VideoUploader._preupload(None, None))
        finally:
            VideoUploader._preupload = original
        self.assertGreaterEqual(got["threads"], mcp_server.UPLOAD_CHUNK_THREADS)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_result_when_upload_finishes(self):
        class Fast:
            async def start(self):
                return {"bvid": "BV1xx"}

        out = await mcp_server._start_with_watchdog(Fast(), {"phase": "init"})
        self.assertEqual(out["bvid"], "BV1xx")

    async def test_cancels_and_reports_phase_when_stalled(self):
        class Stalled:
            async def start(self):
                await asyncio.sleep(3600)

        old = mcp_server.UPLOAD_STALL_SEC
        mcp_server.UPLOAD_STALL_SEC = 0.2
        try:
            with self.assertRaises(TimeoutError) as ctx:
                await mcp_server._start_with_watchdog(Stalled(), {"phase": "CHUNK 2/4"})
        finally:
            mcp_server.UPLOAD_STALL_SEC = old
        self.assertIn("CHUNK 2/4", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
