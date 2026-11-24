"""Kai Cenat clip robot, stage 1.

Finds a recent Twitch clip of Kai Cenat, makes it vertical with hooky
captions, and sends it to Telegram for you to look at.
No Twitch API key needed: it reads the public clips page with yt-dlp.
"""
import json, os, re, subprocess, sys, urllib.request, urllib.parse, uuid
from pathlib import Path

CHANNEL = os.environ.get("TWITCH_CHANNEL", "kaicenat")
CLIPS_URL = f"https://www.twitch.tv/{CHANNEL}/clips?filter=clips&range=7d"
MAX_CLIPS = int(os.environ.get("MAX_CLIPS", "2"))
STATE = Path("posted.json")
WORK = Path("work")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else []


def save_state(ids):
    STATE.write_text(json.dumps(ids[-500:], indent=1))


def find_clips():
    out = run(["yt-dlp", "--flat-playlist", "--dump-json",
               "--playlist-end", "30", CLIPS_URL]).stdout
    clips = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        clips.append({"id": d.get("id"), "url": d.get("url") or d.get("webpage_url"),
                      "title": d.get("title", ""), "views": d.get("view_count") or 0})
    clips.sort(key=lambda c: c["views"], reverse=True)
    return clips


def download(clip, folder):
    raw = folder / "raw.mp4"
    run(["yt-dlp", "-f", "best[ext=mp4]/best", "-o", str(raw), clip["url"]])
    return raw


def transcribe(video):
    from faster_whisper import WhisperModel
    model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), compute_type="int8")
    segs, _ = model.transcribe(str(video), word_timestamps=True)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append((w.start, w.end, w.word.strip()))
    return words


def make_hook(text, title):
    """One short line that says what is actually happening, not just 'omo'."""
    fallback = (title or "Kai Cenat has the chat in shambles").strip()[:70]
    if not ANTHROPIC_KEY or len(text) < 20:
        return fallback
    body = json.dumps({
        "model": "claude-haiku-5-5", "max_tokens": 60,
        "messages": [{"role": "user", "content":
            "Write ONE punchy hook line (max 9 words, no hashtags, no quotes) for a "
            "short vertical clip of the streamer Kai Cenat. It must say what is "
            "actually happening, so a stranger wants to keep watching. "
            f"Clip title: {title}\nTranscript: {text[:1500]}"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)["content"][0]["text"].strip().strip('"')
    except Exception as e:
        print("hook fallback:", e)
        return fallback


def ts(t):
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(words, hook, path):
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
            "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,"
            "Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,"
            "Encoding\n"
            "Style: Cap,Arial,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,60,60,520,1\n"
            "Style: Hook,Arial,70,&H0000D7FF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,8,60,60,260,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = [f"Dialogue: 0,{ts(0)},{ts(4)},Hook,,0,0,0,,{hook.upper()}"]
    for i in range(0, len(words), 3):
        grp = words[i:i + 3]
        txt = " ".join(w[2] for w in grp).upper().replace("\n", " ")
        lines.append(f"Dialogue: 0,{ts(grp[0][0])},{ts(grp[-1][1])},Cap,,0,0,0,,{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def render(raw, ass, out):
    vf = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920,boxblur=30:5[bg];[b]scale=1080:-2[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,subtitles={ass.name}[v]")
    run(["ffmpeg", "-y", "-i", raw.name, "-filter_complex", vf, "-map", "[v]",
         "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-movflags", "+faststart", out.name], cwd=raw.parent)


def send_telegram(video, caption):
    if not (TG_TOKEN and TG_CHAT):
        print("No Telegram secrets, skipping send.")
        return
    b = uuid.uuid4().hex
    parts = []
    for k, v in {"chat_id": TG_CHAT, "caption": caption[:1000]}.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{b}\r\nContent-Disposition: form-data; name="video"; '
                  f'filename="clip.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode())
    parts.append(video.read_bytes())
    parts.append(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                                 b"".join(parts),
                                 {"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=300).read()


def main():
    WORK.mkdir(exist_ok=True)
    done = load_state()
    todo = [c for c in find_clips() if c["id"] not in done][:MAX_CLIPS]
    print(f"{len(todo)} new clip(s) to process")
    for clip in todo:
        folder = WORK / re.sub(r"\W+", "_", clip["id"])
        folder.mkdir(exist_ok=True)
        try:
            raw = download(clip, folder)
            words = transcribe(raw)
            text = " ".join(w[2] for w in words)
            hook = make_hook(text, clip["title"])
            ass = folder / "caps.ass"
            write_ass(words or [(0, 2, "")], hook, ass)
            out = folder / "final.mp4"
            render(raw, ass, out)
            send_telegram(out, f"{hook}\n\n#KaiCenat #Streamer #Clips")
            done.append(clip["id"])
            print("done:", clip["id"], "|", hook)
        except Exception as e:
            print("FAILED", clip["id"], e, file=sys.stderr)
    save_state(done)


if __name__ == "__main__":
    main()
