# ساختار تسک

هر تسک یک فایل JSON در `tasks/queue/` است. نام فایل:

```text
<YYYYmmdd-HHMMSS>-<type>-<slug>.json
```

## فیلدها

```json
{
  "id": "20260819-210000-probe-access",
  "type": "probe | fetch | ffmpeg | assemble | shell",
  "title": "توضیح کوتاه فارسی",
  "created_by": "iran-server | local | bot",
  "created_at": "2026-08-19 21:00:00 UTC",
  "priority": "first | normal | low",
  "inputs": {},
  "expect": "چه چیزی نتیجه موفق است",
  "timeout_sec": 600
}
```

## انواع تسک در فاز یک

### probe
بررسی دسترسی از رانر گیت‌هاب که خارج از ایران است.

```json
{ "type": "probe", "inputs": { "urls": ["https://huggingface.co/"] } }
```

### fetch
دریافت یک فایل از دامنه مجاز و ذخیره به‌عنوان خروجی.

```json
{ "type": "fetch", "inputs": { "url": "https://...", "filename": "out.bin" } }
```

فقط دامنه‌های داخل `runner/allowlist.txt` مجازند. این یک پل کنترلی است،
نه پروکسی عمومی. ترافیک کاربر نهایی از آن عبور نمی‌کند.

### ffmpeg
اجرای فرمان ffmpeg روی فایل‌های دریافتی.

```json
{
  "type": "ffmpeg",
  "inputs": {
    "downloads": [ { "url": "https://...", "as": "a.mp4" } ],
    "args": ["-i", "a.mp4", "-t", "3", "out.mp4"],
    "output": "out.mp4"
  }
}
```

### assemble
چیدمان چند کلیپ پشت هم.

```json
{
  "type": "assemble",
  "inputs": {
    "clips": ["https://.../s01.mp4", "https://.../s02.mp4"],
    "output": "final.mp4",
    "fps": 24
  }
}
```

## نتیجه

نتیجه در `tasks/done/<id>.json` نوشته می‌شود:

```json
{
  "id": "...",
  "status": "success | failed",
  "started_at": "...",
  "finished_at": "...",
  "duration_sec": 12,
  "runner": "github-actions",
  "log": "خلاصه خروجی",
  "artifacts": ["نام فایل‌های خروجی"]
}
```

فایل‌های خروجی به‌عنوان Artifact همان اجرا آپلود می‌شوند.

## قانون امنیت

```text
هیچ Token، کلید یا داده شخصی داخل تسک نوشته نمی‌شود
مخزن عمومی است، پس هر چیزی که اینجاست عمومی است
عکس و ویدیوی افراد واقعی هرگز اینجا نمی‌آید
```
