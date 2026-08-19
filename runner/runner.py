#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════
 FOX DISPATCH RUNNER — اجراکننده تسک روی GitHub Actions
 نسخه: 1.0 | 2026-08-19
════════════════════════════════════════════════════════════════

چرا هست:
  سرور ایران به خیلی از سرویس‌ها دسترسی ندارد و طبق قانون قرمز هم
  نباید کار سنگین روی آن اجرا شود. این اجراکننده روی رانر گیت‌هاب
  که خارج از ایران است کار می‌کند.

چه می‌کند:
  تسک‌های داخل tasks/queue را می‌خواند، اجرا می‌کند، نتیجه را در
  tasks/done می‌نویسد و خروجی‌ها را در پوشه out می‌گذارد.

چه نمی‌کند:
  پروکسی عمومی نیست. فقط دامنه‌های فهرست مجاز را می‌گیرد.
  هیچ Secret در تسک نمی‌پذیرد و اگر ببیند، تسک را رد می‌کند.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "tasks", "queue")
DONE = os.path.join(ROOT, "tasks", "done")
OUT = os.path.join(ROOT, "out")
ALLOWLIST = os.path.join(ROOT, "runner", "allowlist.txt")

SECRET_RE = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"[0-9]{8,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[\"':=]\s*\S{8,}"),
]

MAX_OUTPUT_MB = 90


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_allowlist():
    hosts = []
    if os.path.exists(ALLOWLIST):
        for line in open(ALLOWLIST, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                hosts.append(line.lower())
    return hosts


def host_allowed(url, hosts):
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    h = h.lower()
    return any(h == a or h.endswith("." + a) for a in hosts)


def scan_secrets(text):
    hits = []
    for p in SECRET_RE:
        hits += [str(m)[:10] + "…" for m in p.findall(text)]
    return hits


def sh(cmd, timeout=600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "")[-4000:], (p.stderr or "")[-4000:]


def download(url, dest, hosts, timeout=180):
    if not host_allowed(url, hosts):
        raise ValueError("دامنه مجاز نیست: %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "fox-dispatch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return os.path.getsize(dest)


# ───────────────────────── task types ─────────────────────────

def run_probe(task, hosts):
    urls = task["inputs"].get("urls", [])
    lines = []
    for u in urls[:40]:
        t0 = time.time()
        code = "000"
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "fox-dispatch/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                code = str(r.status)
        except urllib.error.HTTPError as e:
            code = str(e.code)
        except Exception as e:
            code = "000 (%s)" % type(e).__name__
        lines.append("%-52s code=%-16s %.2fs" % (u[:52], code, time.time() - t0))
    return "\n".join(lines), []


def run_fetch(task, hosts):
    url = task["inputs"]["url"]
    name = task["inputs"].get("filename") or os.path.basename(
        urllib.parse.urlparse(url).path) or "download.bin"
    dest = os.path.join(OUT, name)
    size = download(url, dest, hosts)
    if size > MAX_OUTPUT_MB * 1024 * 1024:
        os.remove(dest)
        raise ValueError("فایل بزرگ‌تر از سقف %d مگابایت است" % MAX_OUTPUT_MB)
    return "دریافت شد: %s  (%.2f MB)" % (name, size / 1048576), [name]


def run_ffmpeg(task, hosts):
    ins = task["inputs"]
    log = []
    for d in ins.get("downloads", []):
        dest = os.path.join(OUT, d["as"])
        size = download(d["url"], dest, hosts)
        log.append("دریافت %s  %.2f MB" % (d["as"], size / 1048576))
    args = ins["args"]
    for a in args:
        if a.startswith("-") is False and ("/" in a or "\\" in a) and not a.startswith("out"):
            raise ValueError("مسیر مطلق در آرگومان مجاز نیست: %s" % a)
    code, out, err = sh(["ffmpeg", "-y", "-loglevel", "warning"] + args,
                        timeout=task.get("timeout_sec", 900))
    log.append("ffmpeg exit=%d" % code)
    if err:
        log.append(err[-1500:])
    if code != 0:
        raise RuntimeError("ffmpeg شکست خورد")
    outfile = ins.get("output")
    arts = [outfile] if outfile and os.path.exists(os.path.join(OUT, outfile)) else []
    return "\n".join(log), arts


def run_assemble(task, hosts):
    ins = task["inputs"]
    clips = ins["clips"]
    names = []
    for i, url in enumerate(clips):
        n = "clip%02d.mp4" % i
        download(url, os.path.join(OUT, n), hosts)
        names.append(n)
    listfile = os.path.join(OUT, "concat.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for n in names:
            f.write("file '%s'\n" % n)
    outfile = ins.get("output", "final.mp4")
    code, out, err = sh(["ffmpeg", "-y", "-loglevel", "warning", "-f", "concat",
                         "-safe", "0", "-i", "concat.txt", "-c:v", "libx264",
                         "-pix_fmt", "yuv420p", "-r", str(ins.get("fps", 24)), outfile],
                        timeout=task.get("timeout_sec", 1200))
    if code != 0:
        raise RuntimeError("چیدمان شکست خورد: %s" % err[-800:])
    return "چیده شد از %d کلیپ" % len(names), [outfile]


HANDLERS = {"probe": run_probe, "fetch": run_fetch,
            "ffmpeg": run_ffmpeg, "assemble": run_assemble}


# ───────────────────────── main ─────────────────────────

def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    hosts = load_allowlist()
    if not os.path.isdir(QUEUE):
        print("صف وجود ندارد"); return 0
    files = sorted(f for f in os.listdir(QUEUE) if f.endswith(".json"))
    if not files:
        print("صف خالی است، کاری برای انجام نیست")
        return 0
    print("تعداد تسک در صف: %d\n" % len(files))
    ok = fail = 0
    for fn in files:
        path = os.path.join(QUEUE, fn)
        raw = open(path, encoding="utf-8").read()
        print("=" * 62)
        print("تسک: %s" % fn)
        hits = scan_secrets(raw)
        started = now()
        t0 = time.time()
        result = {"id": fn[:-5], "started_at": started, "runner": "github-actions"}
        if hits:
            result.update({"status": "failed", "log": "تسک حاوی چیزی شبیه Secret بود و اجرا نشد: %s" % hits[:2],
                           "artifacts": []})
            print("  رد شد: Secret مشکوک")
            fail += 1
        else:
            try:
                task = json.loads(raw)
                ttype = task.get("type")
                handler = HANDLERS.get(ttype)
                if not handler:
                    raise ValueError("نوع تسک ناشناخته: %s" % ttype)
                print("  نوع: %s   عنوان: %s" % (ttype, task.get("title", "-")))
                cwd = os.getcwd()
                os.chdir(OUT)
                try:
                    log, arts = handler(task, hosts)
                finally:
                    os.chdir(cwd)
                result.update({"status": "success", "log": log, "artifacts": arts,
                               "type": ttype, "title": task.get("title", "")})
                print("  ✅ موفق")
                print("\n".join("     " + l for l in log.splitlines()[:20]))
                ok += 1
            except Exception as e:
                result.update({"status": "failed", "log": "%s: %s" % (type(e).__name__, e),
                               "artifacts": []})
                print("  ❌ ناموفق: %s" % e)
                fail += 1
        result["finished_at"] = now()
        result["duration_sec"] = round(time.time() - t0, 1)
        with open(os.path.join(DONE, fn), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.remove(path)
    print("\n" + "=" * 62)
    print("موفق: %d    ناموفق: %d" % (ok, fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
