#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_dedup.py - 飞书日历/任务自动去重脚本（优化2）
用法: python feishu_dedup.py
无参数，自动读取同目录JSON文件：
  - _pipeline_summary.json  (分析结果，含 analysis.notices)
  - calendar_events.json    (AI用lark-calendar拉的未来7天事件)
  - feishu_tasks.json       (AI用lark-task拉的现有任务)
输出: dedup_result.json
算法：标题文本归一化后取前30字 Jaccard相似度>0.6 + 时间差<24小时 → 判定为重复
完全算法化，不需要AI判断。
"""
import json, os, sys, re, datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORKDIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = os.path.join(WORKDIR, "_pipeline_summary.json")
CALENDAR_FILE = os.path.join(WORKDIR, "calendar_events.json")
TASKS_FILE = os.path.join(WORKDIR, "feishu_tasks.json")
OUTPUT_FILE = os.path.join(WORKDIR, "dedup_result.json")

CST = datetime.timezone(datetime.timedelta(hours=8))

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] 读取 {path} 失败: {e}")
        return default

def normalize_title(text):
    """归一化标题：去空白和标点，取前30字。"""
    t = re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE)
    return t[:30]

def bigrams(text):
    """中文bigram集合。"""
    if len(text) < 2:
        return set()
    return set(text[i:i+2] for i in range(len(text) - 1))

def jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)

def parse_iso(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        return None

def time_diff_hours(dt_a, dt_b):
    """计算两个datetime的小时差绝对值。"""
    if not dt_a or not dt_b:
        return None
    # 处理时区naive/aware混合
    if dt_a.tzinfo is None and dt_b.tzinfo is not None:
        dt_a = dt_a.replace(tzinfo=CST)
    elif dt_b.tzinfo is None and dt_a.tzinfo is not None:
        dt_b = dt_b.replace(tzinfo=CST)
    return abs((dt_a - dt_b).total_seconds()) / 3600.0

def extract_event_time(event):
    """从日历事件/任务中提取时间datetime。"""
    # 日历事件: start_time / start
    t_str = None
    if isinstance(event, dict):
        t_str = (event.get("start_time") or event.get("start") or
                 event.get("event_time") or event.get("deadline") or
                 event.get("due") or event.get("due_time") or "")
    return parse_iso(t_str) if isinstance(t_str, str) else None

def extract_event_title(event):
    """从日历事件/任务中提取标题。"""
    if isinstance(event, dict):
        return (event.get("summary") or event.get("title") or
                event.get("name") or event.get("text") or "")
    return ""

def extract_event_id(event):
    """从日历事件/任务中提取ID。"""
    if isinstance(event, dict):
        return (event.get("event_id") or event.get("id") or
                event.get("guid") or event.get("task_id") or "")
    return ""

def check_duplicate(notice, calendar_events, feishu_tasks):
    """检查一条notice是否与现有日历事件或飞书任务重复。
    返回 (is_duplicate, duplicate_with, reason)。
    """
    notice_title = normalize_title(notice.get("text", ""))[:30]
    notice_bigrams = bigrams(notice_title)
    notice_time = (parse_iso(notice.get("event_time")) or
                   parse_iso(notice.get("deadline")))

    candidates = []
    for ev in calendar_events:
        candidates.append(("calendar", ev))
    for tk in feishu_tasks:
        candidates.append(("task", tk))

    for src_type, item in candidates:
        ev_title = normalize_title(extract_event_title(item))[:30]
        ev_bigrams = bigrams(ev_title)
        sim = jaccard(notice_bigrams, ev_bigrams)

        if sim <= 0.6:
            continue

        # 文本相似度够，检查时间
        ev_time = extract_event_time(item)
        if notice_time and ev_time:
            diff_h = time_diff_hours(notice_time, ev_time)
            if diff_h is not None and diff_h > 24:
                continue  # 时间差超过24小时，不算重复
        # 如果notice无时间但事件有时间，仍按文本相似度判定
        ev_id = extract_event_id(item)
        dup_id = f"{src_type}:{ev_id}"
        return True, dup_id, f"similarity={sim:.2f}, time_diff={time_diff_hours(notice_time, ev_time):.1f}h" if (notice_time and ev_time) else f"similarity={sim:.2f}, no_time_check"

    return False, "", ""

def main():
    print("=" * 50)
    print("飞书日历/任务去重脚本启动")

    # 1. 读取通知
    summary = load_json(SUMMARY_FILE)
    if not summary:
        print("  [错误] 找不到 _pipeline_summary.json")
        sys.exit(1)

    notices = summary.get("analysis", {}).get("notices", [])
    print(f"  读取通知: {len(notices)} 条")

    # 2. 读取现有日历事件和飞书任务
    calendar_events = load_json(CALENDAR_FILE, [])
    feishu_tasks = load_json(TASKS_FILE, [])

    # 兼容不同JSON结构
    if isinstance(calendar_events, dict):
        calendar_events = calendar_events.get("events", calendar_events.get("items", []))
    if isinstance(feishu_tasks, dict):
        feishu_tasks = feishu_tasks.get("tasks", feishu_tasks.get("items", []))

    has_existing_data = bool(calendar_events or feishu_tasks)
    print(f"  日历事件: {len(calendar_events)} 条, 飞书任务: {len(feishu_tasks)} 条")

    # 3. 去重检测
    results = []
    dup_count = 0
    for notice in notices:
        nid = notice.get("id", "")
        if not has_existing_data:
            result = {
                "notice_id": nid,
                "text_preview": notice.get("text", "")[:80],
                "is_duplicate": False,
                "duplicate_with": "",
                "reason": "no_existing_data",
            }
        else:
            is_dup, dup_with, reason = check_duplicate(notice, calendar_events, feishu_tasks)
            if is_dup:
                dup_count += 1
            result = {
                "notice_id": nid,
                "text_preview": notice.get("text", "")[:80],
                "is_duplicate": is_dup,
                "duplicate_with": dup_with,
                "reason": reason,
            }
        results.append(result)

    # 4. 输出
    output = {
        "dedup_at": datetime.datetime.now(CST).isoformat(),
        "total_notices": len(notices),
        "duplicate_count": dup_count if has_existing_data else 0,
        "has_existing_data": has_existing_data,
        "calendar_count": len(calendar_events),
        "task_count": len(feishu_tasks),
        "results": results,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  去重完成: {len(notices)} 条通知, 重复 {dup_count} 条" if has_existing_data
          else f"  无现有数据，全部标记为不重复")
    print(f"  输出: {OUTPUT_FILE}")

    # 控制台摘要
    for r in results:
        if r["is_duplicate"]:
            print(f"    [DUP] {r['notice_id'][:12]}... -> {r['duplicate_with']} ({r['reason']})")

if __name__ == "__main__":
    main()
