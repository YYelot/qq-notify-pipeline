---
name: qq-notify-pipeline
description: "每日自动导出指定QQ群聊记录，提取带时间/地点/截止日期的通知，写入飞书日历和任务，并生成汇总报告上传飞书云空间。使用场景：(1) 用户要求每天自动汇总QQ群通知；(2) 监控课程群/通知群/活动群里的重要消息；(3) 把QQ群通知同步到飞书日历。前置：QCE服务已启动并完成QQ扫码登录，lark-cli已登录。"
---

# QQ 群通知自动化管道

## 前置条件
1. QCE (qq-chat-exporter) 服务已运行在 `http://localhost:40653`，QQ 已扫码登录
2. token 自动从 `%USERPROFILE%\.qq-chat-exporter\security.json` 读取
3. `lark-cli` 已登录飞书
4. 工作目录下有 `monitored_groups.json`（监控群名单）

## 执行流程

### 1. 跑管道脚本
```powershell
python scripts\daily_pipeline.py
```
完成后读 `_pipeline_summary.json`：
- `analysis.notices`：新通知列表（按优先级🔴🟡🟢排序，已去重合并）
- `analysis.priority_summary`：各优先级数量

### 2. 拉飞书现有数据去重
```powershell
lark-cli calendar events list --start "<今天>" --end "<7天后>" --format json > calendar_events.json
lark-cli task tasklists tasks --params '{"tasklist_guid":"<你的任务清单GUID>"}' --as user > feishu_tasks.json
python scripts\feishu_dedup.py
```
读 `dedup_result.json`，`is_duplicate=true` 的通知跳过。

### 3. 写飞书日历（防张冠李戴）
对每条 `is_duplicate=false` 且有事件时间的通知：
```powershell
# 用脚本自动生成描述（不要手动复制原文！）
python scripts\daily_pipeline.py --desc <通知ID>
# 创建事件
lark-cli calendar events create --summary "<标题>" --start "<开始>" --end "<结束>" --location "<地点>" --description "<脚本输出>"
# 校验
python scripts\daily_pipeline.py --verify <通知ID> "<刚创建的描述>"
# 提醒设为提前30分钟
lark-cli calendar events patch --params '{"calendar_id":"primary","event_id":"<事件ID>"}' --data '{"reminders":[{"minutes":30}]}' --as user
```

### 4. 写飞书任务
对每条有截止日期的通知：
```powershell
python scripts\daily_pipeline.py --task-desc <通知ID>
lark-cli task +create --summary "<标题>" --due "<截止>" --tasklist-guid "<清单GUID>" --description "<脚本输出>"
# 分配给自己
lark-cli task +assign --task-id "<任务GUID>" --add "<你的open_id>"
```

### 5. 生成汇总报告并上传飞书
写 `每日通知汇总_YYYYMMDD.md`，按优先级排序，含：紧急通知、未来3天截止项、冷处理变化、去重记录。然后：
```powershell
lark-cli drive +upload --file "每日通知汇总_YYYYMMDD.md" --name "每日通知汇总_YYYYMMDD.md"
```

## 关键规则
- **绝不手动复制通知原文**，一律用 `--desc` / `--task-desc` 脚本生成
- **时间理解**：以通知发布时间为基准算"明天/本周X"，过滤超7天旧通知
- **自动去重**：发现重复日历/任务直接删除
- 脚本运行完会自动关闭 QCE 进程
