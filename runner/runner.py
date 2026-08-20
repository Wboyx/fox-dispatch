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
    """فراخوانی مدل روی Hugging Face از راه Router.

    نکته ۲۰۲۶: مسیر قدیمی api-inference حذف شده و مدل‌ها از راه
    ارائه‌دهنده‌ها سرو می‌شوند. هر ارائه‌دهنده شکل درخواست خودش را دارد.

    ورودی‌ها:
        model     شناسه مدل روی هاب
        provider  nscale | together | fal-ai | hf-inference
        mode      images | raw     پیش‌فرض بر اساس ارائه‌دهنده
    """
    tok = os.environ.get("HF_TOKEN", "")
    if not tok:
        raise RuntimeError("کلید HF_TOKEN در Secrets مخزن تنظیم نشده است")
    ins = task["inputs"]
    model = ins["model"]
    provider = ins.get("provider", "nscale")
    prompt = ins.get("prompt", "")
    base = "https://router.huggingface.co"
    headers = {"Authorization": "Bearer " + tok,
               "Content-Type": "application/json",
               "User-Agent": "fox-dispatch/1.0"}

    if provider in ("nscale", "together", "fireworks-ai", "nebius"):
        url = "%s/%s/v1/images/generations" % (base, provider)
        payload = {"model": model, "prompt": prompt,
                   "response_format": ins.get("response_format", "b64_json")}
        for k in ("size", "n"):
            if ins.get(k):
                payload[k] = ins[k]
        mode = "images"
    elif provider == "fal-ai":
        url = "%s/fal-ai/%s" % (base, ins.get("provider_id", model))
        payload = {"prompt": prompt}
        payload.update(ins.get("parameters") or {})
        mode = "fal"
    else:
        url = "%s/hf-inference/models/%s" % (base, model)
        payload = {"inputs": prompt}
        if ins.get("parameters"):
            payload["parameters"] = ins["parameters"]
        mode = "raw"

    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 600)) as r:
            ctype = r.headers.get("Content-Type", "")
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("Hugging Face %s داد [%s]: %s" % (e.code, provider, detail))
    took = round(time.time() - t0, 1)

    import base64 as _b64
    name = ins.get("output", "hf_out.png")
    if mode == "images":
        data = json.loads(body.decode())
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            body = _b64.b64decode(item["b64_json"])
        elif item.get("url"):
            body = urllib.request.urlopen(
                urllib.request.Request(item["url"], headers={"User-Agent": "fox-dispatch"}),
                timeout=180).read()
        else:
            raise RuntimeError("پاسخ تصویر نداشت: %s" % json.dumps(data)[:300])
    elif mode == "fal":
        data = json.loads(body.decode())
        imgs = data.get("images") or data.get("video") or []
        u = (imgs[0].get("url") if isinstance(imgs, list) and imgs else
             (imgs.get("url") if isinstance(imgs, dict) else None))
        if not u:
            raise RuntimeError("پاسخ فایل نداشت: %s" % json.dumps(data)[:300])
        body = urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": "fox-dispatch"}), timeout=300).read()
    elif "json" in ctype:
        raise RuntimeError("پاسخ JSON بود نه فایل: %s" % body.decode()[:300])

    with open(os.path.join(OUT, name), "wb") as f:
        f.write(body)
    return "ارائه‌دهنده: %s\nمدل: %s\nفایل: %s\nحجم: %.1f KB\nزمان: %ss" % (
        provider, model, name, len(body) / 1024, took), [name]


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

    دو حساب جدا، انتخاب صریح:  "account": "cf" یا "cf2"
    """
    which = (task["inputs"].get("account") or "cf").lower()
    prefix = "CF2" if which in ("cf2", "second", "ai") else "CF"
    acc = os.environ.get("%s_ACCOUNT_ID" % prefix, "")
    tok = os.environ.get("%s_API_TOKEN" % prefix, "")
    if not acc or not tok:
        raise RuntimeError("کلیدهای %s_ACCOUNT_ID و %s_API_TOKEN تنظیم نشده‌اند" % (prefix, prefix))
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
    try:
        with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 300)) as r:
            ctype = r.headers.get("Content-Type", "")
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        hint = ""
        if e.code == 401:
            hint = ("\nراهنما: دسترسی توکن باید Account > Workers AI باشد و "
                    "Account Resources هم انتخاب شده باشد")
        raise RuntimeError("کلادفلر %s داد: %s%s" % (e.code, detail, hint))
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
    """تولید تصویر با Pollinations، بدون هیچ کلیدی. مسیر اضطراری."""
    ins = task["inputs"]
    q = {"width": ins.get("width", 1024), "height": ins.get("height", 576),
         "nologo": "true", "model": ins.get("model", "flux")}
    if ins.get("seed") is not None:
        q["seed"] = ins["seed"]
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(ins["prompt"], safe="") \
          + "?" + urllib.parse.urlencode(q)
    name = ins.get("output", "frame.png")
    dest = os.path.join(OUT, name)
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "fox-dispatch/1.0"})
    with urllib.request.urlopen(req, timeout=task.get("timeout_sec", 180)) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    size = os.path.getsize(dest)
    if size < 2000:
        raise RuntimeError("خروجی خیلی کوچک است")
    return "بدون کلید\nفایل: %s\nحجم: %.1f KB\nزمان: %ss" % (
        name, size / 1024, round(time.time() - t0, 1)), [name]


def run_space(task, hosts):
    """فراخوانی یک Space روی Hugging Face از راه API گرادیو.

    چرا مهم است: سهمیه GPU رایگان روزانه حساب مصرف می‌شود، نه اعتبار پولی.
    این تنها مسیر رایگان و خودکار برای تولید ویدیو است.

    ورودی‌ها:
        space     شناسه فضا، مثلا Lightricks/ltx-video-distilled
        mode      inspect برای دیدن امضای API، call برای اجرا
        api_name  نام تابع، مثلا /generate
        args      فهرست آرگومان‌های ترتیبی
        kwargs    آرگومان‌های نام‌دار
    """
    tok = os.environ.get("HF_TOKEN", "")
    ins = task["inputs"]
    space = ins["space"]
    try:
        from gradio_client import Client
    except ImportError:
        code, out, err = sh([sys.executable, "-m", "pip", "install", "-q",
                             "gradio_client"], timeout=420)
        import importlib, site
        importlib.invalidate_caches()
        for extra in site.getsitepackages() + [site.getusersitepackages()]:
            if extra not in sys.path:
                sys.path.append(extra)
        try:
            from gradio_client import Client
        except ImportError:
            raise RuntimeError(
                "gradio_client در دسترس نیست. نصبش در Workflow انجام می‌شود؛ "
                "اگر این خطا دیده شد یعنی مرحله نصب اجرا نشده. خروجی pip: %s"
                % (err or out)[-300:])

    t0 = time.time()
    # نام پارامتر توکن بین نسخه‌های gradio_client فرق می‌کند
    client = None
    last = None
    for kw in ({"token": tok} if tok else {}, {"hf_token": tok} if tok else {}, {}):
        try:
            client = Client(space, **kw)
            break
        except TypeError as e:
            last = e
        except Exception as e:
            last = e
            break
    if client is None:
        raise RuntimeError("اتصال به فضا ممکن نشد: %s" % last)

    if ins.get("mode", "inspect") == "inspect":
        info = client.view_api(return_format="dict", print_info=False)
        lines = []
        for group in ("named_endpoints", "unnamed_endpoints"):
            eps = info.get(group) or {}
            if eps:
                lines.append("[%s]" % group)
            for ep, d in eps.items():
                params = d.get("parameters") or []
                rets = d.get("returns") or []
                lines.append("  %s" % ep)
                for prm in params:
                    lines.append("      in  %-28s %-10s default=%s" % (
                        prm.get("parameter_name") or prm.get("label"),
                        (prm.get("python_type") or {}).get("type", "?"),
                        str(prm.get("parameter_default"))[:24]))
                for r in rets:
                    lines.append("      out %-28s %s" % (
                        r.get("label"), (r.get("python_type") or {}).get("type", "?")))
        return "امضای فشرده %s:\n%s" % (space, "\n".join(lines)[:3000]), []

    api_name = ins.get("api_name")
    args = ins.get("args") or []
    kwargs = ins.get("kwargs") or {}

    # {"__file": "frame.png"} یعنی فایلی که تسک قبلی در همین اجرا ساخته است.
    # این‌طور می‌شود «تصویر بساز، بعد همان را متحرک کن» را در یک اجرا انجام داد.
    def resolve(v):
        if isinstance(v, dict) and "__file" in v:
            path = os.path.join(OUT, v["__file"])
            if not os.path.exists(path):
                raise RuntimeError("فایل ورودی پیدا نشد: %s" % v["__file"])
            try:
                from gradio_client import handle_file
                return handle_file(path)
            except Exception:
                return path
        if isinstance(v, dict):
            return {k: resolve(x) for k, x in v.items()}
        if isinstance(v, list):
            return [resolve(x) for x in v]
        return v

    args = [resolve(a) for a in args]
    kwargs = {k: resolve(v) for k, v in kwargs.items()}
    result = client.predict(*args, api_name=api_name, **kwargs)

    def collect(r):
        paths = []
        if isinstance(r, str) and os.path.exists(r):
            paths.append(r)
        elif isinstance(r, dict):
            for v in r.values():
                paths += collect(v)
        elif isinstance(r, (list, tuple)):
            for v in r:
                paths += collect(v)
        return paths

    files = collect(result)
    arts = []
    for i, src in enumerate(files):
        ext = os.path.splitext(src)[1] or ".bin"
        name = ins.get("output") if len(files) == 1 and ins.get("output") \
            else "space_out_%d%s" % (i, ext)
        shutil.copy2(src, os.path.join(OUT, name))
        arts.append(name)
    if not arts:
        return "پاسخ بدون فایل: %s" % str(result)[:400], []
    total = sum(os.path.getsize(os.path.join(OUT, a)) for a in arts)
    return "فضا: %s\nفایل‌ها: %s\nحجم کل: %.1f KB\nزمان: %ss" % (
        space, ", ".join(arts), total / 1024, round(time.time() - t0, 1)), arts


MOTION_PRESETS = {
    # حرکت روی تصویر ثابت، بدون هیچ GPU. سهمیه نامحدود.
    "zoom_in":   "zoompan=z='min(zoom+0.0012,1.35)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
    "zoom_out":  "zoompan=z='if(lte(on,1),1.35,max(zoom-0.0012,1.0))':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
    "pan_right": "zoompan=z=1.2:d={frames}:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
    "pan_left":  "zoompan=z=1.2:d={frames}:x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
    "ken_burns": "zoompan=z='min(zoom+0.0010,1.25)':d={frames}:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
    "breathe":   "zoompan=z='1.05+0.05*sin(on/{frames}*2*PI)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}",
}


def run_motion(task, hosts):
    """ساخت حرکت روی تصویر ثابت با ffmpeg.

    چرا مهم است: سهمیه GPU گلوگاه ماست. خیلی از پلان‌ها حرکت پیچیده
    لازم ندارند و با زوم و پن آرام کاملاً قابل قبول می‌شوند.
    این مسیر روی CPU اجرا می‌شود، پس سهمیه‌اش نامحدود است.
    """
    ins = task["inputs"]
    preset = ins.get("preset", "ken_burns")
    if preset not in MOTION_PRESETS:
        raise ValueError("پریست ناشناخته: %s. موجود: %s" % (
            preset, ", ".join(MOTION_PRESETS)))
    src = ins.get("image_url")
    local = "src_image.png"
    if src:
        download(src, os.path.join(OUT, local), hosts)
    elif ins.get("image_file"):
        shutil.copy2(os.path.join(ROOT, ins["image_file"]), os.path.join(OUT, local))
    else:
        raise ValueError("نه image_url داده شده نه image_file")

    dur = float(ins.get("duration", 4))
    fps = int(ins.get("fps", 25))
    w = int(ins.get("width", 1024))
    h = int(ins.get("height", 1024))
    frames = max(int(dur * fps), 2)
    vf = MOTION_PRESETS[preset].format(frames=frames, w=w, h=h, fps=fps)
    # مقیاس بالا قبل از zoompan، وگرنه لرزش پله‌ای دیده می‌شود
    vf = "scale=%d:%d:flags=lanczos,%s" % (w * 3, h * 3, vf)
    out = ins.get("output", "motion.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", local,
           "-vf", vf, "-t", str(dur), "-c:v", "libx264", "-preset", "medium",
           "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps), out]
    t0 = time.time()
    code, o, e = sh(cmd, timeout=task.get("timeout_sec", 600))
    if code != 0:
        raise RuntimeError("ffmpeg شکست خورد: %s" % e[-500:])
    size = os.path.getsize(os.path.join(OUT, out))
    try:
        os.remove(os.path.join(OUT, local))
    except OSError:
        pass
    return ("پریست: %s\nمدت: %ss   %dx%d @ %sfps\nفایل: %s\nحجم: %.1f KB\n"
            "زمان ساخت: %ss\nسهمیه GPU مصرف‌شده: صفر" % (
                preset, dur, w, h, fps, out, size / 1024,
                round(time.time() - t0, 1))), [out]


def run_chain(task, hosts):
    """اجرای چند مرحله پشت سر هم در یک اجرا، با اشتراک پوشه خروجی.

    چرا لازم شد: هر تسک جدا در اجرای جدا می‌افتد و فایل بین‌شان منتقل نمی‌شود.
    زنجیره یعنی «تصویر بساز، بعد همان را متحرک کن» در یک تسک.

    ورودی:
        steps: فهرست مرحله‌ها، هر کدام مثل یک تسک عادی: {"type": ..., "inputs": {...}}
    """
    steps = task["inputs"].get("steps") or []
    if not steps:
        raise ValueError("زنجیره بدون مرحله")
    logs, arts = [], []
    for i, st in enumerate(steps, 1):
        stype = st.get("type")
        handler = HANDLERS.get(stype)
        if not handler or stype == "chain":
            raise ValueError("مرحله %d نوع نامعتبر دارد: %s" % (i, stype))
        sub = {"inputs": st.get("inputs") or {},
               "timeout_sec": st.get("timeout_sec", task.get("timeout_sec", 900))}
        logs.append("── مرحله %d: %s ──" % (i, stype))
        t0 = time.time()
        log, a = handler(sub, hosts)
        logs.append(log)
        logs.append("   زمان مرحله: %ss" % round(time.time() - t0, 1))
        arts += a
    return "\n".join(logs), arts


HANDLERS = {"probe": run_probe, "fetch": run_fetch,
            "ffmpeg": run_ffmpeg, "assemble": run_assemble, "hf": run_hf,
            "cf": run_cf, "keycheck": run_keycheck, "poll": run_poll,
            "space": run_space, "motion": run_motion, "chain": run_chain}


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
