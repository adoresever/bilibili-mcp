# bilibili-mcp

B站（Bilibili）MCP Server —— 让 AI 助手直接操作B站。

支持 OpenClaw / Claude Code / Cursor / Cline 等所有 MCP 客户端。

**27 个工具**，覆盖登录认证、搜索采集、内容发布、数据分析、互动运营。

## 功能

### 登录认证

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_login` | 扫码登录 | 生成二维码（base64图片），AI直接展示给用户扫码 |
| `bili_login_check` | 登录状态检查 | 轮询扫码状态，扫码成功后自动保存凭证 |
| `bili_check_credential` | 凭证验证 | 检查当前登录是否有效，返回用户名等信息 |

### 数据采集

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_search` | 搜索视频 | 按关键词搜索，支持按播放量/最新/弹幕排序 |
| `bili_comments` | 获取评论 | 获取视频热门评论，含子评论 |
| `bili_subtitle` | 获取字幕 | 获取视频AI字幕（语音转文字） |
| `bili_danmaku` | 获取弹幕 | 获取视频弹幕列表 |
| `bili_video_info` | 视频详情 | 获取播放量、评论数、收藏数等 |
| `bili_reply` | 回复评论 | 发表评论或回复评论（支持楼中楼） |
| `bili_crawl` | 批量采集 | 搜索+评论+字幕一步到位 |

### 内容发布

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_send_dynamic` | 发图文动态 | 发布文字/图文动态，支持定时发布、话题关联 |
| `bili_upload_video` | 上传视频 | 单P视频上传，支持封面、标签、分区选择 |
| `bili_upload_video_multi` | 多P上传 | 多分P视频合并投稿 |
| `bili_send_opus` | 图文专栏 | 发布图文长内容（Opus） |
| `bili_video_zones` | 分区查询 | 获取常用分区ID，辅助视频上传 |

### 数据分析

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_hot_videos` | 热门视频 | 获取当前B站热门视频列表 |
| `bili_hot_buzzwords` | 热搜关键词 | 获取B站热搜词/热门话题 |
| `bili_weekly_hot` | 每周必看 | 获取每周必看推荐视频 |
| `bili_rank` | 排行榜 | 全站及17个分区排行榜 |
| `bili_user_info` | 用户信息 | 获取UP主粉丝数、等级、总播放量等 |
| `bili_user_videos` | 用户视频 | 获取UP主投稿列表，支持排序和搜索 |

### 互动运营

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_favorite_lists` | 收藏夹列表 | 获取自己或他人的收藏夹 |
| `bili_favorite_content` | 收藏夹内容 | 获取收藏夹内视频，支持搜索 |
| `bili_send_message` | 发私信 | 给指定用户发送文字私信 |
| `bili_unread_messages` | 未读消息 | 获取私信、@、回复、点赞等未读数 |
| `bili_received_replies` | 收到的回复 | 获取评论回复通知 |
| `bili_received_at_and_likes` | @和点赞 | 获取@提及和点赞通知 |

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/adoresever/bilibili-mcp.git
cd bilibili-mcp
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10+，视频上传自动截取封面需要 ffmpeg。

### 3. 登录

**方式一：通过 AI 工具登录（推荐）**

接入 MCP 后，直接对 AI 说"登录B站"，AI 会调用 `bili_login` 生成二维码展示给你扫码，全程无需终端操作。

**方式二：命令行登录**

```bash
python bili_login.py
```

### 4. 测试运行

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

## 接入 AI 工具

### OpenClaw

```bash
npm i -g mcporter
npx mcporter config add bilibili-mcp "python /path/to/bilibili-mcp/mcp_server.py"
```

或直接把 GitHub 链接粘贴到 OpenClaw 对话框，让它自动配置。

### Claude Code

在项目目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "bilibili-mcp": {
      "command": "python",
      "args": ["/path/to/bilibili-mcp/mcp_server.py"]
    }
  }
}
```

### Cursor / Cline

在设置中添加 MCP Server，command 填 `python`，args 填 `mcp_server.py` 的完整路径。

## 使用示例

接入后，你可以直接用自然语言让 AI 操作：

- "登录B站"（AI会展示二维码给你扫）
- "搜索B站上关于AI Agent的热门视频"
- "获取这个视频的评论，分析用户需求"
- "获取视频字幕，总结视频内容"
- "帮我回复这条评论"
- "批量采集OpenClaw相关视频的评论和字幕"
- "帮我发一条B站动态，配上这几张图片"
- "把这个视频上传到B站科技区"
- "看看B站现在什么最火"
- "获取科技区排行榜前10的视频"
- "分析这个UP主的粉丝和投稿数据"
- "我有多少未读私信和回复？"
- "看看我的收藏夹里有什么"

## 技术栈

- **[bilibili-api-python](https://github.com/Nemo2011/bilibili-api)** — B站 API 封装库，提供搜索、评论、字幕、弹幕等全部接口
- **[MCP (Model Context Protocol)](https://modelcontextprotocol.io/)** — Anthropic 提出的开放协议，标准化 AI 与工具的交互
- **[FastMCP](https://github.com/modelcontextprotocol/python-sdk)** — MCP Python SDK，快速构建 MCP Server

## 项目结构

```
bilibili-mcp/
├── mcp_server.py          # MCP Server 主文件（27个tool）
├── bili_login.py           # 命令行扫码登录（备用）
├── bili_credential.json    # 登录凭证（自动生成，勿提交）
├── requirements.txt        # Python 依赖
├── README.md               # 项目说明
├── LICENSE                 # MIT 开源协议
└── .gitignore              # Git 忽略文件
```

## 注意事项

- 首次使用通过 AI 对话即可完成登录，无需终端操作
- 凭证保存在本地，不会上传
- 请求间隔自动控制，避免频率过快
- 回复评论/发私信功能请谨慎使用，遵守B站社区规则
- 视频上传未指定封面时，自动从视频第3秒截取（需要ffmpeg）
- 本项目仅用于学习和研究

## License

MIT