# Feishu AI Bridge

将飞书即时消息与 AI CLI 后端（Trae CLI / Qwen Code）桥接的服务程序。用户通过飞书发送消息，服务通过飞书事件订阅接收消息，调用 AI 后端处理指令，最终将结果以飞书 Markdown 消息形式返回。

## 核心特性

- **飞书事件订阅**：通过 `lark-cli event consume` 实时接收消息
- **多后端支持**：Trae CLI、Qwen Code，可动态切换
- **多 Session 并行**：每个 Session 独立 TaskQueue，互不干扰
- **Markdown 渲染**：AI 回复以飞书富文本（post）发送，表格/标题/代码块正确渲染
- **智能消息提示**：闲聊快速回复跳过"思考中"提示，长任务显示进度
- **进程管理**：PID 锁防多实例，孤儿进程自动清理，信号优雅退出
- **配置档案**：通过 profiles 目录管理不同 AI 模型配置
- **i18n 支持**：用户界面文案集中管理

## 项目结构

```
feishu-ai-bridge/
├── main.py                      # 入口：PID锁、信号处理、主事件循环
├── restart_service.py           # 服务重启脚本（清理旧进程）
├── settings.yaml                # 主配置（飞书、后端、桥接参数）
├── com.feishu-trae-bridge.plist # macOS launchd 自启动配置
├── profiles/                    # AI 模型配置档案
│   ├── default.yaml
│   ├── deepseek.yaml
│   ├── doubao.yaml
│   ├── kimi.yaml
│   └── reviewer.yaml
└── feishu_ai_bridge/            # 核心包
    ├── __init__.py
    ├── config.py                # 配置加载、AppContext
    ├── feishu.py                # 飞书消息发送（text/markdown）
    ├── event_consumer.py        # 事件订阅、消息处理、进程管理
    ├── session.py               # Session/SessionPool 会话管理
    ├── queue.py                 # TaskQueue 任务队列
    ├── commands.py              # 内置命令处理（/help /status 等）
    ├── backend.py               # AI 后端调用抽象
    ├── traecli.py               # Trae CLI 流式调用
    ├── qwencli.py               # Qwen Code 调用
    └── i18n.py                  # 界面文案
```

## 内置命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息 |
| `/status` | 查看所有 Session 状态 |
| `/reset` | 重置当前 Session 上下文 |
| `/restart` | 重启整个服务 |
| `/stop` | 强制停止当前 Session 的任务 |
| `/backend` | 查看/切换 AI 后端 |
| `/profile` | 查看/切换配置档案 |
| `/session` | 查看/创建/切换/终止 Session |

## 使用方式

- `/创建一个hello.txt文件` — 指令模式，AI 执行操作并回复
- `你好` — 闲聊模式，转发给 AI 对话

## 依赖

- Python 3.10+
- [lark-cli](https://github.com/larksuite/cli)（飞书 CLI）
- Trae CLI 或 Qwen Code（AI 后端）
- PyYAML

## 配置

编辑 `settings.yaml`，填入你的飞书 `chat_id`、`lark_cli` 路径和 `my_open_id`。

## 运行

```bash
python3 main.py
```

或使用 macOS launchd 自启动：

```bash
cp com.feishu-trae-bridge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.feishu-trae-bridge.plist
```
