#!/usr/bin/env python3
"""ClipBlaster server-side Pro cutter — runs on a GitHub Actions runner.

One run handles ONE job:
  * manual (workflow_dispatch): job comes from env INPUT_YOUTUBE_URL etc.
  * scheduled: the script fetches the oldest queued job from the Worker
    queue at /api/shorts/next (the Worker marks it claimed).

Steps: yt-dlp download -> ffprobe duration -> pick clip starts ->
render hook PNG (PIL) -> ffmpeg Pro chain per clip (720x1280, slow
zoom + mid punch, fade, style colour, vignette, hook overlay, loudnorm)
-> outputs/<job_id>/short-N.mp4 + done.json.

done.json is the completion signal: the site polls it from Pages.
On any failure it writes done.json with status "error" instead.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

WORKER = "https://clipblaster-api.nayeemahmed33000.workers.dev"
PAGES_BASE = "https://nayeemahmed33000-maker.github.io/clipblaster"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(REPO_ROOT, "outputs")

STYLES = {
    "vibrant": {"sat": 1.45, "con": 1.12, "vig": 6},
    "cinematic": {"sat": 1.15, "con": 1.10, "vig": 5},
    "soft": {"sat": 1.10, "con": 1.03, "vig": 4},
}
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BENGALI_BOLD = os.path.join(REPO_ROOT, "fonts", "NotoSansBengali-Bold.ttf")


def log(msg):
    print(f"[make-shorts] {msg}", flush=True)


def run(cmd, timeout=900):
    log("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"command failed ({r.returncode}): {' '.join(map(str, cmd))}\n"
            f"stdout: {r.stdout[-1500:]}\nstderr: {r.stderr[-1500:]}"
        )
    return r


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "clipblaster-actions"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_job():
    """Manual inputs win; otherwise take the oldest queued job."""
    url = os.environ.get("INPUT_YOUTUBE_URL", "").strip()
    if url:
        job_id = os.environ.get("INPUT_JOB_ID") or f"job_manual_{os.environ.get('GITHUB_RUN_ID', '0')}"
        return {
            "id": job_id,
            "youtube_url": url,
            "clips": int(os.environ.get("INPUT_CLIPS", "1")),
            "duration": int(os.environ.get("INPUT_DURATION", "30")),
            "style": os.environ.get("INPUT_STYLE", "vibrant"),
            "hook": os.environ.get("INPUT_HOOK", ""),
        }
    data = http_get_json(WORKER + "/api/shorts/next")
    return data.get("job")


def video_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(r.stdout.strip())


def pick_starts(video_len, clip_len, n):
    """Spread clip starts across the video; first clip near the hook."""
    margin = 5.0
    if n == 1:
        return [margin]
    span = max(video_len - clip_len - 2 * margin, 0.0)
    if span <= 0:
        return [margin] * n
    return [margin + (span * i / (n - 1)) for i in range(n)]


def has_bengali(text):
    return any("\u0980" <= ch <= "\u09ff" for ch in text)


def render_hook_png(hook, out_path):
    """White bold text + black stroke on a transparent 720-wide canvas."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    font_path = BENGALI_BOLD if has_bengali(hook) else DEJAVU_BOLD
    size = 64
    font = ImageFont.truetype(font_path, size)
    # Greedy word wrap to fit 640px.
    words = hook.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= 640 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:3]
    line_h = int(size * 1.25)
    pad = 24
    height = len(lines) * line_h + pad * 2
    img = Image.new("RGBA", (720, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        w = font.getlength(line)
        draw.text(((720 - w) / 2, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=4, stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(out_path)
    log(f"hook PNG: {len(lines)} line(s), {os.path.getsize(out_path)} bytes")


def has_audio(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return bool(r.stdout.strip())


def cut_clip(src, start, clip_len, style, hook_png, out_path):
    """Pro chain, mirroring the live site's Pro v2 cut."""
    st = STYLES[style]
    mid_on = round(clip_len * 15)  # frame index at the clip midpoint (30fps)
    zoom = f"min(zoom+0.0015,1.18)+if(between(on,{mid_on},{mid_on + 15}),0.08,0)"
    vf = (
        "[0:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,"
        f"zoompan=z='{zoom}':d=1:s=720x1280:fps=30,"
        "fade=t=in:st=0:d=0.5,"
        f"fade=t=out:st={clip_len - 0.5:.2f}:d=0.5,"
        f"eq=saturation={st['sat']}:contrast={st['con']},"
        f"vignette=PI/{st['vig']}[base];"
        "movie=" + hook_png.replace(":", "\\:") + "[hook];"
        "[base][hook]overlay=(W-w)/2:H*0.08:enable='between(t,0,3)'[v]"
    )
    af = "loudnorm=I=-16:TP=-1.5:LRA=11:linear=true"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{clip_len:.2f}",
        "-i", src,
        "-filter_complex", vf,
        "-map", "[v]",
    ]
    if has_audio(src):
        cmd += ["-map", "0:a", "-af", af]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", f"{clip_len:.2f}",
        out_path,
    ]
    run(cmd)


def write_done(job_dir, payload):
    with open(os.path.join(job_dir, "done.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def cleanup_old_outputs(keep_days=7, keep_newest=20):
    """Drop job dirs older than keep_days (done.json created_at)."""
    if not os.path.isdir(OUT_ROOT):
        return
    now = time.time()
    entries = []
    for name in os.listdir(OUT_ROOT):
        d = os.path.join(OUT_ROOT, name)
        if not os.path.isdir(d):
            continue
        created = None
        try:
            with open(os.path.join(d, "done.json"), encoding="utf-8") as fh:
                created = json.loads(fh.read()).get("created_at")
        except Exception:
            pass
        entries.append((created or 0, name, d))
    entries.sort(reverse=True)
    for created, name, d in entries:
        age_days = (now - created) / 86400 if created else 0
        if age_days > keep_days or len(entries) > keep_newest:
            log(f"cleanup: removing old outputs/{name}")
            shutil.rmtree(d, ignore_errors=True)
            entries.remove((created, name, d))


def main():
    job = fetch_job()
    if not job:
        log("queue empty — nothing to do")
        return 0
    job_id = job["id"]
    job_dir = os.path.join(OUT_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    log(f"job {job_id}: {job['youtube_url']} clips={job['clips']} "
        f"dur={job['duration']} style={job['style']} hook={job.get('hook')!r}")
    try:
        cleanup_old_outputs()
        src = os.path.join(job_dir, "source.mp4")
        run([
            "yt-dlp", "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "--merge-output-format", "mp4", "--no-playlist",
            "-o", src, job["youtube_url"],
        ])
        total = video_duration(src)
        log(f"source duration: {total:.1f}s")
        if total < 10:
            raise RuntimeError(f"video too short ({total:.1f}s)")
        clip_len = min(int(job["duration"]), int(total) - 6)
        if clip_len < 10:
            clip_len = max(int(total) - 6, 10)
        n = max(1, min(int(job["clips"]), 5))
        starts = pick_starts(total, clip_len, n)
        hook = (job.get("hook") or "").strip()
        hook_png = os.path.join(job_dir, "hook.png")
        if not hook:
            hook = "WAIT FOR THE END"
        render_hook_png(hook, hook_png)
        files = []
        for i, start in enumerate(starts):
            out_path = os.path.join(job_dir, f"short-{i + 1}.mp4")
            cut_clip(src, start, clip_len, job["style"], hook_png, out_path)
            size = os.path.getsize(out_path)
            log(f"short {i + 1}: start={start:.1f}s size={size} bytes")
            files.append({
                "name": f"short-{i + 1}.mp4",
                "url": f"{PAGES_BASE}/outputs/{job_id}/short-{i + 1}.mp4",
                "size": size,
                "start": round(start, 1),
                "duration": clip_len,
            })
        if os.path.exists(src):
            os.remove(src)
        if os.path.exists(hook_png):
            os.remove(hook_png)
        write_done(job_dir, {
            "status": "done",
            "job_id": job_id,
            "video_id": job.get("video_id"),
            "created_at": int(time.time()),
            "files": files,
        })
        log(f"DONE: {len(files)} Short(s) for {job_id}")
        return 0
    except Exception as exc:  # noqa: BLE001 - report any failure honestly
        log(f"ERROR: {exc}")
        write_done(job_dir, {
            "status": "error",
            "job_id": job_id,
            "video_id": job.get("video_id"),
            "created_at": int(time.time()),
            "error": str(exc)[:500],
        })
        return 0  # done.json carries the error; the run itself stays green


if __name__ == "__main__":
    sys.exit(main())
