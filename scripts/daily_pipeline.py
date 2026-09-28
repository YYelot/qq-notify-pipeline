#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日QQ群通知汇总 - 主管道脚本 v2
整合：动态群列表 + 真正断点续传 + 已处理通知去重 + 冷处理名单 + 中文时间解析 + 结构化输出
用法: python daily_pipeline.py
只做导出+分析+状态管理，不调用飞书API（飞书日历/任务由AI在定时任务中执行）。
"""
import json, time, urllib.request, urllib.error, sys, os, re, hashlib, datetime, glob, shutil, subprocess, urllib.parse

def _safe_url(url):
    return urllib.parse.quote(url, safe="/:%@?&=+#~[](),;_")

from concurrent.futures import ThreadPoolExecutor, as_completed

# 错误监控模块（优化6）
try:
    from error_monitor import send_alert, record_failure, record_success, check_consecutive_failures
    ERROR_MONITOR_OK = True
except ImportError:
    ERROR_MONITOR_OK = False
    def record_failure(*a, **kw): pass
    def record_success(*a, **kw): pass
    def send_alert(*a, **kw): pass
    def check_consecutive_failures(*a, **kw): return 0

# OCR 可选依赖检测（优化4）
try:
    import pytesseract
    from PIL import Image as _PILImage
    OCR_AVAILABLE = True
except ImportError:
    pytesseract = None
    _PILImage = None
    OCR_AVAILABLE = False

# ============ Windows UTF-8 stdout ============
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============ 配置 ============
# QCE access token 自动从标准位置读取（不再硬编码）：
#   1) 优先读取 ~/.qq-chat-exporter/security.json 里的 accessToken
#   2) 兜底读取环境变量 QCE_ACCESS_TOKEN
def _load_qce_token():
    try:
        _p = os.path.join(os.path.expanduser("~"), ".qq-chat-exporter", "security.json")
        with open(_p, "r", encoding="utf-8") as _f:
            return json.load(_f).get("accessToken", "")
    except Exception:
        return os.environ.get("QCE_ACCESS_TOKEN", "")
TOKEN = _load_qce_token()
BASE = os.environ.get("QCE_BASE_URL", "http://localhost:40653")
BASE = "http://localhost:40653"
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
WORKDIR = os.path.dirname(os.path.abspath(__file__))
EXPORTS_DIR = os.path.join(WORKDIR, "exports")
# QCE 导出原始目录：从环境变量 QCE_EXPORTS_SRC 读取；未设置或目录不可用时，脚本自动回退走 HTTP downloadUrl 下载
SOURCE_EXPORTS_DIR = os.environ.get("QCE_EXPORTS_SRC", os.path.join(os.path.expanduser("~"), "QQChatExporter", "exports"))
MONITORED_FILE = os.path.join(WORKDIR, "monitored_groups.json")
EXPORT_STATE_FILE = os.path.join(WORKDIR, "export_state.json")
PROCESSED_FILE = os.path.join(WORKDIR, "processed_notifications.json")
COLD_FILE = os.path.join(WORKDIR, "cold_groups.json")
PIPELINE_SUMMARY = os.path.join(WORKDIR, "_pipeline_summary.json")
NOTICES_OUT = os.path.join(EXPORTS_DIR, "_notices.json")
EXPORT_RESULT_OUT = os.path.join(EXPORTS_DIR, "_export_result.json")

CST = datetime.timezone(datetime.timedelta(hours=8))
OVERLAP_MS = 12 * 3600 * 1000          # 重叠12小时防遗漏
FIRST_EXPORT_DAYS = 7                   # 首次导出最近7天
COLD_AFTER_DAYS = 7                    # 连续7天无消息进入冷处理
KEEP_PROCESSED_DAYS = 90               # 保留90天已处理记录

os.makedirs(EXPORTS_DIR, exist_ok=True)

# ============ 工具函数 ============
def log(msg):
    print(msg, flush=True)

def req(method, path, body=None, timeout=30):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, headers=HDR, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"  [WARN] 读取 {path} 失败: {e}")
    return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def now_cst():
    return datetime.datetime.now(CST)

# ============ 1. 状态文件：export_state ============
def load_export_state():
    return load_json(EXPORT_STATE_FILE, {})

def save_export_state(s):
    save_json(EXPORT_STATE_FILE, s)

# ============ 2. 状态文件：processed_notifications ============
def load_processed():
    d = load_json(PROCESSED_FILE, {"notifications": []})
    if "notifications" not in d:
        d = {"notifications": []}
    return d

def save_processed(d):
    # 清理90天前记录
    cutoff = (now_cst().date() - datetime.timedelta(days=KEEP_PROCESSED_DAYS)).isoformat()
    d["notifications"] = [n for n in d.get("notifications", []) if n.get("processed_at", "9999") >= cutoff]
    save_json(PROCESSED_FILE, d)

def normalize_text(text):
    # 去除空白和标点，取前200字
    t = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return t[:200]

def make_notice_id(group_code, text):
    norm = normalize_text(text)
    return hashlib.sha256((str(group_code) + norm).encode("utf-8")).hexdigest()[:16]

# ============ 3. 状态文件：cold_groups ============
def load_cold():
    return load_json(COLD_FILE, {})

def save_cold(c):
    save_json(COLD_FILE, c)

# ============ 4. 中文时间解析 ============
WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
PERIOD_HOUR = {"凌晨": 6, "早上": 7, "早晨": 7, "清晨": 7, "上午": 9, "中午": 12,
               "午": 12, "下午": 15, "傍晚": 18, "晚上": 19, "晚": 19, "夜间": 19}

def extract_tod(text):
    """提取一天中的时间 (hour, minute, period_str)。返回 (hour, minute, period) 或 None。"""
    hour = minute = None
    period = None
    # 时间段
    for p in ["凌晨", "早上", "早晨", "清晨", "上午", "中午", "傍晚", "晚上", "夜间"]:
        if p in text:
            period = p
            break
    if period is None and "下午" in text:
        period = "下午"
    if period is None and re.search(r"(?<![上下])午[后饭]?", text):
        period = "中午"
    # 数字时间: 3点 / 10:00 / 8：30 / 17点 / 19时30分 / 3点半
    m = re.search(r"(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*分?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) is not None else 0
        if hour > 23:
            hour = None
    # 下午/晚上 的12小时制修正
    if hour is not None and period in ("下午", "傍晚", "晚上", "夜间") and hour < 12:
        hour += 12
    if hour is None and period is not None:
        hour = PERIOD_HOUR.get(period, 9)
        minute = 0
    if hour is None:
        return None
    if minute is None:
        minute = 0
    return hour, minute, period

def parse_chinese_time(text, reference_dt):
    """解析中文时间表述，返回 CST datetime 或 None。
    reference_dt: 基准时间(CST datetime)。周一为一周开始。"""
    if not text:
        return None
    ref_date = reference_dt.date()
    monday = ref_date - datetime.timedelta(days=ref_date.weekday())  # 本周周一
    target_date = None

    # --- 周X ---
    m = re.search(r"(下一周|下周|本周|这周|这周|这)([一二三四五六日天])", text)
    if m:
        prefix, ch = m.group(1), m.group(2)
        wd = WEEKDAY_MAP[ch]
        if "下" in prefix:
            target_date = (monday + datetime.timedelta(days=7 + wd))
        else:
            target_date = (monday + datetime.timedelta(days=wd))
    else:
        m = re.search(r"(?<![周本上下这])(周[一二三四五六日天])", text)
        if m:
            wd = WEEKDAY_MAP[m.group(1)[1]]
            candidate = monday + datetime.timedelta(days=wd)
            # 裸"周X"：若本周该日已过(今天之前)，则取下一周
            if candidate < ref_date:
                candidate += datetime.timedelta(days=7)
            target_date = candidate

    # --- 明天/后天/今天 ---
    if target_date is None:
        if "明天" in text or "明日" in text:
            target_date = ref_date + datetime.timedelta(days=1)
        elif "后天" in text:
            target_date = ref_date + datetime.timedelta(days=2)
        elif "今天" in text or "今日" in text:
            target_date = ref_date

    # --- 具体日期: 2026年10月1日 / 9月25日 / 9.28 / 10月30日 ---
    if target_date is None:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                target_date = datetime.date(y, mo, d)
            except ValueError:
                target_date = None
        else:
            m = re.search(r"(?<![\d/])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
            if m:
                mo, d = int(m.group(1)), int(m.group(2))
                y = ref_date.year
                try:
                    cand = datetime.date(y, mo, d)
                    # 距今超过60天且在过去 -> 视作明年
                    if cand < ref_date - datetime.timedelta(days=60):
                        cand = datetime.date(y + 1, mo, d)
                    target_date = cand
                except ValueError:
                    target_date = None
            else:
                m = re.search(r"(?<![\d/第])(\d{1,2})\s*[.．/\-]\s*(\d{1,2})(?!\d)", text)
                if m:
                    mo, d = int(m.group(1)), int(m.group(2))
                    if 1 <= mo <= 12 and 1 <= d <= 31:
                        y = ref_date.year
                        try:
                            cand = datetime.date(y, mo, d)
                            if cand < ref_date - datetime.timedelta(days=60):
                                cand = datetime.date(y + 1, mo, d)
                            # 距基准超过365天则视为非日期（避免"第4-6周"误判）
                            if abs((cand - ref_date).days) <= 365:
                                target_date = cand
                        except ValueError:
                            target_date = None

    # --- 月底 / 周末 ---
    if target_date is None:
        if "月底" in text or "月末" in text:
            if ref_date.month == 12:
                next_m = datetime.date(ref_date.year + 1, 1, 1)
            else:
                next_m = datetime.date(ref_date.year, ref_date.month + 1, 1)
            target_date = next_m - datetime.timedelta(days=1)
        elif "下周末" in text:
            target_date = monday + datetime.timedelta(days=7 + 5)  # 下周六
        elif "周末" in text:
            target_date = monday + datetime.timedelta(days=5)      # 本周六

    if target_date is None:
        return None

    # --- 一天中的时间 ---
    tod = extract_tod(text)
    if tod is not None:
        hour, minute, _ = tod
    else:
        hour, minute = 9, 0
    try:
        return datetime.datetime(target_date.year, target_date.month, target_date.day,
                                  hour, minute, tzinfo=CST)
    except ValueError:
        return None

# ============ 5. 动态群列表 ============
def load_monitored_config():
    cfg = load_json(MONITORED_FILE, {"categories": {}, "exclude": [],
                                     "new_group_keywords": [], "new_group_min_members": 10})
    groups = {}
    for cat, items in cfg.get("categories", {}).items():
        for it in items:
            groups[it["code"]] = {"name": it["name"], "category": cat}
    exclude = {e["code"]: e for e in cfg.get("exclude", [])}
    return cfg, groups, exclude

def fetch_api_groups():
    """GET /api/groups -> {group_code: {name, member_count}}"""
    try:
        d = req("GET", "/api/groups", timeout=15)
        out = {}
        for g in d.get("data", {}).get("groups", []):
            code = str(g.get("groupCode", ""))
            if code:
                out[code] = {"name": g.get("groupName", ""), "member_count": g.get("memberCount", 0)}
        return out, None
    except Exception as e:
        return {}, str(e)

def detect_new_groups(api_groups, monitored, exclude):
    """发现潜在新群（关键词+人数），返回列表，不自动加入。"""
    cfg, _, _ = load_monitored_config()
    kws = cfg.get("new_group_keywords", [])
    min_m = cfg.get("new_group_min_members", 10)
    candidates = []
    for code, info in api_groups.items():
        if code in monitored or code in exclude:
            continue
        if info["member_count"] < min_m:
            continue
        if any(kw in info["name"] for kw in kws):
            candidates.append({"code": code, "name": info["name"], "member_count": info["member_count"]})
    return candidates

# ============ 6. 初始化：从现有导出文件推断状态 ============
def init_state_from_existing(monitored):
    """扫描 exports/group_*.json，为每个监控群初始化 export_state 和 cold_groups。"""
    state = load_export_state()
    cold = load_cold()
    existing_files = glob.glob(os.path.join(EXPORTS_DIR, "group_*.json"))
    # code -> latest file
    latest = {}
    for f in existing_files:
        bn = os.path.basename(f)
        m = re.search(r"group_.+?_(\d{6,})_\d{8}_\d+\.json$", bn)
        if m:
            code = m.group(1)
            if code not in latest or os.path.getmtime(f) > os.path.getmtime(latest[code]):
                latest[code] = f
    for code, info in monitored.items():
        if code in state:
            continue  # 已有记录
        f = latest.get(code)
        max_ts = None
        msg_count = 0
        last_file = os.path.basename(f) if f else ""
        if f and os.path.exists(f):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                msgs = data.get("messages", [])
                msg_count = len(msgs)
                tss = [m.get("timestamp") for m in msgs if isinstance(m.get("timestamp"), (int, float))]
                if tss:
                    max_ts = int(max(tss))
            except Exception as e:
                log(f"  [WARN] 初始化读取 {f} 失败: {e}")
        # 无消息时用文件修改时间兜底，表示"已检查到该时间点，无新消息"
        mtime_ms = int(os.path.getmtime(f) * 1000) if f and os.path.exists(f) else int(time.time() * 1000)
        lms_val = max_ts if max_ts else mtime_ms
        act_val = max_ts if max_ts else mtime_ms
        state[code] = {
            "last_export_ms": lms_val,
            "last_export_at": datetime.datetime.fromtimestamp(os.path.getmtime(f), CST).isoformat() if f else None,
            "message_count": msg_count,
            "last_file": last_file,
        }
        if code not in cold:
            cold[code] = {
                "group_name": info["name"],
                "last_activity_ms": act_val,
                "cold_since": None,
                "status": "active",
            }
    save_export_state(state)
    save_cold(cold)
    return state, cold

# ============ 7. 导出 ============
def submit_export(code, start_ms, end_ms):
    body = {
        "peer": {"chatType": 2, "peerUid": code},
        "format": "JSON",
        "filter": {"startTime": start_ms, "endTime": end_ms},
    }
    r = req("POST", "/api/messages/export", body, timeout=30)
    d = r["data"]
    return d["taskId"], d.get("fileName", "")

def poll_tasks(task_map, rounds=60, interval=5):
    """task_map: {code: taskId} -> {code: {status, messageCount, fileName}}"""
    results = {code: {"status": "unknown", "messageCount": 0, "fileName": ""} for code in task_map}
    for rnd in range(rounds):
        time.sleep(interval)
        try:
            tasks = req("GET", "/api/tasks", timeout=30)["data"]["tasks"]
        except Exception as e:
            log(f"  poll error: {e}")
            continue
        by_id = {t["taskId"]: t for t in tasks}
        done = running = 0
        for code, tid in task_map.items():
            t = by_id.get(tid)
            if not t:
                continue
            results[code]["messageCount"] = t.get("messageCount", 0)
            results[code]["fileName"] = t.get("fileName", results[code]["fileName"])
            results[code]["downloadUrl"] = t.get("downloadUrl", "")
            if t["status"] == "completed":
                results[code]["status"] = "completed"
                done += 1
            elif t["status"] == "failed":
                results[code]["status"] = "failed"
                done += 1
            else:
                running += 1
        log(f"  round {rnd}: completed={done} running={running}")
        if running == 0:
            break
    return results

def copy_completed_files(polled):
    """把 QCE 导出完成的文件从 G: 源目录复制到本地 EXPORTS_DIR。返回 {code: local_filename}"""
    copied = {}
    for code, p in polled.items():
        if p.get("status") != "completed" or not p.get("fileName"):
            continue
        fname = p["fileName"]
        src = os.path.join(SOURCE_EXPORTS_DIR, fname)
        dst = os.path.join(EXPORTS_DIR, fname)
        try:
            if not os.path.exists(src):
                # 回退：按 code 找源目录最新文件
                cands = glob.glob(os.path.join(SOURCE_EXPORTS_DIR, f"group_*_{code}_*.json"))
                if cands:
                    src = max(cands, key=os.path.getmtime)
                    fname = os.path.basename(src)
                    dst = os.path.join(EXPORTS_DIR, fname)
            if os.path.exists(src):
                shutil.copy2(src, dst)
                copied[code] = fname
            else:
                # Fix5: G盘不可用时从 HTTP downloadUrl 下载
                dl = p.get("downloadUrl", "")
                if dl:
                    try:
                        if dl.startswith("/"):
                            dl = BASE + dl
                        dl = _safe_url(dl)
                        req2 = urllib.request.Request(dl, headers={"Authorization": f"Bearer {TOKEN}"})
                        with urllib.request.urlopen(req2, timeout=30) as resp:
                            with open(dst, "wb") as out:
                                out.write(resp.read())
                        copied[code] = fname
                        log(f"  [HTTP下载] {code} <- {fname}")
                    except Exception as de:
                        log(f"  [WARN] HTTP下载 {code} 失败: {de}")
                else:
                    log(f"  [WARN] 源文件不存在且无downloadUrl: {src}")
        except Exception as e:
            log(f"  [WARN] 复制 {code} 失败: {e}")
    return copied

def find_latest_export_file(code):
    """导出完成后，在 exports 目录找到该群最新的导出文件。"""
    files = glob.glob(os.path.join(EXPORTS_DIR, f"group_*_{code}_*.json"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def run_exports(monitored, state, cold, api_groups):
    """决定每个群的导出窗口并执行导出。返回 (rows, changes)。"""
    now_ms = int(time.time() * 1000)
    today_monday = now_cst().weekday() == 0  # 周一
    rows = []
    changes = []  # 冷处理变化

    to_export = {}   # code -> start_ms
    skipped_cold = []
    for code, info in monitored.items():
        cstate = cold.get(code, {"status": "active", "cold_since": None, "last_activity_ms": None})
        status = cstate.get("status", "active")
        if status == "cold" and not today_monday:
            skipped_cold.append(code)
            rows.append({"code": code, "name": info["name"], "status": "cold_skipped",
                         "messageCount": 0, "error": "", "window": None})
            continue
        rec = state.get(code, {})
        last_ms = rec.get("last_export_ms")
        if last_ms:
            start_ms = int(last_ms - OVERLAP_MS)
        else:
            start_ms = now_ms - FIRST_EXPORT_DAYS * 86400 * 1000
        to_export[code] = start_ms
        log(f"  [计划] {code} {info['name']} status={status} window={ms_to_cst(start_ms)} ~ {ms_to_cst(now_ms)}")

    if skipped_cold:
        log(f"  冷处理群(非周一跳过): {len(skipped_cold)} 个")

    # 提交任务（并行优化5: ThreadPoolExecutor max_workers=5）
    export_start = time.time()
    task_map = {}
    submit_errors = []
    def _submit_one(code, start_ms):
        time.sleep(0.3)  # 线程内防限流
        try:
            tid, fname = submit_export(code, int(start_ms), now_ms)
            return code, tid, None
        except Exception as e:
            return code, None, str(e)
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_submit_one, code, start_ms): code
                   for code, start_ms in to_export.items()}
        for fut in as_completed(futures):
            code, tid, err = fut.result()
            if err:
                log(f"  [失败] {code}: {err}")
                submit_errors.append(code)
                record_failure("export:" + code, err, "重试或检查群状态")
                rows.append({"code": code, "name": monitored[code]["name"], "status": "submit_failed",
                             "messageCount": 0, "error": err, "window": None})
            else:
                task_map[code] = tid
                log(f"  [提交] {code} taskId={tid}")

    polled = poll_tasks(task_map) if task_map else {}
    export_duration_sec = round(time.time() - export_start, 1)
    log(f"  导出阶段耗时: {export_duration_sec}s (并行workers=5)")
    # 复制导出文件到本地 exports/
    copied = copy_completed_files(polled)
    if copied:
        log(f"  复制导出文件到本地: {len(copied)} 个")

    # 汇总 + 更新状态
    for code, info in monitored.items():
        if code in skipped_cold or code not in to_export:
            if code in [r["code"] for r in rows]:
                continue
        rec = state.get(code, {})
        p = polled.get(code, {"status": "not_submitted", "messageCount": 0, "fileName": ""})
        latest_file = None
        if code in copied:
            latest_file = os.path.join(EXPORTS_DIR, copied[code])
        elif p["status"] == "completed":
            latest_file = find_latest_export_file(code)
        if not latest_file and rec.get("last_file"):
            latest_file = os.path.join(EXPORTS_DIR, rec["last_file"])
        max_ts = rec.get("last_export_ms")
        if p["status"] == "completed" and latest_file and os.path.exists(latest_file):
            try:
                with open(latest_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                msgs = data.get("messages", [])
                tss = [m.get("timestamp") for m in msgs if isinstance(m.get("timestamp"), (int, float))]
                if tss:
                    max_ts = int(max(tss))
            except Exception as e:
                log(f"  [WARN] 读取导出文件 {latest_file} 失败: {e}")
        # Fix1: 无消息时 last_export_ms 兜底为本次导出结束时间 now_ms
        if not max_ts:
            max_ts = now_ms
        # 更新 export_state
        state[code] = {
            "last_export_ms": max_ts,
            "last_export_at": now_cst().isoformat(),
            "message_count": p["messageCount"],
            "last_file": os.path.basename(latest_file) if latest_file else rec.get("last_file", ""),
        }
        # 更新 cold_groups
        cstate = cold.get(code, {"group_name": info["name"], "last_activity_ms": None,
                                 "cold_since": None, "status": "active"})
        had_new = p["messageCount"] > 0
        prev_status = cstate.get("status", "active")
        if had_new:
            if max_ts:
                cstate["last_activity_ms"] = max_ts
            cstate["status"] = "active"
            cstate["cold_since"] = None
            if prev_status == "cold":
                changes.append({"code": code, "name": info["name"], "change": "cold->active"})
                log(f"  [冷处理恢复] {code} {info['name']} 有新消息，恢复为active")
        else:
            # 无新消息：判断是否满7天
            last_act = cstate.get("last_activity_ms")
            if last_act:
                days_idle = (now_ms - int(last_act)) / 86400000.0
                if days_idle >= COLD_AFTER_DAYS and cstate.get("status") != "cold":
                    cstate["status"] = "cold"
                    cstate["cold_since"] = now_cst().date().isoformat()
                    changes.append({"code": code, "name": info["name"], "change": "active->cold",
                                    "idle_days": round(days_idle, 1)})
                    log(f"  [进入冷处理] {code} {info['name']} 已{round(days_idle,1)}天无消息")
            # 无新消息且之前是cold，保持cold（周一才检测）
        cold[code] = cstate

        rows.append({"code": code, "name": info["name"], "status": p["status"],
                     "messageCount": p["messageCount"], "error": "",
                     "cold_status": cold[code]["status"],
                     "window": {"start": int(to_export[code]) if code in to_export else None,
                                "end": now_ms}})
    save_export_state(state)
    save_cold(cold)
    return rows, changes, export_duration_sec

def ms_to_cst(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(int(ms) / 1000, CST).strftime("%Y-%m-%d %H:%M")

# ============ 8. 分析通知 ============
STRONG_KW = ["通知", "提醒", "注意", "要求", "必须", "务必", "截止", "会议", "活动",
             "讲座", "比赛", "报名", "提交", "考试", "测验", "作业", "实验", "课程",
             "上课", "全体", "辅导员", "教务", "@全体", "重要", "紧急", "截止"]
DEADLINE_KW = ["截止", "提交", "报名", "逾期", "过期", "之前交", "前交", "前提交", "不候", "之前"]
ACTIVITY_KW = ["活动", "讲座", "比赛", "会议", "宣讲会", "观影", "交流会", "志愿"]

# 优先级排序权重（越大越靠前）
PRIORITY_ORDER = {"🔴紧急": 0, "🟡重要": 1, "🟢一般": 2, "⚪忽略": 3}

def classify_priority(notice, now_dt):
    """根据 deadline / event_time 给 notice 分类优先级。
    🔴紧急: 今天或明天
    🟡重要: 本周内（今天之后到本周日）
    🟢一般: 下周及以后
    ⚪忽略: 无时间信息且 score < 4
    """
    # 优先取 deadline，其次 event_time
    t_str = notice.get("deadline") or notice.get("event_time")
    score = notice.get("score", 0)
    if not t_str:
        return "⚪忽略" if score < 4 else "🟢一般"
    try:
        # 解析 ISO 格式时间
        if isinstance(t_str, str):
            t_dt = datetime.datetime.fromisoformat(t_str)
        else:
            t_dt = t_str
    except Exception:
        return "⚪忽略" if score < 4 else "🟢一般"

    ref_date = now_dt.date()
    t_date = t_dt.date()
    # 本周一
    monday = ref_date - datetime.timedelta(days=ref_date.weekday())
    # 本周日
    sunday = monday + datetime.timedelta(days=6)

    # 今天或明天 → 紧急
    if t_date == ref_date or t_date == ref_date + datetime.timedelta(days=1):
        return "🔴紧急"
    # 本周内（本周二到本周日，且在明天之后）→ 重要
    if monday <= t_date <= sunday and t_date > ref_date + datetime.timedelta(days=1):
        return "🟡重要"
    # 下周及以后
    return "🟢一般"

def extract_text(msg):
    c = msg.get("content", {})
    if isinstance(c, dict):
        return c.get("text", "") or ""
    return str(c) if c else ""

def extract_links(msg):
    links = set()
    text = extract_text(msg)
    for u in re.findall(r"https?://[^\s<>\"'，。、；）】]+", text):
        links.add(u)
    for el in msg.get("elements", []) or []:
        if isinstance(el, dict):
            u = el.get("url") or (el.get("data", {}) or {}).get("url")
            if u and str(u).startswith("http"):
                links.add(str(u))
    return sorted(links)


# ============ 8c. OCR 图片通知识别（优化4） ============
def _is_image_msg(msg):
    """检测消息是否含图片元素。"""
    for el in msg.get("elements", []) or []:
        if isinstance(el, dict):
            etype = el.get("type", "")
            if etype in ("image", "img", "pic"):
                return True
            # QCE 格式: data 里可能有 image
            data = el.get("data", {}) or {}
            if isinstance(data, dict) and data.get("imagePath"):
                return True
    content = msg.get("content", {})
    if isinstance(content, dict):
        ctype = content.get("type", "")
        if "image" in str(ctype).lower():
            return True
    return False

def _get_image_path(msg):
    """从消息元素中提取图片本地路径或URL。"""
    for el in msg.get("elements", []) or []:
        if isinstance(el, dict):
            data = el.get("data", {}) or {}
            p = data.get("imagePath") or el.get("imagePath") or data.get("path")
            if p:
                return str(p)
            url = el.get("url") or data.get("url")
            if url and str(url).startswith("http"):
                return str(url)
    return None

def _ocr_image_file(img_path, code):
    """对图片文件做OCR，返回文本或None。"""
    if not OCR_AVAILABLE:
        return None
    try:
        # 如果是URL，先下载
        if img_path.startswith("http"):
            import tempfile
            img_path = _safe_url(img_path)
            req2 = urllib.request.Request(img_path, headers={"Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req2, timeout=15) as resp:
                tmp = os.path.join(EXPORTS_DIR, f"_ocr_{code}_{int(time.time()*1000)}.tmp")
                with open(tmp, "wb") as out:
                    out.write(resp.read())
            img_path = tmp
        if not os.path.exists(img_path):
            return None
        img = _PILImage.open(img_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception as e:
        log(f"  [OCR失败] {code}: {e}")
        return None

def _build_ocr_notice(ocr_text, msg, code, info, ref):
    """从OCR文本构建一条notice。"""
    if not ocr_text or len(ocr_text.strip()) < 3:
        return None
    t = ocr_text[:500]
    # 解析时间
    event_dt = parse_chinese_time(t, ref)
    # 提取地点
    loc_m = re.search(r"([\w\u4e00-\u9fff]{2,6}(?:楼|教室|厅|馆|中心|室|操场))", t)
    location = loc_m.group(1) if loc_m else ""
    # 简单评分
    score = 2  # OCR默认基础分
    for k in STRONG_KW:
        if k in t:
            score += 1
    nid = make_notice_id(code, t)
    ts = msg.get("timestamp")
    time_cst = ms_to_cst(ts) if ts else ""
    notice = {
        "id": nid,
        "group_code": code,
        "group_name": info["name"],
        "sender": "OCR图片",
        "time_cst": time_cst,
        "event_time": event_dt.isoformat() if event_dt else None,
        "deadline": None,
        "text": t,
        "links": [],
        "at_all": False,
        "type": "图片通知",
        "score": score,
        "is_deadline": False,
        "has_time": bool(event_dt),
        "has_date": bool(event_dt),
        "has_loc": bool(location),
        "location": location,
        "source": "ocr",
    }
    notice["priority"] = classify_priority(notice, ref)
    return notice

def analyze(monitored, state, processed):
    notices = []
    skipped_processed = []
    group_stats = {}
    total_text = 0
    for code, info in monitored.items():
        rec = state.get(code, {})
        lf = rec.get("last_file")
        path = os.path.join(EXPORTS_DIR, lf) if lf else None
        if not path or not os.path.exists(path):
            # 回退：找最新
            f = find_latest_export_file(code)
            path = f
        if not path or not os.path.exists(path):
            group_stats[code] = {"name": info["name"], "text_msgs": 0, "total_msgs": 0}
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            log(f"  [WARN] 读取 {path} 失败: {e}")
            continue
        msgs = data.get("messages", [])
        text_msgs = 0
        for msg in msgs:
            if msg.get("system", False):
                continue
            t = extract_text(msg)
            if t and len(t.strip()) >= 5:
                text_msgs += 1
        group_stats[code] = {"name": info["name"], "text_msgs": text_msgs, "total_msgs": len(msgs)}
        total_text += text_msgs

        for msg in msgs:
            if msg.get("system", False):
                continue
            t = extract_text(msg)
            if not t or len(t.strip()) < 10:
                continue
            sender = msg.get("sender", {})
            sname = sender.get("name", "") if isinstance(sender, dict) else str(sender)
            links = extract_links(msg)
            at_all = "@全体" in t or "全体成员" in t
            score = (3 if at_all else 0) + sum(1 for k in STRONG_KW if k in t) \
                    + sum(2 for k in DEADLINE_KW if k in t)
            has_time = bool(re.search(r"\d+[:：点时]\d*", t))
            has_date = bool(re.search(r"\d+月\d+日|本周|下周|周[一二三四五六日天]|明天|后天|今天|月底|周末", t))
            has_loc = bool(re.search(r"楼|教室|厅|馆|中心|室\d|C\d|B\d|A\d|操场|线上|腾讯会议", t))
            is_deadline = bool(re.search(r"截止|逾期|之前|前交|前提交|不候", t))

            if not (score >= 3 or (score >= 2 and (has_date or is_deadline))):
                continue

            nid = make_notice_id(code, t)
            already = any(n["id"] == nid for n in processed.get("notifications", []))
            if already:
                skipped_processed.append({"id": nid, "group_code": code,
                                          "text_preview": t[:60].replace("\n", " "), "action": "skipped"})
                continue

            # 解析 event_time / deadline
            # 【关键】基准时间用这条通知自己的发送时间戳，而不是脚本运行当天。
            # 这样"明天/后天/本周几/月底"永远按通知发出那天算，不会因哪天跑脚本而漂移。
            ts = msg.get("timestamp")
            ref = datetime.datetime.fromtimestamp(int(ts) / 1000, CST) if ts else now_cst()
            event_dt = parse_chinese_time(t, ref)
            deadline_dt = None
            if is_deadline:
                # 截止类：把解析到的时间当作 deadline，无具体时刻则23:59
                deadline_dt = event_dt
                if deadline_dt is not None and not re.search(r"\d{1,2}\s*[:：点时]", t):
                    deadline_dt = deadline_dt.replace(hour=23, minute=59)
            # 分类
            if is_deadline:
                ntype = "截止/待办"
            elif any(k in t for k in ACTIVITY_KW):
                ntype = "活动/会议"
            elif any(k in t for k in ["作业", "论文", "课程", "考试", "学习", "课"]):
                ntype = "学习/课程"
            else:
                ntype = "群通知"

            ts = msg.get("timestamp")
            time_cst = ms_to_cst(ts) if ts else ""
            notice_obj = {
                "id": nid,
                "group_code": code,
                "group_name": info["name"],
                "sender": sname,
                "time_cst": time_cst,
                "ts": ts,
                "event_time": event_dt.isoformat() if event_dt else None,
                "deadline": deadline_dt.isoformat() if deadline_dt else None,
                "text": t[:1500],
                "links": links,
                "at_all": at_all,
                "type": ntype,
                "score": score,
                "is_deadline": is_deadline,
                "has_time": has_time, "has_date": has_date, "has_loc": has_loc,
                "source": "text",
            }
            # 紧急度相对"现在"判断，而非通知发送日
            notice_obj["priority"] = classify_priority(notice_obj, now_cst())
            notices.append(notice_obj)
    # 优化4: 扫描图片消息做OCR
    if OCR_AVAILABLE:
        log("  [OCR] 正在扫描图片消息...")
        for code, info in monitored.items():
            rec = state.get(code, {})
            lf = rec.get("last_file")
            path = os.path.join(EXPORTS_DIR, lf) if lf else find_latest_export_file(code)
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    odata = json.load(fh)
            except Exception:
                continue
            for msg in odata.get("messages", []):
                if msg.get("system", False):
                    continue
                if not _is_image_msg(msg):
                    continue
                img_p = _get_image_path(msg)
                if not img_p:
                    continue
                ocr_text = _ocr_image_file(img_p, code)
                if not ocr_text:
                    continue
                _ots = msg.get("timestamp")
                ref = datetime.datetime.fromtimestamp(int(_ots) / 1000, CST) if _ots else now_cst()
                on = _build_ocr_notice(ocr_text, msg, code, info, ref)
                if on:
                    # 去重检查
                    nid = on["id"]
                    already = any(n["id"] == nid for n in processed.get("notifications", []))
                    if not already and not any(n["id"] == nid for n in notices):
                        notices.append(on)
                        log(f"  [OCR] {code}: {ocr_text[:50].replace(chr(10)," ")}...")
    else:
        log("  [OCR] 不可用，跳过图片识别（pip install pytesseract pillow）")

    # 排序：先按优先级（🔴>🟡>🟢>⚪），再按 score 降序
    notices.sort(key=lambda x: (PRIORITY_ORDER.get(x.get("priority", "🟢一般"), 9), -x["score"], x["time_cst"]))
    # 优先级汇总
    pri_summary = {"🔴紧急": 0, "🟡重要": 0, "🟢一般": 0, "⚪忽略": 0}
    for n in notices:
        p = n.get("priority", "🟢一般")
        if p in pri_summary:
            pri_summary[p] += 1
    result = {"analyzed_at": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
              "total_text_msgs": total_text, "notice_count": len(notices),
              "priority_summary": pri_summary,
              "group_stats": group_stats, "notices": notices}
    save_json(NOTICES_OUT, result)
    return result, skipped_processed


# ============ 8b. 通知合并（多群同事件） ============
SUPPLY_KW = ["就是", "那篇", "那个", "同上", "纸质", "电子版", "电子版",
            "交我", "发给我", "交给", "模板", "附件", "教室", "地点", "在",
            "截止时间", "材料", "格式", "几点交", "哪里交", "收到请", "确认一下",
            "问卷", "链接", "扫码", "报名"]
RANDOM_CHAT = ["又要", "第几次", "不想", "好累", "吐槽", "？？", "哈哈"]

def _is_supplement(text):
    """判断一条同群消息是不是真·补充（提供新信息），而非闲聊/吐槽。"""
    t = text or ""
    if any(k in t for k in RANDOM_CHAT):
        return False
    # 提供了具体补充信号（纸质/教室/模板/截止/链接等）
    return any(k in t for k in SUPPLY_KW)


def _extract_chinese_bigrams(text):
    """从文本中提取中文二字词集合（无jieba时的近似分词），返回所有unique bigram集合。"""
    cn = re.sub(r"[^\u4e00-\u9fff0-9]", "", text or "")
    if len(cn) < 2:
        return set()
    return set(cn[i:i+2] for i in range(len(cn) - 1))

def _containment(set_a, set_b):
    """containment系数：交集/较小集合大小，衡量短文本有多少关键词出现在长文本中。"""
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    return inter / min(len(set_a), len(set_b))

def _parse_iso(dt_str):
    """安全解析 ISO 时间字符串，返回 datetime 或 None。"""
    if not dt_str:
        return None
    try:
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        return None

def merge_notices(notices):
    """合并同事件通知：
    - 跨群：事件时间接近 + 关键词containment>=0.5
    - 同群：消息间隔<6h 且 containment>=0.35（允许把"追问地点/补格式"等补充消息并入）
    合并后不是只留最长一条，而是把所有相关原文拼接保留，供建日历/任务时全部附上。
    返回 (merged_notices, merged_count)。
    """
    if len(notices) < 2:
        return notices, 0

    enriched = []
    for n in notices:
        bg = _extract_chinese_bigrams(n.get("text", ""))
        et = _parse_iso(n.get("event_time")) or _parse_iso(n.get("deadline"))
        enriched.append({"notice": n, "bigrams": bg, "dt": et,
                         "ts": n.get("ts") or 0})

    used = [False] * len(enriched)
    merged = []
    merged_count = 0

    for i in range(len(enriched)):
        if used[i]:
            continue
        cur = enriched[i]
        cur_n = cur["notice"]
        group_codes = [cur_n["group_code"]]
        group_names = [cur_n["group_name"]]
        merged_from = [{"group_code": cur_n["group_code"],
                        "group_name": cur_n["group_name"],
                        "time_cst": cur_n.get("time_cst", ""),
                        "text": cur_n.get("text", "")}]
        best_score = cur_n.get("score", 0)

        for j in range(i + 1, len(enriched)):
            if used[j]:
                continue
            oth = enriched[j]
            oth_n = oth["notice"]
            same_group = oth_n["group_code"] in group_codes
            sim = _containment(cur["bigrams"], oth["bigrams"])

            if same_group:
                # 同群：时间窗12小时内（覆盖夜->晨）+ 话题相关 + 确属补充（非闲聊吐槽）
                if not (cur["ts"] and oth["ts"]):
                    continue
                gap_h = abs(int(oth["ts"]) - int(cur["ts"])) / 3600000.0
                if gap_h > 12:
                    continue
                if not _is_supplement(oth_n.get("text", "")):
                    continue
                # 补充消息须与主通知共享至少一个实义bigram，避免无关补充乱挂
                if not (cur["bigrams"] & oth["bigrams"]):
                    continue
            else:
                # 跨群：事件时间接近 + 相似
                time_close = False
                if cur["dt"] and oth["dt"]:
                    if abs((cur["dt"] - oth["dt"]).total_seconds()) / 3600.0 < 2:
                        time_close = True
                elif not cur["dt"] and not oth["dt"]:
                    time_close = True
                if not (time_close and sim >= 0.5):
                    continue

            used[j] = True
            merged_count += 1
            group_codes.append(oth_n["group_code"])
            group_names.append(oth_n["group_name"])
            merged_from.append({"group_code": oth_n["group_code"],
                                "group_name": oth_n["group_name"],
                                "time_cst": oth_n.get("time_cst", ""),
                                "text": oth_n.get("text", "")})
            if oth_n.get("score", 0) > best_score:
                best_score = oth_n["score"]
                cur_n = oth_n
                cur["dt"] = oth["dt"]

        used[i] = True
        # 拼接所有相关原文（去重），而非只取最长
        seen = set()
        parts = []
        for m in merged_from:
            t = (m.get("text") or "").strip()
            key = t[:30]
            if t and key not in seen:
                seen.add(key)
                parts.append(f"【{m.get('group_name','')}·{m.get('time_cst','')}】{t}")
        merged_text = "\n---\n".join(parts)[:2000]

        merged_n = dict(cur_n)
        merged_n["group_name"] = " / ".join(dict.fromkeys(group_names))
        merged_n["group_codes"] = list(dict.fromkeys(group_codes))
        merged_n["merged_from"] = merged_from
        merged_n["text"] = merged_text
        merged_n["id"] = make_notice_id(cur_n["group_code"], parts[0] if parts else "")
        merged_n["score"] = best_score
        merged.append(merged_n)

    return merged, merged_count


# ============ 时间解析自测 ============
def time_parser_selftest():
    """自测：确认常见case正确，且"第4-6周"不再误判为日期。"""
    ref = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=CST)
    cases = [
        ("明天下午3点", "2026-09-24 15:00"),
        ("本周四晚上7点", "2026-09-24 19:00"),
        ("下周一上午10:00", "2026-09-28 10:00"),
        ("9月25日14:00", "2026-09-25 14:00"),
        ("后天上午", "2026-09-25 09:00"),
    ]
    bad = [
        ("本学期创新学分申请将在第4-6周在新系统开放", None),
        ("第4-6周完成", None),
    ]
    ok = True
    for txt, exp in cases:
        r = parse_chinese_time(txt, ref)
        got = r.strftime("%Y-%m-%d %H:%M") if r else "None"
        mark = "OK" if got == exp else "FAIL"
        if got != exp:
            ok = False
        log(f"  [selftest] [{mark}] {txt!r} -> {got} (expect {exp})")
    for txt, exp in bad:
        r = parse_chinese_time(txt, ref)
        got = r.strftime("%Y-%m-%d %H:%M") if r else "None"
        mark = "OK" if r is None else "FAIL"
        if r is not None:
            ok = False
        log(f"  [selftest] [{mark}] {txt!r} -> {got} (expect None)")
    return ok

# ============ 9. 主流程 ============
def check_qce():
    try:
        d = req("GET", "/", timeout=8)
        return True, d.get("data", {}).get("version", "unknown")
    except Exception as e:
        return False, str(e)

QCE_DIR = os.path.join(WORKDIR, "QCE", "NapCat-QCE-Windows-x64")
LAUNCHER_BAT = os.path.join(QCE_DIR, "launcher-user.bat")

def _taskkill(name):
    """强制结束指定进程名的所有进程。"""
    try:
        subprocess.run(["taskkill", "/f", "/im", name], capture_output=True,
                       timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        log(f"  [WARN] 结束 {name} 失败: {e}")

def kill_qq_and_napcat():
    """杀掉所有 QQ 进程和 NapCat（用于检测到占用时强制重启登录）。"""
    log("  [QCE] 关闭用户QQ与NapCat进程...")
    _taskkill("NapCatWinBootMain.exe")
    _taskkill("QQ.exe")
    # 等待进程完全退出
    time.sleep(6)

def start_launcher():
    """后台静默启动 launcher-user.bat（自动探测QQ路径并注入）。"""
    log("  [QCE] 启动 launcher-user.bat ...")
    if not os.path.exists(LAUNCHER_BAT):
        log(f"  [错误] 找不到启动脚本: {LAUNCHER_BAT}")
        return False
    try:
        subprocess.Popen(["cmd", "/c", LAUNCHER_BAT], cwd=QCE_DIR,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except Exception as e:
        log(f"  [WARN] 启动 launcher 失败: {e}")
        return False

def wait_qce(timeout_sec=150):
    """轮询等待 QCE API 可用，返回 (ok, version)。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ok, info = check_qce()
        if ok:
            return True, info
        time.sleep(5)
    return False, check_qce()[1]

def ensure_qce(max_retries=2):
    """确保 QCE 服务可用。
    若不可用，自动杀掉 QQ+NapCat 并重启登录（解决用户QQ占用数据目录导致的冲突）。
    返回 (ok, version, how)；how ∈ {already_running, restarted, failed}。
    """
    ok, info = check_qce()
    if ok:
        return True, info, "already_running"
    log(f"  [QCE自恢复] QCE不可用: {info}，尝试自动重启登录（最多{max_retries}次）")
    for attempt in range(1, max_retries + 1):
        kill_qq_and_napcat()
        start_launcher()
        ok, info = wait_qce(150)
        if ok:
            log(f"  [QCE自恢复] 第{attempt}次重启成功，version={info}")
            return True, info, f"restarted_{attempt}"
        log(f"  [QCE自恢复] 第{attempt}次仍失败: {info}")
    return False, info, "failed"

def shutdown_qce():
    """任务完成后关闭 QCE（释放 NapCat + QQ 进程）。"""
    log("  [QCE] 任务完成，关闭QCE服务...")
    _taskkill("NapCatWinBootMain.exe")
    _taskkill("QQ.exe")
    log("  [QCE] QCE已关闭（NapCat + QQ 已结束）")

def main():
    try:
        _main_impl()
        shutdown_qce()   # 完成任务后关闭QCE
    except Exception as e:
        log(f"  [FATAL] 管道执行失败: {e}")
        import traceback
        log(traceback.format_exc())
        record_failure("overall", str(e), "检查QCE服务和网络连接")
        send_alert("critical", "管道执行失败", str(e), "检查QCE服务和网络连接")
        sys.exit(1)


def _main_impl():
    log("=" * 64)
    log("每日QQ群通知汇总管道 v2 启动")
    log(f"  时间: {now_cst().strftime('%Y-%m-%d %H:%M:%S %A')}")
    log(f"  工作目录: {WORKDIR}")

    # 0. QCE 检测（不可用时自动杀掉QQ+NapCat重启登录）
    ok, info, how = ensure_qce()
    if not ok:
        log(f"  [错误] QCE服务不可用（自动重启{2}次仍失败）: {info}")
        log("  请手动运行 QCE\\NapCat-QCE-Windows-x64\\launcher-user.bat 并扫码登录")
        record_failure("qce", info, "多次自动重启失败，需人工扫码")
        send_alert("critical", "QCE多次启动失败", info, "手动启动launcher-user.bat并扫码")
        sys.exit(1)
    log(f"  QCE服务正常 (version={info}, 启动方式={how})")
    # 时间解析自测
    time_parser_selftest()

    # 1. 加载监控群配置 + 动态API群列表
    cfg, monitored, exclude = load_monitored_config()
    log(f"  监控群配置: {len(monitored)} 个监控, {len(exclude)} 个排除")
    api_groups, api_err = fetch_api_groups()
    if api_err:
        log(f"  [WARN] 获取API群列表失败: {api_err}，使用本地监控配置")
    else:
        log(f"  API返回群总数: {len(api_groups)}")
    new_candidates = detect_new_groups(api_groups, monitored, exclude) if api_groups else []
    if new_candidates:
        log("  [发现潜在新群] (需用户确认后手动加入 monitored_groups.json):")
        for c in new_candidates:
            log(f"    - {c['code']} {c['name']} (人数={c['member_count']})")

    # 2. 初始化状态（从现有导出文件推断）
    log("\n=== 初始化/加载状态文件 ===")
    state, cold = init_state_from_existing(monitored)
    log(f"  export_state: {len(state)} 群, cold_groups: {len(cold)} 群")

    # 3. 导出
    log("\n=== 导出群聊记录（断点续传 + 冷处理）===")
    rows, cold_changes, export_duration_sec = run_exports(monitored, state, cold, api_groups)
    total_msgs = sum(r.get("messageCount", 0) for r in rows)
    record_success("export")
    log(f"  导出完成: {len(rows)} 群, 新消息合计 {total_msgs} 条")

    # 4. 分析通知
    log("\n=== 分析通知（已处理去重 + 中文时间解析）===")
    processed = load_processed()
    notice_result, skipped_processed = analyze(monitored, state, processed)
    log(f"  文本消息: {notice_result['total_text_msgs']}")
    log(f"  新通知: {notice_result['notice_count']} 条")
    log(f"  跳过(已处理): {len(skipped_processed)} 条")

    # 4b. 通知合并（多群同事件）
    log("\n=== 通知合并（多群同事件）===")
    merged_notices, merged_count = merge_notices(notice_result["notices"])
    notice_result["notices"] = merged_notices
    notice_result["notice_count"] = len(merged_notices)
    # 重新计算优先级汇总
    _pri2 = {"🔴紧急": 0, "🟡重要": 0, "🟢一般": 0, "⚪忽略": 0}
    for n in merged_notices:
        p = n.get("priority", "🟢一般")
        if p in _pri2:
            _pri2[p] += 1
    notice_result["priority_summary"] = _pri2
    notice_result["merged_count"] = merged_count
    log(f"  合并前: {notice_result['notice_count'] + merged_count} 条, 合并后: {notice_result['notice_count']} 条, 合并次数: {merged_count}")

    # 5. 把新通知写入 processed_notifications（根据内容设置不同的action）
    today = now_cst().date().isoformat()
    existing_ids = {n["id"] for n in processed.get("notifications", [])}
    for n in notice_result["notices"]:
        if n["id"] not in existing_ids:
            # 根据通知内容判断action
            has_event_time = bool(n.get("event_time"))
            has_deadline = bool(n.get("deadline"))
            if has_event_time and has_deadline:
                action = "pending_calendar_task"
            elif has_event_time:
                action = "pending_calendar"
            elif has_deadline:
                action = "pending_task"
            else:
                action = "ignored"  # 不需要添加到日历/任务
            
            processed.setdefault("notifications", []).append({
                "id": n["id"], "group_code": n["group_code"],
                "text_hash": n["id"], "processed_at": today,
                "action": action, "calendar_event_id": "", "task_guid": "",
            })
    save_processed(processed)

    # 6. 写 _export_result.json
    exp_result = {"exported_at": now_cst().isoformat(), "total_messages": total_msgs,
                  "groups": rows,
                  "cold_changes": cold_changes,
                  "new_group_candidates": new_candidates}
    save_json(EXPORT_RESULT_OUT, exp_result)

    # 7. 写 _pipeline_summary.json（AI读取这个）
    summary = {
        "pipeline": "daily_pipeline_v2",
        "run_at": now_cst().isoformat(),
        "qce_ok": True,
        "qce_version": info,
        "api_group_count": len(api_groups),
        "new_group_candidates": new_candidates,
        "export": {"total_new_messages": total_msgs, "groups": rows, "cold_changes": cold_changes,
                     "parallel_workers": 5, "export_duration_sec": export_duration_sec},
        "analysis": {"total_text_msgs": notice_result["total_text_msgs"],
                     "new_notice_count": notice_result["notice_count"],
                     "priority_summary": notice_result.get("priority_summary", {}),
                     "merged_count": merged_count,
                     "skipped_processed_count": len(skipped_processed),
                     "notices": notice_result["notices"],
                     "skipped_processed": skipped_processed},
        "cold_groups": {c: v for c, v in cold.items()},
        "next_steps_for_ai": [
            "读取本文件 analysis.notices，为每条新通知添加飞书日历事件(event_time)和任务(deadline)",
            "添加前先查询现有日历/任务避免重复",
            "汇总报告见本文件，上传飞书云空间并回复用户",
        ],
    }
    save_json(PIPELINE_SUMMARY, summary)

    # 8. 控制台摘要
    log("\n=== 通知摘要 ===")
    _pri = notice_result.get("priority_summary", {})
    log(f"  优先级: 🔴紧急={_pri.get('🔴紧急',0)} 🟡重要={_pri.get('🟡重要',0)} 🟢一般={_pri.get('🟢一般',0)} ⚪忽略={_pri.get('⚪忽略',0)}")
    for i, n in enumerate(notice_result["notices"], 1):
        et = n.get("event_time") or "-"
        dl = n.get("deadline") or "-"
        pfx = "[🔴紧急] " if n.get("priority") == "🔴紧急" else ""
        log(f"  [{i}] {pfx}[{n['type']}] [{n.get('priority','')}] event={et[:16]} deadline={dl[:16]} | {n['group_name']}")
        log(f"      {n['text'][:90].replace(chr(10),' ')}")
    log("\n" + "=" * 64)
    log(f"完成: 新通知 {notice_result['notice_count']} 条, 跳过已处理 {len(skipped_processed)} 条")
    log(f"输出: {PIPELINE_SUMMARY}")


# ============ 10. 日历/任务辅助工具函数（防张冠李戴） ============

def get_notice_by_id(notice_id):
    """根据通知ID从_pipeline_summary.json里找到对应的通知。
    AI创建日历时调用这个函数，不需要自己从列表里找，从根源上杜绝看错行！
    """
    summary = load_json(PIPELINE_SUMMARY, {})
    notices = summary.get("analysis", {}).get("notices", [])
    for n in notices:
        if n.get("id") == notice_id:
            return n
    return None


def generate_calendar_description(notice_id):
    """自动生成完整的日历事件描述。
    AI只需要传通知ID，脚本自动填充原文、来源群、发布时间等。
    不需要AI手动复制粘贴原文，彻底杜绝张冠李戴！
    """
    notice = get_notice_by_id(notice_id)
    if not notice:
        return f"[错误] 找不到通知ID: {notice_id}"
    
    text = notice.get("text", "")
    group_name = notice.get("group_name", "")
    time_cst = notice.get("time_cst", "")
    links = notice.get("links", [])
    location = notice.get("location", "")
    merged_from = notice.get("merged_from", [])
    
    # 构建描述
    desc = f"""📜 通知原文：
{text}

📢 来源群：{group_name}
🕐 发布时间：{time_cst}"""
    
    if location:
        desc += f"\n📍 地点：{location}"
    
    if merged_from and len(merged_from) > 1:
        desc += f"\n\n🔗 多群合并通知："
        for m in merged_from:
            desc += f"\n  - {m.get('group_name', '')}"
    
    if links:
        desc += f"\n\n🔗 相关链接："
        for link in links:
            desc += f"\n  {link}"
    
    desc += f"\n\n✅ 通知ID：{notice_id}"
    
    return desc


def generate_task_description(notice_id):
    """自动生成完整的任务描述。
    和generate_calendar_description类似，但更简洁。
    """
    notice = get_notice_by_id(notice_id)
    if not notice:
        return f"[错误] 找不到通知ID: {notice_id}"
    
    text = notice.get("text", "")
    group_name = notice.get("group_name", "")
    time_cst = notice.get("time_cst", "")
    deadline = notice.get("deadline", "")
    
    desc = f"""📜 通知原文：
{text}

📢 来源群：{group_name}
🕐 发布时间：{time_cst}"""
    
    if deadline:
        desc += f"\n⏰ 截止时间：{deadline}"
    
    desc += f"\n\n✅ 通知ID：{notice_id}"
    
    return desc


def verify_calendar_event(notice_id, event_description):
    """校验日历事件描述是否和通知原文匹配。
    创建完日历事件后调用这个函数，自动检查有没有张冠李戴！
    返回 (is_valid, error_message)
    """
    notice = get_notice_by_id(notice_id)
    if not notice:
        return False, f"找不到通知ID: {notice_id}"
    
    text = notice.get("text", "")
    # 取原文前50个字符作为校验片段
    check_fragment = text[:50].replace("\n", " ").strip()
    
    # 检查描述里是否包含这个片段
    if check_fragment and check_fragment in event_description.replace("\n", " "):
        return True, "校验通过：描述和通知原文匹配"
    else:
        return False, f"校验失败：描述里找不到通知原文片段 '{check_fragment[:30]}...'，可能张冠李戴了！"


def list_notices_for_ai():
    """列出所有新通知的ID和摘要，方便AI逐个处理。
    AI调用这个函数，按列表逐个处理，不会看花眼！
    """
    summary = load_json(PIPELINE_SUMMARY, {})
    notices = summary.get("analysis", {}).get("notices", [])
    
    print("=" * 60)
    print(f"新通知列表（共 {len(notices)} 条）：")
    print("=" * 60)
    
    for i, n in enumerate(notices, 1):
        nid = n.get("id", "")
        title = n.get("text", "")[:60].replace("\n", " ")
        group = n.get("group_name", "")
        priority = n.get("priority", "")
        event_time = n.get("event_time", "") or "-"
        deadline = n.get("deadline", "") or "-"
        
        print(f"\n[{i}] {priority}")
        print(f"  通知ID：{nid}")
        print(f"  摘要：{title}...")
        print(f"  来源群：{group}")
        print(f"  事件时间：{event_time[:16]}")
        print(f"  截止时间：{deadline[:16]}")
    
    print("\n" + "=" * 60)
    print("使用方法：")
    print("  1. 调用 generate_calendar_description(通知ID) 生成日历描述")
    print("  2. 调用 generate_task_description(通知ID) 生成任务描述")
    print("  3. 创建完事件后，调用 verify_calendar_event(通知ID, 事件描述) 校验")
    print("=" * 60)


# 命令行入口：python daily_pipeline.py --list-notices
# 或：python daily_pipeline.py --desc <通知ID>
# 或：python daily_pipeline.py --verify <通知ID> <事件描述>
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--list-notices":
            list_notices_for_ai()
        elif cmd == "--desc" and len(sys.argv) > 2:
            print(generate_calendar_description(sys.argv[2]))
        elif cmd == "--task-desc" and len(sys.argv) > 2:
            print(generate_task_description(sys.argv[2]))
        elif cmd == "--verify" and len(sys.argv) > 3:
            notice_id = sys.argv[2]
            desc = sys.argv[3]
            valid, msg = verify_calendar_event(notice_id, desc)
            print(f"校验结果：{'✅ 通过' if valid else '❌ 失败'}")
            print(f"详情：{msg}")
            sys.exit(0 if valid else 1)
        else:
            print("用法：")
            print("  python daily_pipeline.py                  # 运行管道")
            print("  python daily_pipeline.py --list-notices     # 列出所有新通知")
            print("  python daily_pipeline.py --desc <通知ID>  # 生成日历描述")
            print("  python daily_pipeline.py --task-desc <通知ID>  # 生成任务描述")
            print("  python daily_pipeline.py --verify <通知ID> <事件描述>  # 校验事件描述")
    else:
        main()
