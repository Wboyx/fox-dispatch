#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════
 FOXTASK — کلاینت سرور ایران برای ثبت تسک روی گیت‌هاب
 نسخه: 1.0 | 2026-08-19
════════════════════════════════════════════════════════════════

چرا:
  سرور ایران به خیلی از سرویس‌ها دسترسی ندارد، ولی به گیت‌هاب دارد.
  پس فقط تسک را ثبت می‌کند و اجرای واقعی روی رانر گیت‌هاب انجام می‌شود.

استفاده:
  export GITHUB_TOKEN=...
  foxtask.py submit probe --title "تست دسترسی" --url https://huggingface.co/
  foxtask.py wait <id>
  foxtask.py runs

امنیت:
  Token فقط از متغیر محیطی خوانده می‌شود و هرگز در تسک یا لاگ نمی‌رود.
  قبل از ارسال، متن تسک برای الگوی Secret اسکن می‌شود.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("FOX_DISPATCH_REPO", "Wboyx/fox-dispatch")
API = "https://api.github.com"

SECRET_RE = [re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
             re.compile(r"[0-9]{8,12}:[A-Za-z0-9_-]{30,}")]

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m", "x": "\033[0m"}


def token():
    t = os.environ.get("GITHUB_TOKEN", "")
    if not t:
        print("متغیر GITHUB_TOKEN تنظیم نشده است.")
        sys.exit(1)
    return t


def api(path, method="GET", body=None, quiet404=False):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token(),
        "Accept": "application/vnd.github+json",
        "User-Agent": "foxtask/1.0",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and quiet404:
            raise FileNotFoundError(path)
        detail = e.read().decode()[:300]
        print("%sخطای گیت‌هاب %s%s: %s" % (C["r"], e.code, C["x"], detail))
        sys.exit(2)


def now_id(ttype, slug):
    return "%s-%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), ttype, slug)


def cmd_submit(a):
    inputs = {}
    if a.input_json:
        inputs = json.loads(a.input_json)
    if a.url:
        if a.type == "probe":
            inputs.setdefault("urls", []).extend(a.url)
        else:
            inputs["url"] = a.url[0]
    if a.filename:
        inputs["filename"] = a.filename
    if getattr(a, "model", None):
        inputs["model"] = a.model
    if getattr(a, "prompt", None):
        inputs["prompt"] = a.prompt

    tid = now_id(a.type, a.slug or "task")
    task = {
        "id": tid, "type": a.type, "title": a.title or a.type,
        "created_by": a.by, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "priority": a.priority, "inputs": inputs,
        "expect": a.expect or "", "timeout_sec": a.timeout,
    }
    text = json.dumps(task, ensure_ascii=False, indent=2)
    for p in SECRET_RE:
        if p.search(text):
            print("%sتسک حاوی چیزی شبیه Secret است. ارسال نشد.%s" % (C["r"], C["x"]))
            return 1
    path = "tasks/queue/%s.json" % tid
    api("/repos/%s/contents/%s" % (REPO, path), "PUT", {
        "message": "task: %s" % task["title"],
        "content": base64.b64encode(text.encode()).decode(),
    })
    print("%sتسک ثبت شد%s" % (C["g"], C["x"]))
    print("  شناسه : %s" % tid)
    print("  مسیر  : %s" % path)
    print("\nپیگیری:\n  foxtask.py wait %s" % tid)
    return 0


def fetch_result(tid):
    try:
        r = api("/repos/%s/contents/tasks/done/%s.json" % (REPO, tid), quiet404=True)
        return json.loads(base64.b64decode(r["content"]).decode())
    except (FileNotFoundError, SystemExit):
        return None


def cmd_status(a):
    res = fetch_result(a.id)
    if not res:
        print("هنوز نتیجه‌ای ثبت نشده. احتمالاً در حال اجراست.")
        return 1
    show(res)
    return 0


def show(res):
    color = C["g"] if res.get("status") == "success" else C["r"]
    print("\nشناسه   : %s" % res.get("id"))
    print("وضعیت   : %s%s%s" % (color, res.get("status"), C["x"]))
    print("مدت     : %s ثانیه" % res.get("duration_sec"))
    print("اجراکننده: %s" % res.get("runner"))
    if res.get("artifacts"):
        print("خروجی‌ها : %s" % ", ".join(res["artifacts"]))
        print("%sفایل‌ها در بخش Artifacts همان اجرا قابل دانلودند.%s" % (C["d"], C["x"]))
    print("\nلاگ:")
    for line in (res.get("log") or "").splitlines():
        print("  " + line)


def cmd_wait(a):
    print("در انتظار نتیجه %s ..." % a.id)
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        res = fetch_result(a.id)
        if res:
            show(res)
            return 0 if res.get("status") == "success" else 1
        time.sleep(a.interval)
        print("  %s..." % datetime.now().strftime("%H:%M:%S"))
    print("زمان انتظار تمام شد. با دستور status دوباره بررسی کن.")
    return 2


def cmd_runs(a):
    r = api("/repos/%s/actions/runs?per_page=%d" % (REPO, a.limit))
    print("\nآخرین اجراها:\n")
    for run in r.get("workflow_runs", []):
        st = run.get("conclusion") or run.get("status")
        col = C["g"] if st == "success" else (C["y"] if st in ("in_progress", "queued") else C["r"])
        print("  #%-5s %s%-12s%s %s  %s" % (run["run_number"], col, st, C["x"],
                                            run["created_at"][:19].replace("T", " "),
                                            run.get("display_title", "")[:46]))
    return 0


def cmd_queue(a):
    try:
        r = api("/repos/%s/contents/tasks/queue" % REPO)
    except SystemExit:
        return 1
    items = [x for x in r if x["name"].endswith(".json")]
    print("\nتسک‌های در صف: %d" % len(items))
    for x in items:
        print("  %s" % x["name"])
    return 0


def main():
    p = argparse.ArgumentParser(prog="foxtask", description="ثبت تسک روی fox-dispatch")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("submit", help="ثبت تسک جدید")
    s.add_argument("type", choices=["probe", "fetch", "ffmpeg", "assemble", "hf", "cf", "keycheck"])
    s.add_argument("--model"); s.add_argument("--prompt")
    s.add_argument("--title"); s.add_argument("--slug")
    s.add_argument("--url", action="append")
    s.add_argument("--filename")
    s.add_argument("--input-json")
    s.add_argument("--expect")
    s.add_argument("--by", default="iran-server")
    s.add_argument("--priority", default="normal", choices=["first", "normal", "low"])
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(f=cmd_submit)

    s = sub.add_parser("status", help="وضعیت یک تسک")
    s.add_argument("id"); s.set_defaults(f=cmd_status)

    s = sub.add_parser("wait", help="انتظار تا آماده‌شدن نتیجه")
    s.add_argument("id"); s.add_argument("--interval", type=int, default=15)
    s.add_argument("--timeout", type=int, default=600); s.set_defaults(f=cmd_wait)

    s = sub.add_parser("runs", help="آخرین اجراهای گیت‌هاب")
    s.add_argument("--limit", type=int, default=8); s.set_defaults(f=cmd_runs)

    s = sub.add_parser("queue", help="فهرست صف"); s.set_defaults(f=cmd_queue)

    a = p.parse_args()
    if not getattr(a, "f", None):
        p.print_help(); return 0
    return a.f(a) or 0


if __name__ == "__main__":
    sys.exit(main())
