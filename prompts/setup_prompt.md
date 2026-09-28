# 搭建提示词（直接发给 AI）

> 把下面整段发给一个具备工具调用能力的 AI（如豆包），它会自动帮你探测环境、安装缺失组件、配置、试跑。

```
【任务】请在这台电脑上搭建一套「QQ 群通知自动化 → 飞书日历/任务」工作环境，并完成首次试跑。

你是自主环境搭建工程师：先探测、后安装、逐项验证，缺什么自动下载安装。

## 第一步：环境自检
逐项检查并列出【已有/缺失/版本】：
1. Python：python --version / py --version
2. lark-cli：lark-cli --version
3. QCE：端口 40653 是否监听、安装目录
4. NapCatQQ 进程/目录
5. curl 是否可用

## 第二步：自动安装缺失项
1. Python 缺失：winget install Python.Python.3.12，或从 python.org 下载（装用户目录，勾选 Add to PATH）
2. lark-cli 缺失：按官方文档安装
3. QCE 缺失：从 GitHub Releases 下载 Windows x64 版，解压到 D:\QCE，运行 launcher-user.bat
4. NapCat 缺失：下载与 QQ 版本匹配的版本

## 第三步：登录与群勾选（需用户一次性配合）
1. 提示用户用本人 QQ 扫码登录 NapCatQQ（仅首次）
2. 登录后读取 %USERPROFILE%\.qq-chat-exporter\security.json 的 accessToken
3. 提示用户扫码登录 lark-cli
4. 调用 QCE API GET http://localhost:40653/api/groups 拉取全部群，列成清单让用户勾选要监控的群
5. 把勾选结果写入 monitored_groups.json（群名+群号+分类），回显给用户确认

## 第四步：放置脚本
1. 建工作目录 D:\notify-work
2. 把部署包里的 scripts/daily_pipeline.py、feishu_dedup.py、error_monitor.py 复制到工作目录
3. 在飞书创建任务清单，记录 GUID

## 第五步：试跑
运行 python daily_pipeline.py，确认导出成功、通知被识别。
然后按下面的「每日执行流程」把通知写进飞书日历和任务，生成汇总报告上传飞书。

## 每日执行流程（定时任务用这段）

定时触发。AI 只做：跑脚本、拉去重数据、写日历任务、出报告。

⚠️ 绝对禁止手动复制通知原文到日历描述！一律用脚本生成：
1. python daily_pipeline.py --list-notices 列通知
2. python daily_pipeline.py --desc <ID> 生成日历描述
3. python daily_pipeline.py --task-desc <ID> 生成任务描述
4. 创建后 python daily_pipeline.py --verify <ID> "<描述>" 校验，失败删除重建

流程：
1. python daily_pipeline.py（自动检测并重启QCE，跑完自动关闭）
2. 读 _pipeline_summary.json 里的 analysis.notices
3. lark-cli 拉日历和任务列表，跑 feishu_dedup.py 去重
4. 对不重复的通知，用脚本生成描述 → lark-cli 建日历事件（提醒提前30分钟）→ 校验
5. 对有截止日期的，lark-cli 建任务 → assign 给当前用户
6. 生成 每日通知汇总_YYYYMMDD.md，按优先级排序，上传飞书云空间

关键规则：
- 时间以通知发布时间为基准算"明天/本周X"，过滤超7天旧通知
- 发现重复日历/任务自动删除
- 断点续传+冷处理已内置于脚本
```
