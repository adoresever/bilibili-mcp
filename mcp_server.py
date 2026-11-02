#!/usr/bin/env python3
"""
Bilibili MCP Server
B站数据采集 MCP 服务，支持 OpenClaw / Claude Code / Cursor / Cline

功能：搜索视频、抓取评论、获取字幕、弹幕、回复评论
首次运行需扫码登录B站，凭证自动保存复用
"""

import asyncio
import contextlib
import functools
import json
import base64
import os
import sys
import tempfile
import time
from pathlib import Path
from mcp.server.fastmcp import FastMCP

from bilibili_api import video, search, comment, Credential, dynamic, opus
from bilibili_api import hot, rank, user, session, favorite_list
from bilibili_api.video_uploader import (
    VideoUploader, VideoUploaderPage, VideoMeta, Lines, upload_cover,
)
from datetime import datetime
from bilibili_api.utils.picture import Picture
from bilibili_api.login_v2 import (
    QrCodeLogin,
    QrCodeLoginChannel,
    QrCodeLoginEvents,
)

from secure_store import redact
from bili_auth import (
    CredentialStatus,
    credential_is_complete,
    load_credential,
    validate_save_and_reload,
)

# ========== 初始化 ==========

CRED_FILE = Path(__file__).parent / "bili_credential.enc"

mcp = FastMCP("bilibili-mcp")


# 所有工具的返回值和报错都先经过 redact()：即使 B 站接口在报错里带回 cookie / token，也不会传给 AI。
_register_tool = mcp.tool


def _safe_tool(*args, **kwargs):
    register = _register_tool(*args, **kwargs)

    def wrap(fn):
        @functools.wraps(fn)
        async def guarded(*a, **kw):
            try:
                out = await fn(*a, **kw)
            except Exception as e:
                raise Exception(redact(str(e))) from None
            return redact(out) if isinstance(out, str) else out
        return register(guarded)
    return wrap


mcp.tool = _safe_tool


# ========== 登录会话（进程级缓存） ==========

_login_session: QrCodeLogin | None = None
_login_session_created_at: float | None = None
_LOGIN_SESSION_MAX_AGE = 180
QR_FILE = Path(__file__).parent / "qrcode_login.png"


def _clear_login_session() -> None:
    global _login_session, _login_session_created_at
    _login_session = None
    _login_session_created_at = None
    try:
        QR_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _session_is_expired() -> bool:
    return bool(
        _login_session_created_at is not None
        and time.monotonic() - _login_session_created_at >= _LOGIN_SESSION_MAX_AGE
    )


async def get_cred() -> Credential:
    """获取凭证，未登录时引导用户调用 bili_login"""
    result = await load_credential(CRED_FILE)
    if result.status == CredentialStatus.LOCKED:
        raise Exception("B站凭证是加密的，需要用户在弹出的对话框里输入口令；用户取消了或口令错误。请让用户重试，不要重新扫码登录。")
    if result.status != CredentialStatus.VALID or result.credential is None:
        raise Exception("未登录B站！请先调用 bili_login 工具获取登录二维码，让用户扫码登录。")
    return result.credential


# ========== 上传鲁棒性：慢链路 / 海外线路 ==========
# 海外网络到 upos CDN 的单连接上行常常只有几十 KB/s，一块 10 MB 数据在规定时间内写不完，
# bilibili_api 就对每块判失败并无限重传，调用方看到的是"一直卡着"。这里三件事：
#   1) 把上传期间的 HTTP 超时放大（默认 30s → 1800s，可用 BILI_MCP_UPLOAD_TIMEOUT 覆盖）
#   2) 提高分块并发（服务端默认 4 → 12，可用 BILI_MCP_UPLOAD_THREADS 覆盖）
#   3) 加停滞看门狗：长时间没有任何进度事件就取消并把最后阶段回报给调用方
UPLOAD_REQUEST_TIMEOUT = float(os.environ.get("BILI_MCP_UPLOAD_TIMEOUT", "1800"))
UPLOAD_CHUNK_THREADS = int(os.environ.get("BILI_MCP_UPLOAD_THREADS", "12"))
UPLOAD_STALL_SEC = float(os.environ.get("BILI_MCP_UPLOAD_STALL", "900"))


def _event_label(event) -> str:
    """把上传事件压成一行人话：只留事件名和 offset/size/info 这类有用字段。"""
    if not isinstance(event, dict):
        return str(event)[:160]
    name, data = event.get("name"), event.get("data")
    info = ""
    if isinstance(data, tuple) and data and isinstance(data[0], dict):
        detail = {k: v for k, v in data[0].items() if k != "page" and not hasattr(v, "path")}
        info = json.dumps(detail, ensure_ascii=False, default=str)[:200]
    return f"{name} {info}".strip()


@contextlib.contextmanager
def _upload_tuning():
    """上传期间放大超时、提高分块并发；退出时恢复原状（同一进程里的其它请求不受影响）。"""
    from bilibili_api import request_settings
    from bilibili_api.video_uploader import VideoUploader

    old_timeout = request_settings.get_timeout()
    orig_preupload = VideoUploader._preupload

    async def preupload(self, page):
        pre = await orig_preupload(self, page)
        try:
            pre["threads"] = max(int(pre.get("threads") or 1), UPLOAD_CHUNK_THREADS)
        except (TypeError, ValueError):
            pass
        return pre

    request_settings.set_timeout(UPLOAD_REQUEST_TIMEOUT)
    VideoUploader._preupload = preupload
    try:
        yield
    finally:
        request_settings.set_timeout(old_timeout)
        VideoUploader._preupload = orig_preupload


async def _start_with_watchdog(uploader, progress: dict) -> dict:
    """跑完上传；UPLOAD_STALL_SEC 秒内没有任何进度就取消，给出可读错误而不是挂死。"""
    task = asyncio.ensure_future(uploader.start())
    started = time.monotonic()
    while True:
        done, _ = await asyncio.wait({task}, timeout=15)
        if done:
            return task.result()
        idle = time.monotonic() - progress.get("last", started)
        stalled = UPLOAD_STALL_SEC and idle > UPLOAD_STALL_SEC
        if stalled:
            task.cancel()
            raise TimeoutError(
                f"上传停滞 {idle:.0f} 秒（最后阶段：{progress.get('phase', 'init')}）；"
                f"海外线路可调大 BILI_MCP_UPLOAD_STALL 或换网络重试"
            )


# ========== Tool: 扫码登录（生成二维码） ==========

@mcp.tool()
async def bili_login() -> str:
    """
    生成B站登录二维码，返回二维码图片（base64）供用户扫码。
    用户扫码后，调用 bili_login_check 检查登录状态。

    Returns:
        包含二维码base64图片数据的JSON，前端可直接渲染为图片展示给用户
    """
    global _login_session, _login_session_created_at

    credential_result = await load_credential(CRED_FILE)
    if credential_result.status == CredentialStatus.VALID:
        return json.dumps({
            "status": "already_logged_in",
            "message": "已登录B站，无需重复登录",
        }, ensure_ascii=False)
    if credential_result.status == CredentialStatus.LOCKED:
        return json.dumps({
            "status": "credential_locked",
            "message": "已有加密凭证，但口令输入被取消或错误。请让用户再试一次；"
                       "如果用户忘了口令，需要用户自己删除 bili_credential.enc 后再重新扫码登录。",
        }, ensure_ascii=False)

    if _login_session is not None and not _session_is_expired():
        if not _login_session.has_done():
            return json.dumps({
                "status": "login_in_progress",
                "message": "已有进行中的扫码登录，请继续使用原二维码",
                "next_step": "请调用 bili_login_check 检查登录状态",
            }, ensure_ascii=False)
    if _login_session is not None:
        _clear_login_session()

    # TV 登录结果直接提供 cookie_info，不依赖 Web 回调 URL 中的 Cookie。
    _login_session = QrCodeLogin(platform=QrCodeLoginChannel.TV)
    await _login_session.generate_qrcode()
    _login_session_created_at = time.monotonic()

    # 获取二维码图片的 base64（无损PNG）
    pic = _login_session.get_qrcode_picture()
    img_base64 = base64.b64encode(pic.content).decode("utf-8")

    # 保存二维码图片到项目根目录
    with open(QR_FILE, "wb") as f:
        f.write(pic.content)

    # 获取终端文本版二维码（纯文本客户端备用）
    terminal_qr = _login_session.get_qrcode_terminal()

    # 获取原始扫码URL（用户也可以手动在B站App打开）
    qr_url = getattr(_login_session, "_QrCodeLogin__qr_link", "")

    return json.dumps({
        "status": "qrcode_ready",
        "message": "请用B站App扫描二维码登录（180秒内有效）",
        "qrcode_image": f"data:image/png;base64,{img_base64}",
        "qrcode_file": str(QR_FILE),
        "qrcode_terminal": terminal_qr,
        "qrcode_url": qr_url,
        "next_step": "用户扫码后，请调用 bili_login_check 检查登录状态",
    }, ensure_ascii=False)


# ========== Tool: 检查登录状态 ==========

@mcp.tool()
async def bili_login_check() -> str:
    """
    检查B站扫码登录状态。在用户扫码后调用此工具。
    如果返回 "scanning" 或 "confirming"，请等待几秒后再次调用。
    如果返回 "done"，登录成功，可以使用其他工具了。
    如果返回 "timeout"，需要重新调用 bili_login 生成新二维码。

    Returns:
        当前登录状态
    """
    global _login_session

    if not _login_session:
        credential_result = await load_credential(CRED_FILE)
        if credential_result.status == CredentialStatus.VALID:
            return json.dumps({
                "status": "already_logged_in",
                "message": "已登录B站",
            }, ensure_ascii=False)
        return json.dumps({
            "status": "no_session",
            "credential_status": credential_result.status.value,
            "message": "没有进行中的登录；MCP 进程可能已重启，请重新调用 bili_login",
        }, ensure_ascii=False)

    if _session_is_expired():
        _clear_login_session()
        return json.dumps({
            "status": "timeout",
            "message": "二维码已过期，请重新调用 bili_login 生成新二维码",
        }, ensure_ascii=False)

    try:
        state = await _login_session.check_state()
    except Exception as error:
        return json.dumps({
            "status": "login_check_failed",
            "message": f"登录状态查询失败（{type(error).__name__}），请稍后重试",
        }, ensure_ascii=False)

    if state == QrCodeLoginEvents.SCAN:
        return json.dumps({
            "status": "scanning",
            "message": "等待用户扫描二维码...",
            "next_step": "请等待3秒后再次调用 bili_login_check",
        }, ensure_ascii=False)

    elif state == QrCodeLoginEvents.CONF:
        return json.dumps({
            "status": "confirming",
            "message": "二维码已扫描，等待用户在手机上确认...",
            "next_step": "请等待2秒后再次调用 bili_login_check",
        }, ensure_ascii=False)

    elif state == QrCodeLoginEvents.TIMEOUT:
        _clear_login_session()
        return json.dumps({
            "status": "timeout",
            "message": "二维码已过期，请重新调用 bili_login 生成新二维码",
        }, ensure_ascii=False)

    elif state == QrCodeLoginEvents.DONE:
        try:
            credential = _login_session.get_credential()
        except Exception as error:
            return json.dumps({
                "status": "credential_unavailable",
                "message": f"二维码已确认，但凭证提取失败（{type(error).__name__}）",
            }, ensure_ascii=False)

        if not credential_is_complete(credential):
            return json.dumps({
                "status": "credential_incomplete",
                "message": "二维码已确认，但未取得有效登录凭证",
            }, ensure_ascii=False)

        credential_result = await validate_save_and_reload(CRED_FILE, credential)
        if credential_result.status != CredentialStatus.VALID:
            return json.dumps({
                "status": credential_result.status.value,
                "message": "二维码已确认，但登录凭证验证未通过",
            }, ensure_ascii=False)

        _clear_login_session()
        return json.dumps({
            "status": "done",
            "message": "登录成功！凭证已验证并保存，现在可以使用所有B站功能了",
        }, ensure_ascii=False)

    return json.dumps({
        "status": "unknown",
        "message": f"未知状态: {state}",
    }, ensure_ascii=False)


# ========== Tool: 登录状态查询 ==========

@mcp.tool()
async def bili_check_credential() -> str:
    """
    检查当前B站登录凭证是否有效

    Returns:
        登录状态信息
    """
    credential_result = await load_credential(CRED_FILE)
    if credential_result.status != CredentialStatus.VALID:
        return json.dumps({
            "logged_in": False,
            "credential_status": credential_result.status.value,
            "message": "当前没有有效登录凭证，请调用 bili_login 重新登录",
        }, ensure_ascii=False)

    credential = credential_result.credential
    my_info = credential_result.user_info or {}
    return json.dumps({
        "logged_in": True,
        "credential_status": CredentialStatus.VALID.value,
        "uid": credential.dedeuserid,
        "username": my_info.get("name", ""),
        "message": "凭证有效",
    }, ensure_ascii=False)


# ========== Tool 1: 搜索视频 ==========

@mcp.tool()
async def bili_search(keyword: str, num: int = 10, order: str = "totalrank") -> str:
    """
    搜索B站视频

    Args:
        keyword: 搜索关键词，如"AI Agent"、"大模型教程"
        num: 返回视频数量，默认10，最大50
        order: 排序方式 totalrank=综合 click=播放量 pubdate=最新 dm=弹幕
    
    Returns:
        JSON格式的视频列表，包含标题、BV号、播放量、评论数、UP主等
    """
    order_map = {
        "totalrank": search.OrderVideo.TOTALRANK,
        "click": search.OrderVideo.CLICK,
        "pubdate": search.OrderVideo.PUBDATE,
        "dm": search.OrderVideo.DM,
    }
    order_enum = order_map.get(order, search.OrderVideo.TOTALRANK)

    result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=1,
        order_type=order_enum,
    )

    videos = []
    for item in result.get("result", [])[:num]:
        title = item.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
        videos.append({
            "bvid": item.get("bvid", ""),
            "aid": item.get("aid", 0),
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "review": item.get("review", 0),
            "danmaku": item.get("video_review", 0),
            "duration": item.get("duration", ""),
            "description": item.get("description", "")[:200],
        })

    return json.dumps({"keyword": keyword, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 2: 获取评论 ==========

@mcp.tool()
async def bili_comments(bvid: str, num: int = 30) -> str:
    """
    获取B站视频的热门评论

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
        num: 获取评论数量，默认30
    
    Returns:
        JSON格式的评论列表，包含用户名、评论内容、点赞数、回复数
    """
    cred = await get_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]

    comments = []
    page = 1
    while len(comments) < num:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
                credential=cred,
            )
            replies = resp.get("replies") or []
            if not replies:
                break

            for r in replies:
                member = r.get("member", {})
                content = r.get("content", {})
                c = {
                    "rpid": r.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": r.get("like", 0),
                    "reply_count": r.get("rcount", 0),
                    "time": r.get("ctime", 0),
                }
                # 子评论
                sub_replies = []
                for sub in (r.get("replies") or [])[:2]:
                    sub_replies.append({
                        "user": sub.get("member", {}).get("uname", ""),
                        "content": sub.get("content", {}).get("message", ""),
                        "like": sub.get("like", 0),
                    })
                if sub_replies:
                    c["top_replies"] = sub_replies
                comments.append(c)

            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            break

    return json.dumps({"bvid": bvid, "count": len(comments[:num]), "comments": comments[:num]}, ensure_ascii=False)


# ========== Tool 3: 获取字幕 ==========

@mcp.tool()
async def bili_subtitle(bvid: str) -> str:
    """
    获取B站视频的AI字幕（语音转文字）

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
    
    Returns:
        视频的完整字幕文本
    """
    cred = await get_cred()
    v = video.Video(bvid=bvid, credential=cred)

    info = await v.get_info()
    cid = info.get("cid", 0)
    if not cid and info.get("pages"):
        cid = info["pages"][0].get("cid", 0)

    if not cid:
        return json.dumps({"error": "无法获取cid"}, ensure_ascii=False)

    subtitle_list = await v.get_subtitle(cid=cid)
    subtitles = subtitle_list.get("subtitles", [])

    if not subtitles:
        return json.dumps({"message": "该视频没有字幕"}, ensure_ascii=False)

    # 优先AI中文字幕
    target = None
    for s in subtitles:
        if s.get("lan") in ["ai-zh", "zh-CN", "zh"]:
            target = s
            break
    if not target:
        target = subtitles[0]

    # 下载字幕
    import aiohttp
    sub_url = target.get("subtitle_url", "")
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url

    async with aiohttp.ClientSession() as session:
        async with session.get(sub_url) as resp:
            sub_data = await resp.json()

    texts = [item.get("content", "") for item in sub_data.get("body", [])]
    full_text = "\n".join(texts)

    return json.dumps({
        "bvid": bvid,
        "title": info.get("title", ""),
        "language": target.get("lan_doc", ""),
        "segments": len(texts),
        "text": full_text,
    }, ensure_ascii=False)


# ========== Tool 4: 获取弹幕 ==========

@mcp.tool()
async def bili_danmaku(bvid: str, num: int = 100) -> str:
    """
    获取B站视频的弹幕

    Args:
        bvid: 视频BV号
        num: 获取弹幕数量，默认100
    
    Returns:
        弹幕列表，包含弹幕文本和出现时间
    """
    cred = await get_cred()
    v = video.Video(bvid=bvid, credential=cred)

    danmakus = await v.get_danmakus(page_index=0)
    result = []
    for d in danmakus[:num]:
        result.append({
            "text": d.text,
            "time": d.dm_time,
        })

    return json.dumps({"bvid": bvid, "count": len(result), "danmakus": result}, ensure_ascii=False)


# ========== Tool 5: 视频详情 ==========

@mcp.tool()
async def bili_video_info(bvid: str) -> str:
    """
    获取B站视频的详细信息

    Args:
        bvid: 视频BV号
    
    Returns:
        视频标题、描述、UP主、播放量、评论数、收藏数等详细数据
    """
    cred = await get_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    stat = info.get("stat", {})

    return json.dumps({
        "bvid": info.get("bvid"),
        "aid": info.get("aid"),
        "title": info.get("title"),
        "description": info.get("desc"),
        "author": info.get("owner", {}).get("name"),
        "duration": info.get("duration"),
        "pages": len(info.get("pages", [])),
        "tags": [t.get("tag_name") for t in info.get("tag", []) if t.get("tag_name")],
        "stat": {
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "favorite": stat.get("favorite", 0),
            "coin": stat.get("coin", 0),
            "like": stat.get("like", 0),
            "share": stat.get("share", 0),
        },
    }, ensure_ascii=False)


# ========== Tool 6: 回复评论 ==========

@mcp.tool()
async def bili_reply(bvid: str, text: str, rpid: int = 0, root: int = 0) -> str:
    """
    在B站视频下发表评论或回复某条评论

    Args:
        bvid: 视频BV号
        text: 评论/回复的文本内容
        rpid: 要回复的目标评论ID（0表示发表新评论）
        root: 楼层根评论ID（回复一级评论时不用填，回复楼中楼时填根评论ID）
    
    Returns:
        发表结果
    """
    cred = await get_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]

    try:
        if rpid == 0:
            # 发表新评论
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                credential=cred,
            )
        else:
            # 回复评论
            # root=0 表示回复一级评论，root=rpid
            # root!=0 表示回复楼中楼，root=根评论，parent=目标评论
            actual_root = root if root != 0 else rpid
            result = await comment.send_comment(
                text=text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                root=actual_root,
                parent=rpid,
                credential=cred,
            )

        return json.dumps({"success": True, "message": "评论发送成功"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 7: 批量采集 ==========

@mcp.tool()
async def bili_crawl(keyword: str, max_videos: int = 5, comments_per_video: int = 20, get_subtitles: bool = True) -> str:
    """
    批量采集：搜索B站视频并抓取每个视频的评论和字幕

    Args:
        keyword: 搜索关键词
        max_videos: 最多采集视频数，默认5
        comments_per_video: 每个视频采集评论数，默认20
        get_subtitles: 是否获取字幕，默认True
    
    Returns:
        包含视频信息、评论和字幕的完整采集数据
    """
    cred = await get_cred()

    # 搜索
    search_result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=1,
        order_type=search.OrderVideo.TOTALRANK,
    )

    results = []
    for item in search_result.get("result", [])[:max_videos]:
        bvid = item.get("bvid", "")
        if not bvid:
            continue

        title = item.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
        video_data = {
            "bvid": bvid,
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "review": item.get("review", 0),
        }

        # 评论
        comments_json = await bili_comments(bvid=bvid, num=comments_per_video)
        comments_data = json.loads(comments_json)

        # 字幕
        subtitle_text = ""
        if get_subtitles:
            try:
                subtitle_json = await bili_subtitle(bvid=bvid)
                subtitle_data = json.loads(subtitle_json)
                subtitle_text = subtitle_data.get("text", "")
            except:
                pass

        results.append({
            "video": video_data,
            "comments": comments_data.get("comments", []),
            "subtitle_text": subtitle_text,
        })

        await asyncio.sleep(1)

    return json.dumps({
        "keyword": keyword,
        "video_count": len(results),
        "total_comments": sum(len(r["comments"]) for r in results),
        "results": results,
    }, ensure_ascii=False)


# ========== 辅助：封面处理 ==========

import subprocess


def _extract_cover_from_video(video_path: str) -> Picture:
    """从视频第3秒截取一帧作为封面，返回 Picture 对象"""
    tmp_cover = os.path.join(tempfile.gettempdir(), f"bili_cover_{os.getpid()}.png")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "3", "-i", video_path,
             "-vframes", "1", "-q:v", "2", tmp_cover],
            capture_output=True, timeout=30,
        )
        if os.path.isfile(tmp_cover) and os.path.getsize(tmp_cover) > 0:
            return Picture.from_file(tmp_cover)
    finally:
        if os.path.isfile(tmp_cover):
            os.remove(tmp_cover)
    # fallback: 第0秒
    tmp_cover2 = os.path.join(tempfile.gettempdir(), f"bili_cover2_{os.getpid()}.png")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "0", "-i", video_path,
             "-vframes", "1", "-q:v", "2", tmp_cover2],
            capture_output=True, timeout=30,
        )
        if os.path.isfile(tmp_cover2) and os.path.getsize(tmp_cover2) > 0:
            return Picture.from_file(tmp_cover2)
    finally:
        if os.path.isfile(tmp_cover2):
            os.remove(tmp_cover2)
    raise RuntimeError(f"无法从视频截取封面: {video_path}")


# ========== 辅助：合集（season） ==========
# 投稿接口（x/vu/web/add/v3）不收合集字段，合集归属是投稿之后单独改的。
# 而且真正能用的是「小节」（section）：新版合集下必有一个默认小节，
# 添加视频走 section/episodes/add，要的是 section_id（不是 season_id）+ 视频的 aid/cid。
# 「上传时加进合集」= 传完拿到 aid → 查该合集的第一个 section → 把 aid/cid 加进去。

SEASON_LIST_API = "https://member.bilibili.com/x2/creative/web/seasons"
SEASON_SECTION_API = "https://member.bilibili.com/x2/creative/web/season/section"
SECTION_ADD_API = "https://member.bilibili.com/x2/creative/web/season/section/episodes/add"
SECTION_DEL_API = "https://member.bilibili.com/x2/creative/web/season/section/episode/del"
VIDEO_VIEW_API = "https://api.bilibili.com/x/web-interface/view"


def _member_api(url: str, method: str, cred: Credential, data: dict | None = None,
                params: dict | None = None, json_body: bool = True):
    """member.bilibili.com 的写接口统一走这里：csrf 在 query，JSON 体里也带一份。

    episode/del 这类老接口要表单体，传 json_body=False。
    """
    from bilibili_api.utils.network import Api

    api = Api(url=url, method=method, credential=cred, no_csrf=True,
              json_body=json_body and data is not None, verify=True)
    api.update_params(**(params or {"csrf": cred.bili_jct}))
    if data is not None:
        api.update_data(**data)
    return api


async def _view_meta(cred: Credential | None = None, bvid: str = "", aid: int = 0) -> dict:
    """拿稿件的 aid / title / cid。

    带上凭据再请求：裸请求会被 B 站 412 风控拦掉（公开接口也一样）。
    """
    from bilibili_api.utils.network import Api

    params = {"bvid": bvid} if bvid else {"aid": int(aid)}
    api = Api(url=VIDEO_VIEW_API, method="GET", credential=cred) if cred else \
        Api(url=VIDEO_VIEW_API, method="GET")
    data = await api.update_params(**params).result
    return data if isinstance(data, dict) else {}


async def _season_sections(cred: Credential, season_id: int = 0) -> list[dict]:
    """我的合集（含每个合集的 section）；给 season_id 就只返回那一个合集的。"""
    from bilibili_api.utils.network import Api

    data = await Api(url=SEASON_LIST_API, method="GET", credential=cred, verify=True).update_params(
        pn=1, ps=100, order="ctime", sort="desc").result
    out = []
    for item in (data or {}).get("seasons") or []:
        season = item.get("season") or {}
        if season_id and int(season.get("id") or 0) != int(season_id):
            continue
        for s in ((item.get("sections") or {}).get("sections") or []):
            out.append({
                "season_id": season.get("id"),
                "season_title": season.get("title"),
                "section_id": s.get("id"),
                "section_title": s.get("title"),
            })
    return out


async def _season_add(cred: Credential, section_id: int, aid: int, cid: int, title: str) -> dict:
    """把一条视频加进合集小节。已经在里面（20080）按成功处理，重复调用是幂等的。"""
    api = _member_api(SECTION_ADD_API, "POST", cred, data={
        "sectionId": int(section_id),
        "episodes": [{"aid": int(aid), "cid": int(cid), "title": title, "charging_pay": 0}],
        "csrf": cred.bili_jct,
    })
    try:
        return await api.result
    except Exception as e:                                    # noqa: BLE001
        if "20080" in str(e):                                 # 当前稿件已存在在合集中
            return {"already_in_season": True}
        raise


async def _season_episode_id(cred: Credential, section_id: int, aid: int) -> int | None:
    """视频在合集里的 episode_id（移除时要用它，不是 aid）。"""
    from bilibili_api.utils.network import Api

    data = await Api(url=SEASON_SECTION_API, method="GET", credential=cred, verify=True).update_params(
        id=int(section_id)).result
    for ep in (data or {}).get("episodes") or []:
        if int(ep.get("aid") or 0) == int(aid):
            return int(ep.get("id") or 0) or None
    return None


async def _attach_season(cred: Credential, aid: int | None, season_id: int, title: str) -> dict:
    """上传成功后挂合集。挂失败只回报，不回滚已经传好的稿件。"""
    if not season_id:
        return {}
    if not aid:
        return {"success": False, "season_id": int(season_id),
                "error": "投稿结果里没有 aid，未能挂合集"}
    try:
        sections = await _season_sections(cred, int(season_id))
        if not sections:
            return {"success": False, "season_id": int(season_id),
                    "error": f"合集 {season_id} 下没有小节（section），加不进去"}
        section = sections[0]
        info = await _view_meta(cred, aid=int(aid))
        cid = ((info.get("pages") or [{}])[0]).get("cid")
        if not cid:
            return {"success": False, "season_id": int(season_id), "error": "拿不到视频 cid"}
        await _season_add(cred, section["section_id"], int(aid),
                          int(cid), (info.get("title") or title or "")[:80])
        return {"success": True, "season_id": int(season_id),
                "section_id": section["section_id"],
                "season_title": section.get("season_title")}
    except Exception as e:                                    # noqa: BLE001
        return {"success": False, "season_id": int(season_id), "error": redact(str(e))}




@mcp.tool()
async def bili_send_dynamic(
    text: str,
    images: list[str] | None = None,
    topic_id: int = 0,
    schedule_time: int = 0,
) -> str:
    """
    发布B站图文动态

    Args:
        text: 动态文本内容，支持@和表情（如 [doge]）
        images: 图片列表，每项可以是本地文件路径或图片URL，最多9张。为空则发布纯文字动态
        topic_id: 话题ID（可选，0表示不关联话题）
        schedule_time: 定时发布的Unix时间戳（可选，0表示立即发布）

    Returns:
        发布结果，包含动态ID
    """
    if not text or not text.strip():
        return json.dumps({"success": False, "error": "text 不能为空"}, ensure_ascii=False)

    cred = await get_cred()

    dyn = dynamic.BuildDynamic.empty()
    dyn.add_plain_text(text.strip())
    # 上传图片
    if images:
        for img_path in images[:9]:
            try:
                if not img_path or not img_path.strip():
                    continue
                img_path = img_path.strip()
                if img_path.startswith(("http://", "https://")):
                    pic = await Picture.async_from_url(img_path)
                else:
                    if not os.path.isfile(img_path):
                        return json.dumps({"success": False, "error": f"图片文件不存在: {img_path}"}, ensure_ascii=False)
                    pic = Picture.from_file(img_path)
                dyn.add_image(pic)
            except Exception as e:
                return json.dumps({"success": False, "error": f"图片处理失败: {img_path} - {str(e)}"}, ensure_ascii=False)

    if topic_id:
        dyn.set_topic(topic_id)

    if schedule_time > 0:
        dyn.set_send_time(schedule_time)

    try:
        result = await dynamic.send_dynamic(info=dyn, credential=cred)
        return json.dumps({
            "success": True,
            "message": "动态发布成功",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 9: 上传视频 ==========

def _current_tids() -> dict:
    """bilibili_api 自带的当前分区表 {子分区ID: "主分区 → 子分区"}；旧分区 ID 不在其中"""
    import bilibili_api
    path = Path(bilibili_api.__file__).parent / "data" / "video_uploader_meta_pre.json"
    with open(path, encoding="utf-8") as f:
        tid_list = json.load(f)["tid_list"]
    return {c["id"]: f'{p["name"]} → {c["name"]}' for p in tid_list for c in p["children"]}


class _MetaWithExtra(VideoMeta):
    """在 VideoMeta 生成的投稿参数上追加库里没有的字段（cover43、creation_statement）"""

    def __init__(self, extra: dict, **kwargs):
        super().__init__(**kwargs)
        self.extra = extra

    def __dict__(self) -> dict:
        d = super().__dict__()
        d.update(self.extra)
        return d


@mcp.tool()
async def bili_upload_video(
    video_path: str,
    title: str,
    desc: str = "",
    tid: int = 124,
    tags: str = "AI",
    cover_path: str = "",
    source: str = "",
    dynamic_text: str = "",
    no_reprint: bool = True,
    cover43_path: str = "",
    publish_time: str = "",
    ai_content: bool = False,
    season_id: int = 0,
) -> str:
    """
    上传视频到B站

    Args:
        video_path: 视频文件的本地路径
        title: 视频标题（最多80字）
        desc: 视频简介描述
        tid: 分区ID，默认124（趣味科普人文-社科·法律·心理）。
             常用分区：231=科技·计算机技术 230=科技·软件应用 95=科技·数码
             201=知识·科学科普 122=知识·野生技能协会 207=知识·财经商业
             228=知识·人文历史 21=生活·日常 17=游戏·单机游戏 171=游戏·电子竞技
             完整列表用 bili_video_zones 查询；已下线的分区 ID 会被拒绝
        tags: 标签，逗号分隔，如"AI,教程,编程"（至少1个标签）
        cover_path: 封面图片路径（可选，不填则B站自动截取）
        source: 转载来源URL（非原创时必填）
        dynamic_text: 粉丝动态文本（可选，投稿时同步发布的动态内容）
        no_reprint: 是否启用未经作者授权禁止转载，默认True
        cover43_path: 4:3 封面图片路径（可选，用于首页等 4:3 位置）
        publish_time: 定时发布时间，本机时区 "YYYY-MM-DD HH:MM"（可选，需距现在 2 小时~15 天；不填则立即发布）
        ai_content: 创作声明"含AI生成内容"，默认False
        season_id: 投稿后直接加入的合集 ID（0 = 不进合集）。用 bili_seasons 查自己的合集；
                   投稿接口本身不收合集字段，这里是在传完之后立刻挂 season/switch，
                   所以返回里会有 "season" 一项说明挂没挂上

    Returns:
        上传结果，包含BV号
    """
    # ---- 参数校验 ----
    # 定时发布最先校验：时间写错时宁可不传，也不能变成立即发布
    delay_time = None
    if publish_time and publish_time.strip():
        try:
            delay_time = int(datetime.strptime(publish_time.strip(), "%Y-%m-%d %H:%M").timestamp())
        except ValueError:
            return json.dumps({"success": False, "error": "publish_time 格式应为 \"YYYY-MM-DD HH:MM\""}, ensure_ascii=False)
        if not time.time() + 7200 <= delay_time <= time.time() + 15 * 86400:
            return json.dumps({"success": False, "error": "publish_time 需距现在 2 小时~15 天"}, ensure_ascii=False)

    # 已下线的分区 ID 会被B站默默放进"生活 → 日常"，提前拦下
    if tid not in _current_tids():
        return json.dumps({"success": False, "error": f"分区 {tid} 已不存在，请用当前分区 ID"}, ensure_ascii=False)

    if not video_path or not video_path.strip():
        return json.dumps({"success": False, "error": "video_path 不能为空，请提供视频文件的本地路径"}, ensure_ascii=False)

    video_path = video_path.strip()
    if not os.path.isfile(video_path):
        return json.dumps({"success": False, "error": f"视频文件不存在: {video_path}"}, ensure_ascii=False)

    if not title or not title.strip():
        return json.dumps({"success": False, "error": "title 不能为空"}, ensure_ascii=False)

    cover43_path = (cover43_path or "").strip()
    if cover43_path and not os.path.isfile(cover43_path):
        return json.dumps({"success": False, "error": f"4:3 封面文件不存在: {cover43_path}"}, ensure_ascii=False)

    cred = await get_cred()

    try:
        # 准备分P
        page = VideoUploaderPage(
            path=video_path,
            title=title.strip()[:80],
            description=(desc or "")[:250],
        )

        # 标签处理
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if not tag_list:
            tag_list = ["视频"]

        # 判断原创/转载
        is_original = not bool(source and source.strip())

        # 封面：用户提供 > 自动从视频截取
        if cover_path and cover_path.strip() and os.path.isfile(cover_path.strip()):
            cover_pic = Picture.from_file(cover_path.strip())
        else:
            cover_pic = _extract_cover_from_video(video_path)

        # VideoMeta 不支持的投稿字段：4:3 封面 URL、创作声明（id 1 = 含AI生成内容）
        extra = {}
        if cover43_path:
            extra["cover43"] = await upload_cover(Picture.from_file(cover43_path), cred)
        if ai_content:
            extra["creation_statement"] = {"id": 1}

        meta = _MetaWithExtra(
            extra,
            tid=tid,
            title=title.strip()[:80],
            desc=desc or "",
            cover=cover_pic,
            tags=tag_list,
            original=is_original,
            source=source.strip() if source and source.strip() else None,
            no_reprint=no_reprint if is_original else False,
            dynamic=dynamic_text.strip() if dynamic_text and dynamic_text.strip() else None,
            delay_time=delay_time,
        )

        uploader = VideoUploader(
            pages=[page],
            meta=meta,
            credential=cred,
        )

        # 上传事件回调：进度进 stderr（不占调用方上下文），失败时回报最后阶段
        progress_info = {"phase": "init", "last": time.monotonic()}

        @uploader.on("__ALL__")
        async def on_event(data: dict):
            progress_info["phase"] = _event_label(data)
            progress_info["last"] = time.monotonic()
            print(f"[bili_upload] {progress_info['phase']}", file=sys.stderr, flush=True)

        with _upload_tuning():
            result = await _start_with_watchdog(uploader, progress_info)

        # 合集：投稿接口不收合集字段。定时稿发布前 B 站也不让加（62003），交给 season_sync 补。
        season_note = {}
        if season_id:
            submitted = result if isinstance(result, dict) else {}
            if delay_time is None:
                season_note = await _attach_season(cred, submitted.get("aid"), season_id, title.strip())
            else:
                season_note = {"success": True, "deferred": True,
                               "note": "定时稿：B 站不让发布前加合集，发布后用 bili_season_sync 补挂"}

        return json.dumps({
            "success": True,
            "message": "视频上传成功",
            "data": result if isinstance(result, dict) else str(result),
            **({"season": season_note} if season_note else {}),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "last_phase": progress_info.get("phase", "unknown") if "progress_info" in dir() else "init",
        }, ensure_ascii=False)


# ========== Tool: 查内容管理里的稿件（含定时发布、审核中） ==========

@mcp.tool()
async def bili_upload_manager_info(ps: int = 10, status: str = "all") -> str:
    """
    列出内容管理（稿件管理）里的稿件，含定时发布、审核中、未过审——公开视频接口看不到这些。

    Args:
        ps: 返回条数，默认 10
        status: all=全部 / pubed=已发布 / not_pubed=未发布 / is_pubing=审核中

    Returns:
        JSON 列表：bvid、标题、状态、发布时间（本地时间）。定时投稿后用它核对是否已提交。
    """
    from bilibili_api import creative_center
    from bilibili_api.creative_center import UploadManagerOrder, UploadManagerStatus

    mapping = {"all": UploadManagerStatus.ALL, "pubed": UploadManagerStatus.PUBED,
               "not_pubed": UploadManagerStatus.NOT_PUBED, "is_pubing": UploadManagerStatus.IS_PUBING}
    if status not in mapping:
        return json.dumps({"success": False, "error": f"status 只能是 {list(mapping)}"}, ensure_ascii=False)
    cred = await get_cred()
    limit = max(1, min(int(ps or 10), 50))

    # status=all 实测会漏掉刚投的（审核中 / 定时待发布）稿件 —— 恰好是最该看到的那几条，
    # 所以 all 改成三个状态分开查再按 bvid 去重合并。
    if status == "all":
        statuses = [UploadManagerStatus.IS_PUBING, UploadManagerStatus.NOT_PUBED,
                    UploadManagerStatus.PUBED]
    else:
        statuses = [mapping[status]]

    seen, items = set(), []
    for st in statuses:
        # 默认排序是「按播放量」，播放量低的稿件根本进不了前 N 条（系列里 #2~#5 就是这么丢的）。
        # 按上传日期排才是我们要的「最近的稿件」。
        data = await creative_center.get_video_upload_manager_info(
            cred, ps=limit, status=st, order=UploadManagerOrder.SENDDATE)
        for item in (data.get("arc_audits") or data.get("archives") or []):
            arc = item.get("Archive") or item
            key = arc.get("bvid") or id(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

    rows = []
    for item in items:
        arc = item.get("Archive") or item
        audit = item.get("Audit") or {}
        ptime, dtime = arc.get("ptime"), arc.get("dtime")
        rows.append({
            "bvid": arc.get("bvid"),
            "title": arc.get("title"),
            # 稿件状态在 Archive 上（例如「通过审核，等待发布」），Audit 不一定有
            "state": arc.get("state_desc") or audit.get("state_desc") or arc.get("state") or audit.get("state"),
            # dtime 才是定时发布时间；为空 = B 站会按立即发布处理
            "ptime": time.strftime("%Y-%m-%d %H:%M", time.localtime(ptime)) if isinstance(ptime, (int, float)) and ptime else None,
            "dtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(dtime)) if isinstance(dtime, (int, float)) and dtime else None,
        })
    return json.dumps({"success": True, "count": len(rows), "videos": rows}, ensure_ascii=False)


# ========== Tool: 合集（season） ==========

@mcp.tool()
async def bili_seasons() -> str:
    """
    列出我自己的合集（新版合集 / season）和它下面的小节（section）。

    投新稿把 season_id 传给 bili_upload_video 就会直接进合集；
    给旧稿补挂用 bili_set_season。

    Returns:
        JSON 列表：season_id、合集标题、section_id、小节标题
    """
    cred = await get_cred()
    rows = await _season_sections(cred)
    return json.dumps({"success": True, "count": len(rows), "seasons": rows}, ensure_ascii=False)


@mcp.tool()
async def bili_set_season(bvid: str = "", aid: int = 0, season_id: int = 0) -> str:
    """
    给已经投稿的视频挂进 / 移出合集（补挂旧稿用；新投稿直接用 bili_upload_video 的 season_id）。

    Args:
        bvid: 视频 BV 号（与 aid 二选一）
        aid: 视频 aid（与 bvid 二选一）
        season_id: 目标合集 ID；0 表示从它当前所在的合集移出

    Returns:
        操作结果（含 aid、合集/小节 ID、标题）
    """
    cred = await get_cred()
    bvid = (bvid or "").strip()
    if not aid and not bvid:
        return json.dumps({"success": False, "error": "bvid 和 aid 至少给一个"}, ensure_ascii=False)

    try:
        info = await _view_meta(cred, bvid=bvid, aid=int(aid or 0))
    except Exception as e:                                    # noqa: BLE001
        msg = str(e)
        # 公开 view 接口对「审核通过、等待发布」的定时稿就报 62003 —— 这时候连标题都查不到，
        # 也就谈不上挂合集了，等它上线再说。
        if "62003" in msg or "等待发布" in msg:
            return json.dumps({"success": False, "bvid": bvid or None,
                               "error": "这条稿件还没发布（B 站：审核通过、等待发布），"
                                        "发布之后才能加合集；pipeline 的 season_sync 会自动补挂"},
                              ensure_ascii=False)
        return json.dumps({"success": False, "error": redact(msg)}, ensure_ascii=False)
    real_aid = int(info.get("aid") or aid or 0)
    if not real_aid:
        return json.dumps({"success": False, "error": f"查不到稿件 {bvid or aid}"}, ensure_ascii=False)
    title = (info.get("title") or "")[:80]

    if season_id:
        sections = await _season_sections(cred, int(season_id))
        if not sections:
            return json.dumps({"success": False, "error": f"合集 {season_id} 下没有小节（section）"},
                              ensure_ascii=False)
        section = sections[0]
        cid = ((info.get("pages") or [{}])[0]).get("cid")
        if not cid:
            return json.dumps({"success": False, "error": "拿不到视频 cid"}, ensure_ascii=False)
        try:
            await _season_add(cred, section["section_id"], real_aid, int(cid), title)
        except Exception as e:                                # noqa: BLE001
            msg = str(e)
            if "62003" in msg or "等待发布" in msg:
                return json.dumps({"success": False, "aid": real_aid, "season_id": int(season_id),
                                   "error": "B 站不让把「审核通过、等待发布」的定时稿加进合集；"
                                            "等它按定时真正发布之后再挂（pipeline 的 season_sync 会自动补）"},
                                  ensure_ascii=False)
            return json.dumps({"success": False, "error": redact(msg), "aid": real_aid,
                               "season_id": int(season_id)}, ensure_ascii=False)
        return json.dumps({"success": True, "aid": real_aid, "season_id": int(season_id),
                           "section_id": section["section_id"], "title": title,
                           "message": f'已加入合集「{section.get("season_title")}」'}, ensure_ascii=False)

    # season_id=0：从当前所在的合集里摘出去（episode/del 要的是 episode_id，不是 aid）
    for s in await _season_sections(cred):
        try:
            ep_id = await _season_episode_id(cred, s["section_id"], real_aid)
        except Exception:                                     # noqa: BLE001
            continue
        if not ep_id:
            continue
        try:
            await _member_api(SECTION_DEL_API, "POST", cred,
                              data={"id": ep_id, "csrf": cred.bili_jct}, json_body=False).result
        except Exception as e:                                # noqa: BLE001
            return json.dumps({"success": False, "error": redact(str(e)), "aid": real_aid},
                              ensure_ascii=False)
        return json.dumps({"success": True, "aid": real_aid, "removed_from": s.get("season_title"),
                           "title": title, "message": "已移出合集"}, ensure_ascii=False)
    return json.dumps({"success": True, "aid": real_aid, "title": title,
                       "message": "这条稿件本来就不在任何合集里"}, ensure_ascii=False)


async def _sync_season(cred: Credential, season_id: int, title_prefix: str = "",
                       limit: int = 20, dry_run: bool = False) -> dict:
    """把「已发布、但还没进合集」的稿件补进合集。season_sync.py 和工具都走这里。"""
    from bilibili_api import user
    from bilibili_api.user import VideoOrder
    from bilibili_api.utils.network import Api

    sections = await _season_sections(cred, int(season_id))
    if not sections:
        return {"success": False, "error": f"合集 {season_id} 下没有小节（section）"}
    section = sections[0]

    data = await Api(url=SEASON_SECTION_API, method="GET", credential=cred,
                     verify=True).update_params(id=int(section["section_id"])).result
    current = {int(ep.get("aid") or 0) for ep in (data or {}).get("episodes") or []}

    uid = int(getattr(cred, "dedeuserid", 0) or 0)
    if not uid:
        return {"success": False, "error": "凭据里没有 uid"}
    listing = await user.User(uid, credential=cred).get_videos(ps=max(1, min(int(limit), 50)), pn=1,
                                              order=VideoOrder.PUBDATE)
    vlist = ((listing or {}).get("list") or {}).get("vlist") or []

    added, skipped, errors, already = [], [], [], 0
    for v in vlist:
        aid, title = int(v.get("aid") or 0), (v.get("title") or "")
        if not aid or (title_prefix and not title.startswith(title_prefix)):
            continue
        if aid in current:
            already += 1
            continue
        if dry_run:
            skipped.append({"aid": aid, "bvid": v.get("bvid"), "title": title, "note": "dry-run"})
            continue
        try:
            info = await _view_meta(cred, aid=aid)
            cid = ((info.get("pages") or [{}])[0]).get("cid")
            if not cid:
                errors.append({"aid": aid, "title": title, "error": "拿不到 cid"})
                continue
            await _season_add(cred, section["section_id"], aid, int(cid), title[:80])
            added.append({"aid": aid, "bvid": v.get("bvid"), "title": title})
        except Exception as e:                                # noqa: BLE001
            msg = str(e)
            if "62003" in msg or "等待发布" in msg:
                skipped.append({"aid": aid, "title": title, "note": "未发布，等它上线"})
            else:
                errors.append({"aid": aid, "title": title, "error": msg[:200]})

    return {"success": True, "season_id": int(season_id),
            "season_title": section.get("season_title"), "section_id": section["section_id"],
            "already": already, "added": added, "skipped": skipped, "errors": errors}


@mcp.tool()
async def bili_season_sync(season_id: int = 0, title_prefix: str = "", limit: int = 20,
                           dry_run: bool = False) -> str:
    """
    把「已经发布、但还没进合集」的稿件补进合集。

    为什么要补：B 站不让把「审核通过、等待发布」的定时稿加进合集（62003），
    所以投稿当时挂不上，得等它按定时真正发布之后再挂。重复调用是幂等的。

    Args:
        season_id: 合集 ID（0 = 用 config 里记的那一个）
        title_prefix: 只补标题以它开头的稿件，留空表示不限
        limit: 检查最近多少个已发布稿件，默认 20
        dry_run: 只列出会补哪些，不真的动手

    Returns:
        JSON：already（已在合集）、added（本次补上）、skipped（含未发布的）、errors
    """
    cred = await get_cred()
    try:
        return json.dumps(await _sync_season(cred, int(season_id or 0), title_prefix,
                                             limit, dry_run), ensure_ascii=False)
    except Exception as e:                                    # noqa: BLE001
        return json.dumps({"success": False, "error": redact(str(e))}, ensure_ascii=False)


# ========== Tool 10: 上传多P视频 ==========

@mcp.tool()
async def bili_upload_video_multi(
    video_paths: list[str],
    page_titles: list[str],
    title: str,
    desc: str = "",
    tid: int = 124,
    tags: str = "AI",
    cover_path: str = "",
    source: str = "",
    season_id: int = 0,
) -> str:
    """
    上传多P视频到B站（多个分P合并为一个投稿）

    Args:
        video_paths: 视频文件路径列表，如["/path/p1.mp4", "/path/p2.mp4"]
        page_titles: 各分P标题列表，与video_paths一一对应
        title: 视频总标题
        desc: 视频简介
        tid: 分区ID，默认124
        tags: 标签，逗号分隔
        cover_path: 封面图片路径（可选）
        source: 转载来源URL（非原创时必填）
        season_id: 投稿后直接加入的合集 ID（0 = 不进合集）

    Returns:
        上传结果
    """
    cred = await get_cred()

    if not video_paths:
        return json.dumps({"success": False, "error": "video_paths 不能为空"}, ensure_ascii=False)
    if not page_titles:
        return json.dumps({"success": False, "error": "page_titles 不能为空"}, ensure_ascii=False)
    if len(video_paths) != len(page_titles):
        return json.dumps({"success": False, "error": "video_paths 和 page_titles 数量必须一致"}, ensure_ascii=False)

    try:
        pages = []
        for i, vp in enumerate(video_paths):
            vp = (vp or "").strip()
            if not vp or not os.path.isfile(vp):
                return json.dumps({"success": False, "error": f"文件不存在: {vp}"}, ensure_ascii=False)
            pages.append(VideoUploaderPage(
                path=vp,
                title=(page_titles[i] or "").strip()[:80] or f"P{i+1}",
                description="",
            ))

        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or ["视频"]
        is_original = not bool(source and source.strip())

        # 封面：用户提供 > 从第一个视频自动截取
        if cover_path and cover_path.strip() and os.path.isfile(cover_path.strip()):
            cover_pic = Picture.from_file(cover_path.strip())
        else:
            cover_pic = _extract_cover_from_video(pages[0].path)

        meta = VideoMeta(
            tid=tid,
            title=(title or "").strip()[:80],
            desc=desc or "",
            cover=cover_pic,
            tags=tag_list,
            original=is_original,
            source=source.strip() if source and source.strip() else None,
        )

        uploader = VideoUploader(pages=pages, meta=meta, credential=cred)

        result = await uploader.start()
        season_note = {}
        if season_id:
            submitted = result if isinstance(result, dict) else {}
            season_note = await _attach_season(cred, submitted.get("aid"), season_id, (title or "").strip())
        return json.dumps({
            "success": True,
            "message": f"多P视频上传成功（共{len(pages)}P）",
            "data": result if isinstance(result, dict) else str(result),
            **({"season": season_note} if season_note else {}),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 11: 发布专栏文章 (Opus) ==========

@mcp.tool()
async def bili_send_opus(
    title: str,
    content: str,
    images: list[str] | None = None,
    category_id: int = 0,
) -> str:
    """
    发布B站图文（Opus，新版专栏）

    Args:
        title: 文章标题
        content: 文章正文内容（纯文本，段落用换行分隔）
        images: 文章中插入的图片路径或URL列表（可选）
        category_id: 分类ID（可选，0表示不指定）

    Returns:
        发布结果
    """
    cred = await get_cred()

    dyn = dynamic.BuildDynamic.empty()

    # 专栏动态以纯文本+图片方式构建
    # 标题作为第一行加粗
    full_text = f"【{title}】\n\n{content}"
    dyn.add_plain_text(full_text)

    if images:
        for img_path in images:
            try:
                if not img_path or not img_path.strip():
                    continue
                img_path = img_path.strip()
                if img_path.startswith(("http://", "https://")):
                    pic = await Picture.async_from_url(img_path)
                else:
                    pic = Picture.from_file(img_path)
                dyn.add_image(pic)
            except Exception as e:
                pass  # 图片失败不阻断发布

    try:
        result = await dynamic.send_dynamic(info=dyn, credential=cred)
        return json.dumps({
            "success": True,
            "message": "图文发布成功",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 12: 查询分区列表 ==========

@mcp.tool()
async def bili_video_zones() -> str:
    """
    获取B站当前投稿分区ID列表（来自 bilibili_api 自带的分区表），供上传视频时选择tid参数

    Returns:
        {主分区: {子分区ID: 子分区名}}
    """
    zones = {}
    for tid, name in _current_tids().items():
        parent, child = name.split(" → ")
        zones.setdefault(parent, {})[tid] = child
    return json.dumps(zones, ensure_ascii=False)

# ========================================================================
#                        V1.2 — 数据分析 & 互动运营
# ========================================================================


# ========== Tool 13: 热门视频 ==========

@mcp.tool()
async def bili_hot_videos(pn: int = 1, ps: int = 20) -> str:
    """
    获取B站当前热门视频列表

    Args:
        pn: 页码，默认1
        ps: 每页数量，默认20，最大50

    Returns:
        热门视频列表，包含标题、播放量、UP主等
    """
    result = await hot.get_hot_videos(pn=pn, ps=min(ps, 50))
    videos = []
    for item in result.get("list", []):
        stat = item.get("stat", {})
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "author": item.get("owner", {}).get("name", ""),
            "play": stat.get("view", 0),
            "like": stat.get("like", 0),
            "danmaku": stat.get("danmaku", 0),
            "reply": stat.get("reply", 0),
            "desc": (item.get("desc", "") or "")[:100],
            "duration": item.get("duration", 0),
            "tname": item.get("tname", ""),
        })
    return json.dumps({"page": pn, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 14: 热搜关键词 ==========

@mcp.tool()
async def bili_hot_buzzwords(page_num: int = 1, page_size: int = 20) -> str:
    """
    获取B站热搜词/热门关键词

    Args:
        page_num: 页码，默认1
        page_size: 每页数量，默认20

    Returns:
        热搜词列表
    """
    result = await hot.get_hot_buzzwords(page_num=page_num, page_size=page_size)
    return json.dumps(result, ensure_ascii=False)


# ========== Tool 15: 每周必看 ==========

@mcp.tool()
async def bili_weekly_hot(week: int = 0) -> str:
    """
    获取B站每周必看视频推荐

    Args:
        week: 期数（0表示获取期数列表，>0表示获取该期的视频）

    Returns:
        每周必看期数列表或指定期的视频列表
    """
    if week <= 0:
        result = await hot.get_weekly_hot_videos_list()
        return json.dumps(result, ensure_ascii=False)
    else:
        result = await hot.get_weekly_hot_videos(week=week)
        return json.dumps(result, ensure_ascii=False)


# ========== Tool 16: 排行榜 ==========

@mcp.tool()
async def bili_rank(category: str = "all", day: int = 3) -> str:
    """
    获取B站各分区排行榜

    Args:
        category: 分区名，可选值：
            all=全站 original=原创 rookie=新人
            douga=动画 music=音乐 dance=舞蹈 game=游戏
            knowledge=知识 technology=科技 sports=运动 car=汽车
            life=生活 food=美食 animal=动物 fashion=时尚
            ent=娱乐 cinephile=影视
        day: 时间维度，3=三日 7=七日

    Returns:
        排行榜视频列表
    """
    type_map = {
        "all": rank.RankType.All, "original": rank.RankType.Original,
        "rookie": rank.RankType.Rookie, "douga": rank.RankType.Douga,
        "music": rank.RankType.Music, "dance": rank.RankType.Dance,
        "game": rank.RankType.Game, "knowledge": rank.RankType.Knowledge,
        "technology": rank.RankType.Technology, "sports": rank.RankType.Sports,
        "car": rank.RankType.Car, "life": rank.RankType.Life,
        "food": rank.RankType.Food, "animal": rank.RankType.Animal,
        "fashion": rank.RankType.Fashion, "ent": rank.RankType.Ent,
        "cinephile": rank.RankType.Cinephile,
    }
    day_map = {3: rank.RankDayType.THREE_DAY, 7: rank.RankDayType.WEEK}

    rank_type = type_map.get(category.lower(), rank.RankType.All)
    rank_day = day_map.get(day, rank.RankDayType.THREE_DAY)

    result = await rank.get_rank(type_=rank_type, day=rank_day)
    videos = []
    for item in result.get("list", []):
        stat = item.get("stat", {})
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "author": item.get("owner", {}).get("name", ""),
            "play": stat.get("view", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "score": item.get("score", 0),
            "tname": item.get("tname", ""),
        })
    return json.dumps({"category": category, "day": day, "count": len(videos), "videos": videos}, ensure_ascii=False)


# ========== Tool 17: 用户信息 ==========

@mcp.tool()
async def bili_user_info(uid: int) -> str:
    """
    获取B站用户的详细信息

    Args:
        uid: 用户UID

    Returns:
        用户昵称、粉丝数、关注数、签名、等级、视频数等
    """
    cred = await get_cred()
    u = user.User(uid=uid, credential=cred)
    info = await u.get_user_info()

    # 尝试获取UP主数据
    up_stat = {}
    try:
        up_stat = await u.get_up_stat()
    except:
        pass

    relation = {}
    try:
        relation = await u.get_relation_info()
    except:
        pass

    return json.dumps({
        "uid": uid,
        "name": info.get("name", ""),
        "sign": info.get("sign", ""),
        "level": info.get("level", 0),
        "face": info.get("face", ""),
        "fans": relation.get("follower", info.get("follower", 0)),
        "following": relation.get("following", info.get("following", 0)),
        "likes": up_stat.get("likes", 0),
        "archive_view": up_stat.get("archive", {}).get("view", 0),
        "article_view": up_stat.get("article", {}).get("view", 0),
        "is_senior_member": info.get("is_senior_member", 0),
        "top_photo": info.get("top_photo", ""),
    }, ensure_ascii=False)


# ========== Tool 18: 用户视频列表 ==========

@mcp.tool()
async def bili_user_videos(uid: int, pn: int = 1, ps: int = 30, order: str = "pubdate", keyword: str = "") -> str:
    """
    获取B站用户的投稿视频列表

    Args:
        uid: 用户UID
        pn: 页码，默认1
        ps: 每页数量，默认30
        order: 排序方式 pubdate=最新 click=播放量 stow=收藏
        keyword: 搜索关键词（在该用户视频中搜索）

    Returns:
        视频列表
    """
    cred = await get_cred()
    u = user.User(uid=uid, credential=cred)

    order_map = {
        "pubdate": user.VideoOrder.PUBDATE,
        "click": user.VideoOrder.VIEW,
        "stow": user.VideoOrder.FAVORITE,
    }
    order_enum = order_map.get(order, user.VideoOrder.PUBDATE)

    result = await u.get_videos(pn=pn, ps=ps, order=order_enum, keyword=keyword)
    videos = []
    for item in result.get("list", {}).get("vlist", []):
        videos.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "play": item.get("play", 0),
            "comment": item.get("comment", 0),
            "created": item.get("created", 0),
            "length": item.get("length", ""),
            "description": (item.get("description", "") or "")[:100],
        })
    return json.dumps({
        "uid": uid,
        "page": pn,
        "total": result.get("page", {}).get("count", 0),
        "count": len(videos),
        "videos": videos,
    }, ensure_ascii=False)


# ========== Tool 19: 收藏夹列表 ==========

@mcp.tool()
async def bili_favorite_lists(uid: int = 0) -> str:
    """
    获取用户的收藏夹列表

    Args:
        uid: 用户UID（0表示获取自己的收藏夹）

    Returns:
        收藏夹列表，包含ID、名称、视频数量
    """
    cred = await get_cred()
    if uid == 0:
        own_uid = str(cred.dedeuserid or "")
        if not own_uid.isdigit() or int(own_uid) <= 0:
            return json.dumps({
                "success": False,
                "error": "凭证有效，但未能取得当前用户 UID",
            }, ensure_ascii=False)
        uid = int(own_uid)

    result = await favorite_list.get_video_favorite_list(uid=uid, credential=cred)
    fav_lists = []
    for item in (result.get("list", []) or []):
        fav_lists.append({
            "id": item.get("id", 0),
            "title": item.get("title", ""),
            "media_count": item.get("media_count", 0),
            "fav_state": item.get("fav_state", 0),
        })
    return json.dumps({"uid": uid, "count": len(fav_lists), "lists": fav_lists}, ensure_ascii=False)


# ========== Tool 20: 收藏夹内容 ==========

@mcp.tool()
async def bili_favorite_content(media_id: int, page: int = 1, keyword: str = "") -> str:
    """
    获取收藏夹内的视频列表

    Args:
        media_id: 收藏夹ID（从 bili_favorite_lists 获取）
        page: 页码，默认1
        keyword: 搜索关键词（在收藏夹内搜索）

    Returns:
        收藏夹内的视频列表
    """
    cred = await get_cred()
    result = await favorite_list.get_video_favorite_list_content(
        media_id=media_id,
        page=page,
        keyword=keyword if keyword else None,
        credential=cred,
    )
    medias = []
    for item in (result.get("medias", []) or []):
        medias.append({
            "bvid": item.get("bvid", ""),
            "title": item.get("title", ""),
            "play": item.get("cnt_info", {}).get("play", 0),
            "collect": item.get("cnt_info", {}).get("collect", 0),
            "author": item.get("upper", {}).get("name", ""),
            "duration": item.get("duration", 0),
            "fav_time": item.get("fav_time", 0),
        })
    return json.dumps({
        "media_id": media_id,
        "page": page,
        "has_more": result.get("has_more", False),
        "count": len(medias),
        "medias": medias,
    }, ensure_ascii=False)


# ========== Tool 21: 发私信 ==========

@mcp.tool()
async def bili_send_message(receiver_uid: int, text: str) -> str:
    """
    给B站用户发送私信

    Args:
        receiver_uid: 接收者的UID
        text: 私信文本内容

    Returns:
        发送结果
    """
    cred = await get_cred()

    if not text or not text.strip():
        return json.dumps({"success": False, "error": "text 不能为空"}, ensure_ascii=False)

    try:
        result = await session.send_msg(
            credential=cred,
            receiver_id=receiver_uid,
            msg_type=session.EventType.TEXT,
            content=text.strip(),
        )
        return json.dumps({
            "success": True,
            "message": f"私信已发送给UID:{receiver_uid}",
            "data": result if isinstance(result, dict) else str(result),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


# ========== Tool 22: 未读消息 ==========

@mcp.tool()
async def bili_unread_messages() -> str:
    """
    获取B站未读消息数（私信、@、回复、点赞等）

    Returns:
        各类未读消息数量
    """
    cred = await get_cred()
    try:
        result = await session.get_unread_messages(credential=cred)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ========== Tool 23: 最近收到的回复 ==========

@mcp.tool()
async def bili_received_replies() -> str:
    """
    获取最近收到的评论回复通知

    Returns:
        回复列表
    """
    cred = await get_cred()
    try:
        result = await session.get_replies(credential=cred)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ========== Tool 24: 最近收到的@和点赞 ==========

@mcp.tool()
async def bili_received_at_and_likes() -> str:
    """
    获取最近收到的@提及和点赞通知

    Returns:
        包含 at 和 likes 两部分的通知数据
    """
    cred = await get_cred()
    result = {}
    try:
        result["at"] = await session.get_at(credential=cred)
    except Exception as e:
        result["at_error"] = str(e)
    try:
        result["likes"] = await session.get_likes(credential=cred)
    except Exception as e:
        result["likes_error"] = str(e)
    return json.dumps(result, ensure_ascii=False)


# ========== 启动 ==========

if __name__ == "__main__":
    mcp.run(transport="stdio")
