# QQ 群通知自动化管道 (qq-notify-pipeline)

> 每天自动扫描你加入的 QQ 群聊，提取有价值的通知（课程调整、活动报名、截止日期等），写入飞书日历和任务，并生成汇总报告上传飞书云空间。全程无人值守。

## 它能做什么

- **断点续传**：每次只拉上次导出之后的新消息，往前重合 12 小时防遗漏
- **智能通知识别**：自动从闲聊里挑出带时间/地点/截止日期的通知，按紧急程度排序
- **正确的时间理解**：以"通知发布时间"为基准解析"明天/后天/本周X/下周X"，不会把旧通知当成新通知
- **自动去重**：和现有飞书日历/任务比对，重复的不重复添加；发现历史重复自动清理
- **防张冠李戴**：通知原文由脚本自动生成描述并校验，AI 不会把 A 通知贴到 B 日程上
- **冷处理**：连续 7 天没消息的群自动降为每周检测一次，有新消息再恢复
- **错误告警**：连续失败 3 次自动通过飞书机器人告警

## 架构

```
QQ群聊 ──> NapCatQQ + QCE ──> 聊天记录JSON
                                      │
                              daily_pipeline.py
                              (导出/分析/去重/冷处理)
                                      │
                              _pipeline_summary.json
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                         ▼
        飞书日历事件            飞书任务清单              汇总报告.md
        (lark-cli)             (lark-cli)               (上传飞书云空间)
```

## 前置依赖

| 组件 | 用途 |
|------|------|
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 本地 QQ 无头框架 |
| [QCE (qq-chat-exporter)](https://github.com/shuakami/qq-chat-exporter) | 通过 REST API 导出 QQ 聊天记录 |
| Python 3.9+ | 运行管道脚本 |
| [lark-cli](https://www.feishu.cn/) | 操作飞书日历/任务/云空间 |
| Windows | QCE 和 NapCat 当前为 Windows 版 |

## 快速开始

### 1. 安装并登录 QCE

1. 下载 QCE Windows x64 版，解压到任意目录，运行 `launcher-user.bat`
2. 用手机 QQ 扫码登录（仅首次）
3. token 自动从 `%USERPROFILE%\.qq-chat-exporter\security.json` 读取，无需手动配置

验证：
```powershell
curl -H "Authorization: Bearer <你的token>" http://localhost:40653/api/groups
```

### 2. 配置监控群

复制示例配置，填入你要监控的群（群号+群名）：
```powershell
copy config\monitored_groups.example.json monitored_groups.json
```

运行下面命令让 AI 帮你拉取全部群列表并勾选：
```
请调用 QCE API /api/groups，列出我加入的所有群，让我勾选要监控的，然后写入 monitored_groups.json
```

### 3. 跑一次管道

```powershell
cd <工作目录>
python scripts\daily_pipeline.py
```

完成后查看 `_pipeline_summary.json`，里面是识别出的新通知。

### 4. 写入飞书

用 `lark-cli` 把通知写进日历和任务（见 `prompts/setup_prompt.md` 里的完整定时任务提示词）。

### 5. 建定时任务

在豆包（或其他支持 cron 的 AI 工具）创建每天 12:00 触发的任务，执行提示词见 `prompts/setup_prompt.md`。

## 文件说明

| 文件 | 作用 |
|------|------|
| `scripts/daily_pipeline.py` | 主管道：检测QCE→并行导出→断点续传→通知分析→冷处理 |
| `scripts/feishu_dedup.py` | 与现有飞书日历/任务比对去重 |
| `scripts/error_monitor.py` | 连续失败检测与飞书webhook告警 |
| `config/monitored_groups.example.json` | 监控群名单示例 |
| `prompts/setup_prompt.md` | 发给 AI 的完整搭建+每日执行提示词 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `QCE_ACCESS_TOKEN` | QCE token（默认从 security.json 自动读） |
| `QCE_BASE_URL` | 默认 `http://localhost:40653` |
| `FEISHU_WEBHOOK_URL` | 飞书自定义机器人 webhook，用于错误告警（可选） |

## License

MIT
