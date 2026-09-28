#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
error_monitor.py - 错误监控与告警模块（优化6）
功能：
  - send_alert(level, title, detail, suggestion)：飞书webhook告警，无webhook则只写本地
  - record_failure(component, error, suggestion)：记录失败到 _error_log.json
  - check_consecutive_failures(component)：计算某组件连续失败次数
  - 连续失败>=3次自动升级为critical级别
用法: from error_monitor import send_alert, record_failure, check_consecutive_failures
"""
import json, os, time, datetime, urllib.request, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKDIR = os.path.dirname(os.path.abspath(__file__))
ERROR_LOG = os.path.join(WORKDIR, "_error_log.json")

CST = datetime.timezone(datetime.timedelta(hours=8))

def _now_iso():
    return datetime.datetime.now(CST).isoformat()

def _load_error_log():
    default = {"failures": [], "last_alert_at": "",
               "consecutive": {"export": 0, "analysis": 0, "overall": 0}}
    if not os.path.exists(ERROR_LOG):
        return default
    try:
        with open(ERROR_LOG, "r", encoding="utf-8") as f:
            d = json.load(f)
        # 确保结构完整
        for k, v in default.items():
            if k not in d:
                d[k] = v
        if "consecutive" not in d:
            d["consecutive"] = {}
        for k in ("export", "analysis", "overall"):
            d["consecutive"].setdefault(k, 0)
        return d
    except Exception:
        return default

def _save_error_log(d):
    with open(ERROR_LOG, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _component_category(component):
    """从组件名推断大类：export:xxx -> export, analysis -> analysis, overall -> overall"""
    if component.startswith("export:"):
        return "export"
    for cat in ("export", "analysis", "overall"):
        if component.startswith(cat):
            return cat
    return "overall"

def check_consecutive_failures(component):
    """读取 _error_log.json 计算某组件的连续失败次数。
    连续 = 最近 N 次 failures 中该 component 连续失败（中间无成功记录）。
    简化实现：按大类统计最近连续失败数。
    """
    log = _load_error_log()
    cat = _component_category(component)
    # 返回大类的连续计数
    return log.get("consecutive", {}).get(cat, 0)

def record_failure(component, error, suggestion=""):
    """记录一次失败。自动更新连续失败计数。"""
    log = _load_error_log()
    entry = {
        "time": _now_iso(),
        "component": component,
        "error": str(error)[:500],
        "suggestion": suggestion,
    }
    log["failures"].append(entry)
    # 只保留最近100条
    if len(log["failures"]) > 100:
        log["failures"] = log["failures"][-100:]

    cat = _component_category(component)
    log["consecutive"][cat] = log["consecutive"].get(cat, 0) + 1
    log["consecutive"]["overall"] = log["consecutive"].get("overall", 0) + 1
    _save_error_log(log)

    # 连续失败>=3次时自动升级告警
    consec = log["consecutive"][cat]
    level = "critical" if consec >= 3 else "warning"
    print(f"  [ERROR_MONITOR] 记录失败: {component} (连续{consec}次, level={level})", flush=True)
    if consec >= 3:
        send_alert("critical", f"连续失败告警: {component}",
                   f"{component} 已连续失败 {consec} 次。错误: {str(error)[:200]}",
                   suggestion)
    return consec

def record_success(component):
    """记录一次成功，重置连续失败计数。"""
    log = _load_error_log()
    cat = _component_category(component)
    if log["consecutive"].get(cat, 0) > 0:
        print(f"  [ERROR_MONITOR] 恢复: {cat} (连续失败已清零)", flush=True)
    log["consecutive"][cat] = 0
    # overall 不清零，只在整体重启时清
    _save_error_log(log)

def send_alert(level, title, detail, suggestion=""):
    """通过飞书自定义机器人webhook发送告警。
    无 FEISHU_WEBHOOK_URL 环境变量时只写本地日志。
    level: info / warning / critical
    """
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
    log = _load_error_log()

    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
    msg = (f"{emoji} [{level.upper()}] {title}\n"
           f"时间: {_now_iso()}\n"
           f"详情: {str(detail)[:300]}\n"
           f"建议: {suggestion}" if suggestion else
           f"{emoji} [{level.upper()}] {title}\n时间: {_now_iso()}\n详情: {str(detail)[:300]}")

    # 写本地日志
    log["last_alert_at"] = _now_iso()
    log["last_alert"] = {"level": level, "title": title, "detail": str(detail)[:300],
                         "suggestion": suggestion}
    _save_error_log(log)

    if not webhook:
        print(f"  [ALERT-{level}] {title}: {str(detail)[:100]} (无webhook, 仅本地记录)", flush=True)
        return

    # 发送飞书webhook
    try:
        body = {
            "msg_type": "text",
            "content": {"text": msg}
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(webhook, data=data,
                                      headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code", 0) != 0:
                print(f"  [ALERT] webhook返回错误: {result}", flush=True)
            else:
                print(f"  [ALERT-{level}] 已发送飞书告警: {title}", flush=True)
    except Exception as e:
        print(f"  [ALERT] 发送失败: {e}", flush=True)

if __name__ == "__main__":
    # 自测
    print("=== error_monitor 自测 ===")
    # 测试 record_failure
    record_failure("export:12345", "测试错误: 连接超时", "检查QCE服务")
    record_failure("export:12345", "测试错误: 再次超时", "检查网络")
    c = check_consecutive_failures("export:12345")
    print(f"  连续失败次数: {c}")
    # 第三次应触发 critical
    record_failure("export:12345", "第三次失败", "重启服务")
    c = check_consecutive_failures("export:12345")
    print(f"  连续失败次数(第三次后): {c}")
    # 恢复
    record_success("export:12345")
    c = check_consecutive_failures("export:12345")
    print(f"  恢复后连续失败: {c}")
    print("自测完成，检查 _error_log.json")
