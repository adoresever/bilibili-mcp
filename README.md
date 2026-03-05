# bilibili-mcp

B站（Bilibili）MCP Server —— 让 AI 助手直接操作B站。

支持 OpenClaw / Claude Code / Cursor / Cline 等所有 MCP 客户端。

## 功能

| Tool | 功能 | 说明 |
|------|------|------|
| `bili_search` | 搜索视频 | 按关键词搜索，支持按播放量/最新/弹幕排序 |
| `bili_comments` | 获取评论 | 获取视频热门评论，含子评论 |
| `bili_subtitle` | 获取字幕 | 获取视频AI字幕（语音转文字） |
| `bili_danmaku` | 获取弹幕 | 获取视频弹幕列表 |
| `bili_video_info` | 视频详情 | 获取播放量、评论数、收藏数等 |
| `bili_reply` | 回复评论 | 发表评论或回复评论（支持楼中楼） |
| `bili_crawl` | 批量采集 | 搜索+评论+字幕一步到位 |

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/你的用户名/bilibili-mcp.git
cd bilibili-mcp
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 扫码登录

```bash
python bili_login.py
```

终端会显示二维码，用B站App扫码确认，凭证自动保存到 `bili_credential.json`。

### 4. 测试运行

```bash
# 用 MCP Inspector 测试
npx @modelcontextprotocol/inspector python mcp_server.py

# 或直接 Python 测试
python -c "
import asyncio
from mcp_server import bili_search
print(asyncio.run(bili_search('AI Agent', num=3)))
"
```

## 接入 AI 工具

### OpenClaw

```bash
# 通过 MCPorter 接入
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

- "搜索B站上关于AI Agent的热门视频"
- "获取这个视频的评论，分析用户需求"
- "获取视频字幕，总结视频内容"
- "帮我回复这条评论"
- "批量采集OpenClaw相关视频的评论和字幕"

## 技术栈

- **[bilibili-api-python](https://github.com/Nemo2011/bilibili-api)** — B站 API 封装库，提供搜索、评论、字幕、弹幕等全部接口
- **[MCP (Model Context Protocol)](https://modelcontextprotocol.io/)** — Anthropic 提出的开放协议，标准化 AI 与工具的交互
- **[FastMCP](https://github.com/modelcontextprotocol/python-sdk)** — MCP Python SDK，快速构建 MCP Server

## 项目结构

```
bilibili-mcp/
├── mcp_server.py          # MCP Server 主文件（7个tool）
├── bili_login.py           # 扫码登录脚本
├── bili_credential.json    # 登录凭证（自动生成，勿提交）
├── requirements.txt        # Python 依赖
├── README.md               # 项目说明
├── LICENSE                 # MIT 开源协议
└── .gitignore              # Git 忽略文件
```

## 注意事项

- 首次使用需扫码登录，凭证保存在本地，不会上传
- 请求间隔自动控制，避免频率过快
- 回复评论功能请谨慎使用，遵守B站社区规则
- 本项目仅用于学习和研究

## License

MIT
