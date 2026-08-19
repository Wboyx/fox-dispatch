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


def run_hf(task, hosts):
    """فراخوانی مدل روی Hugging Face. کلید از Secret مخزن خوانده می‌شود."""
    tok = os.environ.get("HF_TOKEN", "")
    if not tok:
        raise RuntimeError(
            "کلید HF_TOKEN در Secrets مخزن تنظیم نشده است. "
            "مسیر: Settings > Secrets and variables > Actions > New repository secret")
    ins = task["inputs"]
    model = ins["model"]
    payload = {"inputs": ins.get("prompt", "")}
    if ins.get("parameters"):
        payload["parameters"] = ins["parameters"]
    url = "https://api-inference.huggingface.co/models/" + model
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json",
                 "User-Agent": "fox-dispatch/1.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 600)) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    took = round(time.time() - t0, 1)
    name = ins.get("output") or ("hf_out." + ("png" if "image" in ctype else
                                              "mp4" if "video" in ctype else "json"))
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(body)
    return "مدل: %s\nنوع پاسخ: %s\nحجم: %.1f KB\nزمان: %ss" % (
        model, ctype, len(body) / 1024, took), [name]


def run_keycheck(task, hosts):
    """بررسی می‌کند کدام کلیدها تنظیم شده‌اند. هرگز مقدار کلید را چاپ نمی‌کند."""
    names = ["HF_TOKEN", "GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY",
             "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
             "CF_ACCOUNT_ID", "CF_API_TOKEN", "CF2_ACCOUNT_ID", "CF2_API_TOKEN",
             "NVIDIA_API_KEY"]
    lines = []
    for n in names:
        v = os.environ.get(n, "")
        lines.append("%-20s %s" % (n, ("✅ تنظیم شده   طول: %d" % len(v)) if v else "❌ تنظیم نشده"))
    return "\n".join(lines), []


def run_cf(task, hosts):
    """تولید تصویر با Cloudflare Workers AI.

    پشتیبانی از دو حساب جدا. انتخاب صریح است، نه چرخش خودکار:
        "account": "cf"   حساب اصلی، پیش‌فرض
        "account": "cf2"  حساب دوم، مخصوص کار هوش مصنوعی

    دلیل جداسازی: بار کار آزمایشی نباید روی حسابی بیفتد که رله ربات
    روی آن است. این جداسازی بار است، نه دور زدن سهمیه.
    """
    which = (task["inputs"].get("account") or "cf").lower()
    prefix = "CF2" if which in ("cf2", "second", "ai") else "CF"
    acc = os.environ.get("%s_ACCOUNT_ID" % prefix, "")
    tok = os.environ.get("%s_API_TOKEN" % prefix, "")
    if not acc or not tok:
        raise RuntimeError(
            "کلیدهای %s_ACCOUNT_ID و %s_API_TOKEN در Secrets مخزن تنظیم نشده‌اند" % (prefix, prefix))
    ins = task["inputs"]
    model = ins.get("model", "@cf/black-forest-labs/flux-1-schnell")
    payload = {"prompt": ins["prompt"]}
    for k in ("steps", "width", "height", "seed"):
        if ins.get(k) is not None:
            payload[k] = ins[k]
    url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s" % (acc, model)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json",
                                          "User-Agent": "fox-dispatch/1.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 300)) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    took = round(time.time() - t0, 1)
    name = ins.get("output", "frame.png")
    if "json" in ctype:
        data = json.loads(body.decode())
        img_b64 = (data.get("result") or {}).get("image")
        if not img_b64:
            raise RuntimeError("پاسخ تصویر نداشت: %s" % json.dumps(data)[:300])
        import base64 as _b64
        body = _b64.b64decode(img_b64)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(body)
    return "حساب: %s\nمدل: %s\nفایل: %s\nحجم: %.1f KB\nزمان: %ss" % (
        prefix, model, name, len(body) / 1024, took), [name]


def run_poll(task, hosts):
    """تولید تصویر با Pollinations — بدون هیچ کلیدی.

    مسیر پشتیبان رایگان برای فریم کلیدی. کیفیتش از FLUX کمتر است
    ولی هیچ حساب و کلیدی نمی‌خواهد، پس همیشه در دسترس است.
    """
    ins = task["inputs"]
    prompt = ins["prompt"]
    q = {"width": ins.get("width", 1024), "height": ins.get("height", 576),
         "nologo": "true", "model": ins.get("model", "flux")}
    if ins.get("seed") is not None:
        q["seed"] = ins["seed"]
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt, safe="") \
          + "?" + urllib.parse.urlencode(q)
    name = ins.get("output", "frame.png")
    dest = os.path.join(OUT, name)
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "fox-dispatch/1.0"})
    with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 180)) as r, \
            open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    size = os.path.getsize(dest)
    if size < 2000:
        raise RuntimeError("خروجی خیلی کوچک است، احتمالاً تصویر واقعی نیست")
    return "بدون کلید\nفایل: %s\nحجم: %.1f KB\nزمان: %ss\nابعاد درخواستی: %sx%s" % (
        name, size / 1024, round(time.time() - t0, 1), q["width"], q["height"]), [name]


HANDLERS = {"probe": run_probe, "fetch": run_fetch,
            "ffmpeg": run_ffmpeg, "assemble": run_assemble, "hf": run_hf,
            "cf": run_cf, "keycheck": run_keycheck, "poll": run_poll}


QUOTA = os.path.join(ROOT, "registry", "quota.json")


def record_quota(result):
    """ثبت مصرف در دفتر سهمیه، تا بدانیم کدام اجراکننده چقدر خرج برداشت."""
    try:
        data = json.load(open(QUOTA, encoding="utf-8")) if os.path.exists(QUOTA) \
            else {"schema": 1, "days": {}}
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        worker = "huggingface" if result.get("type") == "hf" else "github-actions"
        d = data.setdefault("days", {}).setdefault(day, {})
        w = d.setdefault(worker, {"tasks": 0, "seconds": 0.0, "failed": 0})
        w["tasks"] += 1
        w["seconds"] = round(w["seconds"] + float(result.get("duration_sec", 0)), 1)
        if result.get("status") != "success":
            w["failed"] += 1
        for k in sorted(data["days"])[:-30]:
            data["days"].pop(k, None)
        with open(QUOTA, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("  (ثبت سهمیه انجام نشد: %s)" % e)


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
        record_quota(result)
        with open(os.path.join(DONE, fn), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.remove(path)
    print("\n" + "=" * 62)
    print("موفق: %d    ناموفق: %d" % (ok, fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
