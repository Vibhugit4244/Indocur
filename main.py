
import os
import re
import json
import time
import shutil
import subprocess
import tempfile
import requests
import xml.etree.ElementTree as ET

from pathlib import Path
from urllib.parse import urlparse
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from youtube_transcript_api import YouTubeTranscriptApi

CHANNEL_URL = os.getenv(
    "YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@IndoLogus"
)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path("state/state.json")
OUTPUT_DIR = Path("output")
MAX_VIDEOS = int(os.getenv("MAX_VIDEOS_PER_RUN", "2"))

RSS_NS = {"yt": "http://www.youtube.com/xml/schemas/2015",
          "atom": "http://www.w3.org/2005/Atom"}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; LectureMonitor/1.0)"
})


def telegram(message, document=None):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return

    base = f"https://api.telegram.org/bot{BOT_TOKEN}/"

    try:
        if document:
            with open(document, "rb") as f:
                r = session.post(
                    base + "sendDocument",
                    data={"chat_id": CHAT_ID, "caption": message[:1024]},
                    files={"document": f},
                    timeout=120
                )
        else:
            r = session.post(
                base + "sendMessage",
                json={"chat_id": CHAT_ID, "text": message[:4000]},
                timeout=30
            )
        r.raise_for_status()
        if not r.json().get("ok"):
            print("Telegram API error:", r.text[:500])
    except Exception as exc:
        print("Telegram delivery error:", exc)


def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"processed": [], "pending": []}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


def get_channel_id():
    """Resolve the public channel page without yt-dlp."""
    r = session.get(CHANNEL_URL, timeout=30)
    r.raise_for_status()

    patterns = [
        r'"channelId"\s*:\s*"(UC[\w-]{22})"',
        r'"externalId"\s*:\s*"(UC[\w-]{22})"',
        r'<meta itemprop="channelId" content="(UC[\w-]{22})"',
    ]

    for pattern in patterns:
        match = re.search(pattern, r.text)
        if match:
            return match.group(1)

    raise RuntimeError(
        "Could not resolve channel ID from the public channel page. "
        "YouTube may have returned a consent or bot-check page."
    )


def get_new_videos():
    channel_id = get_channel_id()
    feed_url = (
        "https://www.youtube.com/feeds/videos.xml"
        f"?channel_id={channel_id}"
    )
    r = session.get(feed_url, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.content)
    videos = []

    for entry in root.findall("atom:entry", RSS_NS):
        video_id = entry.findtext("yt:videoId", namespaces=RSS_NS)
        title = entry.findtext("atom:title", default="Untitled",
                               namespaces=RSS_NS)
        link = entry.find("atom:link", RSS_NS)
        url = (
            link.attrib.get("href", "")
            if link is not None
            else f"https://www.youtube.com/watch?v={video_id}"
        )

        if video_id:
            videos.append({
                "id": video_id,
                "title": title,
                "url": url
            })

    return videos


def get_transcript(video_id):
    """Try captions; failure does not stop monitoring."""
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        lines = []

        for item in transcript:
            start = float(item.start)
            stamp = (
                f"{int(start // 3600):02}:"
                f"{int((start % 3600) // 60):02}:"
                f"{int(start % 60):02}"
            )
            lines.append((stamp, item.text))

        return lines, None
    except Exception as exc:
        return [], str(exc)[:500]


def capture_slides(video_url, folder):
    """
    Best-effort only. YouTube may block downloading.
    Returns slide image paths and an explanatory error.
    """
    if not shutil.which("ffmpeg"):
        return [], "FFmpeg is not installed."

    try:
        import yt_dlp

        video_file = str(folder / "lecture.mp4")
        options = {
            "format": "best[height<=720]/best",
            "outtmpl": video_file,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 1,
            "socket_timeout": 25,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([video_url])

        if not Path(video_file).exists():
            return [], "Video download did not produce a file."

        frames_dir = folder / "slides"
        frames_dir.mkdir(exist_ok=True)

        # Sample at most one frame every 10 seconds, then use
        # FFmpeg scene detection to retain changing presentation frames.
        pattern = str(frames_dir / "slide-%04d.jpg")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", video_file,
            "-vf",
            "fps=1/10,select='gt(scene,0.18)',"
            "scale=1280:-2",
            "-vsync", "vfr",
            "-q:v", "3",
            pattern
        ]

        subprocess.run(
            command, check=True, timeout=1800,
            capture_output=True, text=True
        )

        images = sorted(frames_dir.glob("slide-*.jpg"))

        # Delete the downloaded video even if frame extraction worked.
        Path(video_file).unlink(missing_ok=True)
        return images, None

    except Exception as exc:
        return [], (
            "Video download or slide extraction failed: "
            + str(exc)[:700]
        )


def create_pdf(video, transcript, transcript_error, slides, folder):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", video["title"])[:70]
    pdf_path = OUTPUT_DIR / f"{safe_title}_{video['id']}.pdf"

    styles = getSampleStyleSheet()
    story = [
        Paragraph(video["title"], styles["Title"]),
        Spacer(1, 12),
        Paragraph(
            f'<link href="{video["url"]}">Open original YouTube video</link>',
            styles["Normal"]
        ),
        Spacer(1, 18),
        Paragraph("LECTURE TRANSCRIPT", styles["Heading1"]),
        Spacer(1, 8),
    ]

    if transcript:
        for timestamp, text in transcript:
            safe_text = (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            story.append(Paragraph(
                f"<b>[{timestamp}]</b> {safe_text}",
                styles["BodyText"]
            ))
            story.append(Spacer(1, 5))
    else:
        story.append(Paragraph(
            "Transcript unavailable. " +
            (transcript_error or "No captions were returned."),
            styles["BodyText"]
        ))

    if slides:
        story.append(PageBreak())
        story.append(Paragraph("PRESENTATION FRAMES",
                               styles["Heading1"]))
        story.append(Paragraph(
            "Frames sampled from the video; not guaranteed to include "
            "every slide.",
            styles["Italic"]
        ))

        for image_path in slides:
            story.append(Spacer(1, 10))
            story.append(Image(
                str(image_path), width=6.5 * inch,
                height=4.1 * inch, kind="proportional"
            ))
            story.append(Spacer(1, 8))

    SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        rightMargin=40, leftMargin=40,
        topMargin=40, bottomMargin=40
    ).build(story)

    return pdf_path


def process_video(video):
    print("Processing:", video["title"], video["id"])

    with tempfile.TemporaryDirectory(prefix="indologus-") as temp:
        folder = Path(temp)
        transcript, transcript_error = get_transcript(video["id"])
        slides, slide_error = capture_slides(video["url"], folder)

        pdf = create_pdf(
            video, transcript, transcript_error, slides, folder
        )

        status = (
            f"Lecture: {video['title']}\n"
            f"Transcript: {'available' if transcript else 'unavailable'}\n"
            f"Presentation frames: {len(slides)}\n"
            f"Video: {video['url']}"
        )

        if slide_error:
            status += "\nSlide extraction note: " + slide_error[:500]

        telegram(status, str(pdf))


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

    state = load_state()
    processed = set(state.get("processed", []))

    try:
        videos = get_new_videos()
    except Exception as exc:
        telegram(
            "IndoLogus monitor could not read the channel feed:\n"
            + str(exc)[:1000]
        )
        raise

    # RSS returns recent uploads; process oldest first.
    new_videos = [
        v for v in reversed(videos)
        if v["id"] not in processed
    ]

    if not new_videos:
        print("No new videos.")
        return

    for video in new_videos[:MAX_VIDEOS]:
        try:
            process_video(video)
        except Exception as exc:
            print("Video processing error:", exc)
            telegram(
                f"IndoLogus processing failed:\n{video['title']}\n"
                f"{video['url']}\n{str(exc)[:700]}"
            )
            # Do not mark failed videos as processed.
            continue

        processed.add(video["id"])
        state["processed"] = sorted(processed)
        save_state(state)

    print("Monitoring run complete.")


if __name__ == "__main__":
    main()
