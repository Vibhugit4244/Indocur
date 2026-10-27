#!/usr/bin/env python3
"""IndoLogus YouTube -> transcript + unique slide screenshots -> Telegram PDF.

Use only with videos you are authorized to download/process, and comply with
YouTube's terms and applicable copyright law. Temporary media is deleted after
processing; only the PDF and state metadata remain.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imagehash
import requests
import yt_dlp
from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image as RLImage, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, KeepTogether,
)

ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.getenv("STATE_PATH", ROOT / "state" / "state.json"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", ROOT / "output"))
CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@IndoLogus").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MAX_VIDEOS_PER_RUN = max(1, int(os.getenv("MAX_VIDEOS_PER_RUN", "2")))
MAX_CHANNEL_SCAN = max(10, int(os.getenv("MAX_CHANNEL_SCAN", "30")))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("indologus")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

@dataclass
class Video:
    video_id: str
    title: str
    url: str
    upload_date: str = ""
    duration: int = 0


def load_state() -> dict[str, Any]:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return {"version": 1, "processed": {}, "failures": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        data.setdefault("processed", {})
        data.setdefault("failures", {})
        data.setdefault("version", 1)
        return data
    except Exception as exc:
        # Do not silently overwrite corrupted history and reprocess everything.
        backup = STATE_PATH.with_suffix(f".corrupt-{int(time.time())}.json")
        shutil.copy2(STATE_PATH, backup)
        raise RuntimeError(f"Could not read state file; backup saved to {backup}: {exc}") from exc


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_channel_videos() -> list[Video]:
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "playlistend": MAX_CHANNEL_SCAN, "ignoreerrors": True,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(CHANNEL_URL, download=False)
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None and isinstance(info, dict) and info.get("id"):
        entries = [info]
    videos: list[Video] = []
    seen: set[str] = set()
    for item in entries or []:
        if not item or not item.get("id"):
            continue
        vid = str(item["id"])
        if vid in seen:
            continue
        seen.add(vid)
        url = item.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        videos.append(Video(
            video_id=vid,
            title=str(item.get("title") or f"YouTube video {vid}"),
            url=str(item.get("webpage_url") or url),
            upload_date=str(item.get("upload_date") or ""),
            duration=int(item.get("duration") or 0),
        ))
    # Process older unseen videos first, so a burst of uploads does not starve older ones.
    videos.sort(key=lambda v: (v.upload_date or "99999999", v.video_id))
    return videos


def safe_filename(text: str, max_len: int = 100) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "", text).strip().replace(" ", "_")
    text = re.sub(r"_+", "_", text).strip("._")
    return (text[:max_len] or "youtube_video")


def run_command(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    log.debug("Running command: %s", " ".join(args))
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)


def download_video_and_subtitles(video: Video, workdir: Path) -> tuple[Path, Path | None, dict[str, Any]]:
    """Download a temporary video and try official/manual/auto captions first."""
    outtmpl = str(workdir / "video.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en.*", "hi", "hi.*"],
        "subtitlesformat": "vtt/best",
        "skip_download": False,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video.url, download=True)
    # yt-dlp may choose a different extension/container; find the largest media file.
    media_candidates = [p for p in workdir.glob("video.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
    if not media_candidates:
        raise RuntimeError("yt-dlp completed but no video file was found")
    media = max(media_candidates, key=lambda p: p.stat().st_size)
    subtitle_candidates = list(workdir.glob("video*.vtt")) + list(workdir.glob("*.vtt"))
    subtitle = max(subtitle_candidates, key=lambda p: p.stat().st_size) if subtitle_candidates else None
    return media, subtitle, info if isinstance(info, dict) else {}


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    """Convert VTT cue blocks into timestamped readable transcript segments."""
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    blocks = re.split(r"\n\s*\n", raw)
    cues: list[tuple[float, str]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        stamp = lines[timing_index].split("-->", 1)[0].strip().split(" ")[0]
        seconds = timestamp_seconds(stamp)
        text_lines = lines[timing_index + 1:]
        cleaned = []
        for line in text_lines:
            line = re.sub(r"<[^>]+>", "", line)
            line = html.unescape(line).strip()
            if line and not re.fullmatch(r"\[.*\]", line):
                cleaned.append(line)
        text = " ".join(cleaned)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            cues.append((seconds, text))
    # Auto captions often repeat overlapping words. Remove adjacent identical cues.
    deduped: list[tuple[float, str]] = []
    for sec, text in cues:
        if deduped and deduped[-1][1].casefold() == text.casefold():
            continue
        deduped.append((sec, text))
    return deduped


def timestamp_seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    try:
        parts = [float(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except ValueError:
        pass
    return 0.0


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcribe_with_whisper(video_path: Path) -> list[tuple[float, str]]:
    log.info("No usable captions found; starting faster-whisper model '%s'", WHISPER_MODEL)
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(video_path), beam_size=3, vad_filter=True)
    result = []
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.text or "").strip()
        if text:
            result.append((float(segment.start), text))
    return result


def extract_unique_slides(video_path: Path, workdir: Path) -> list[tuple[Path, float]]:
    """Sample one frame/sec and keep perceptually distinct frames.

    This is an automated approximation: motion backgrounds, animations, or tiny
    slide changes may still need manual review. It deliberately favors fewer,
    clearer screenshots over dozens of near-duplicates.
    """
    frames_dir = workdir / "slides"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # Decode at 1 fps, scale down for comparison, retain the original-sized sampled frame.
    sample_pattern = frames_dir / "sample-%06d.jpg"
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
        "-vf", "fps=1,scale=1280:-2:force_original_aspect_ratio=decrease",
        "-q:v", "3", "-y", str(sample_pattern),
    ], timeout=60 * 60 * 3)
    samples = sorted(frames_dir.glob("sample-*.jpg"))
    if not samples:
        raise RuntimeError("ffmpeg extracted no frames")

    kept: list[tuple[Path, float, Any]] = []
    previous_hash = None
    previous_kept_second = -999
    # pHash on the full frame; a moderate threshold reduces near-identical frames.
    for index, frame_path in enumerate(samples):
        second = float(index)
        try:
            with Image.open(frame_path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                ph = imagehash.phash(im, hash_size=8)
        except Exception as exc:
            log.warning("Skipping unreadable sampled frame %s: %s", frame_path.name, exc)
            frame_path.unlink(missing_ok=True)
            continue
        distance = 64 if previous_hash is None else ph - previous_hash
        if previous_hash is None or (distance >= 7 and second - previous_kept_second >= 2):
            kept.append((frame_path, second, ph))
            previous_hash = ph
            previous_kept_second = second
        else:
            frame_path.unlink(missing_ok=True)
    # Rename sequentially for stable PDF ordering and retain timestamps.
    result: list[tuple[Path, float]] = []
    for idx, (path, sec, _ph) in enumerate(kept, start=1):
        new_path = frames_dir / f"slide-{idx:04d}-{int(sec):06d}s.jpg"
        if path != new_path:
            path.rename(new_path)
        result.append((new_path, sec))
    log.info("Kept %d distinct-looking frames from %d one-second samples", len(result), len(samples))
    return result


def build_pdf(video: Video, info: dict[str, Any], transcript: list[tuple[float, str]], slides: list[tuple[Path, float]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page_size = A4
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="DocTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=23, alignment=TA_LEFT, textColor=colors.HexColor("#16233a"), spaceAfter=8))
    styles.add(ParagraphStyle(name="SectionHeading", parent=styles["Heading1"], fontSize=15, leading=19, textColor=colors.HexColor("#16233a"), spaceBefore=8, spaceAfter=10))
    styles.add(ParagraphStyle(name="Transcript", parent=styles["BodyText"], fontSize=9.2, leading=13, spaceAfter=5, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="SmallMeta", parent=styles["BodyText"], fontSize=8, leading=11, textColor=colors.HexColor("#505b6b"), spaceAfter=4))
    styles.add(ParagraphStyle(name="SlideLabel", parent=styles["BodyText"], fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#505b6b")))

    def page_footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#d7dce4"))
        canvas.line(16 * mm, 14 * mm, page_size[0] - 16 * mm, 14 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#657084"))
        canvas.drawString(16 * mm, 9 * mm, "IndoLogus Lecture Notes")
        canvas.drawRightString(page_size[0] - 16 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = BaseDocTemplate(str(out_path), pagesize=page_size, rightMargin=17 * mm, leftMargin=17 * mm, topMargin=16 * mm, bottomMargin=19 * mm, title=video.title[:200], author="IndoLogus Automation")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=page_footer)])
    story = [Paragraph(html.escape(video.title), styles["DocTitle"]), Spacer(1, 2 * mm)]
    published = video.upload_date or str(info.get("upload_date") or "Unknown")
    duration = int(info.get("duration") or video.duration or 0)
    meta = [
        [Paragraph("<b>Channel</b>", styles["SmallMeta"]), Paragraph("IndoLogus", styles["SmallMeta"])],
        [Paragraph("<b>Published</b>", styles["SmallMeta"]), Paragraph(html.escape(published), styles["SmallMeta"])],
        [Paragraph("<b>Duration</b>", styles["SmallMeta"]), Paragraph(html.escape(format_time(duration)) if duration else "Unknown", styles["SmallMeta"])],
        [Paragraph("<b>Video</b>", styles["SmallMeta"]), Paragraph(f'<link href="{html.escape(video.url, quote=True)}" color="#245bb5">Open on YouTube</link>', styles["SmallMeta"])],
        [Paragraph("<b>Generated</b>", styles["SmallMeta"]), Paragraph(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), styles["SmallMeta"])],
    ]
    table = Table(meta, colWidths=[28 * mm, 140 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f7fa")),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d7dce4")), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e6ec")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [table, Spacer(1, 6 * mm), Paragraph("Transcript", styles["SectionHeading"])]
    if transcript:
        for sec, text in transcript:
            safe_text = html.escape(text).replace("\n", " ")
            story.append(Paragraph(f'<font color="#245bb5"><b>[{format_time(sec)}]</b></font>  {safe_text}', styles["Transcript"]))
    else:
        story.append(Paragraph("No transcript was available or generated for this video.", styles["Transcript"]))
    story += [PageBreak(), Paragraph("Presentation screenshots", styles["SectionHeading"]), Paragraph(f"{len(slides)} distinct-looking frames extracted automatically. Please review slides with animations or small text changes.", styles["SmallMeta"]), Spacer(1, 3 * mm)]

    # Slides each occupy a landscape page to maximize readability.
    for idx, (path, sec) in enumerate(slides, start=1):
        story.append(Paragraph(f"Slide/frame {idx} · {format_time(sec)}", styles["SlideLabel"]))
        story.append(Spacer(1, 2 * mm))
        with Image.open(path) as im:
            iw, ih = im.size
        max_w = 175 * mm
        max_h = 235 * mm
        scale = min(max_w / iw, max_h / ih)
        story.append(RLImage(str(path), width=iw * scale, height=ih * scale, hAlign="CENTER"))
        if idx < len(slides):
            story.append(PageBreak())
    doc.build(story)


def send_telegram(pdf_path: Path, video: Video, slide_count: int, transcript_count: int) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable")
    size = pdf_path.stat().st_size
    # Telegram Bot API document upload limit for the hosted Bot API is 50 MB.
    if size > 49 * 1024 * 1024:
        raise RuntimeError(f"PDF is {size / 1024 / 1024:.1f} MB, above the safe 49 MB Telegram upload limit")
    endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    caption = f"📚 IndoLogus lecture processed\n{video.title}\nTranscript segments: {transcript_count}\nSlide screenshots: {slide_count}\n{video.url}"
    with pdf_path.open("rb") as handle:
        response = requests.post(endpoint, data={"chat_id": CHAT_ID, "caption": caption[:1024]}, files={"document": (pdf_path.name, handle, "application/pdf")}, timeout=(30, 300))
    try:
        payload = response.json()
    except Exception:
        payload = {"ok": False, "description": response.text[:500]}
    if response.status_code != 200 or not payload.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed (HTTP {response.status_code}): {payload.get('description', 'unknown error')}")
    log.info("Sent %s to Telegram", pdf_path.name)


def process_video(video: Video) -> tuple[Path, int, int]:
    log.info("Processing video: %s (%s)", video.title, video.video_id)
    with tempfile.TemporaryDirectory(prefix="indologus-") as tmp:
        workdir = Path(tmp)
        video_path, subtitle_path, info = download_video_and_subtitles(video, workdir)
        transcript = parse_vtt(subtitle_path) if subtitle_path else []
        if not transcript:
            transcript = transcribe_with_whisper(video_path)
        slides = extract_unique_slides(video_path, workdir)
        filename = f"{safe_filename(video.title)}_{video.video_id}.pdf"
        output_path = OUTPUT_DIR / filename
        build_pdf(video, info, transcript, slides, output_path)
        # Upload while screenshots still exist only in temp storage; PDF is the deliverable.
        send_telegram(output_path, video, len(slides), len(transcript))
        # TemporaryDirectory removes the downloaded video and extracted images automatically.
        return output_path, len(slides), len(transcript)


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running.")
        return 2
    state = load_state()
    processed: dict[str, Any] = state["processed"]
    failures: dict[str, Any] = state["failures"]
    try:
        videos = get_channel_videos()
    except Exception as exc:
        log.exception("Could not scan channel %s", CHANNEL_URL)
        state["last_scan_error"] = {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        save_state(state)
        return 1
    unseen = [v for v in videos if v.video_id not in processed]
    if not unseen:
        log.info("No new videos found. Scanned %d channel entries.", len(videos))
        state["last_successful_scan_at"] = datetime.now(timezone.utc).isoformat()
        state.pop("last_scan_error", None)
        save_state(state)
        return 0
    log.info("Found %d unseen video(s); this run will process up to %d.", len(unseen), MAX_VIDEOS_PER_RUN)
    any_failure = False
    for video in unseen[:MAX_VIDEOS_PER_RUN]:
        try:
            pdf_path, slide_count, transcript_count = process_video(video)
            processed[video.video_id] = {
                "title": video.title, "url": video.url,
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "pdf_name": pdf_path.name, "slides": slide_count, "transcript_segments": transcript_count,
            }
            failures.pop(video.video_id, None)
            save_state(state)
        except Exception as exc:
            any_failure = True
            previous = failures.get(video.video_id, {})
            failures[video.video_id] = {
                "title": video.title, "url": video.url,
                "attempts": int(previous.get("attempts", 0)) + 1,
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc)[:1500],
            }
            log.exception("Failed processing %s (%s)", video.title, video.video_id)
            save_state(state)
    state["last_successful_scan_at"] = datetime.now(timezone.utc).isoformat()
    state.pop("last_scan_error", None)
    save_state(state)
    # A failure should be visible in GitHub Actions while retaining retry state.
    return 1 if any_failure else 0

if __name__ == "__main__":
    sys.exit(main())
