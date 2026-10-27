# IndoLogus YouTube → Telegram lecture PDF automation

Checks the IndoLogus YouTube channel at **2:00 PM and 9:00 PM India time**. For unseen uploads, it downloads the video temporarily, obtains captions (or generates a transcript with `faster-whisper`), samples frames at one frame per second, removes perceptually similar frames, creates one combined PDF, and sends it to Telegram. Temporary video/frame files are deleted when processing finishes.

> **Important:** Use this only for videos you are authorized to download and process. Follow YouTube's Terms of Service, copyright law, and any applicable access restrictions. The script does not bypass access controls or DRM. Slide extraction is heuristic: animations, tiny text changes, and slides shown briefly may be missed. Review the PDF for important study material.

## Project files

- `main.py` — channel scan, download, transcript, slide deduplication, PDF and Telegram upload.
- `.github/workflows/indologus.yml` — scheduled GitHub Actions workflow.
- `state/state.json` — processed video IDs and retry/error information; committed after each run.
- `requirements.txt` — Python dependencies.
- `.env.example` — names of configuration values (contains placeholders only).

## 1. Create the Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, follow the steps, and copy the bot token.
3. Open your new bot and press **Start** (or add it to the intended group/channel with permission to post).
4. Obtain the destination chat ID. For a private chat, send a message to the bot and use the official Telegram Bot API `getUpdates` endpoint in a browser, replacing `BOT_TOKEN` with your token: `https://api.telegram.org/botBOT_TOKEN/getUpdates`. Read `message.chat.id` from the response. Do not share the token publicly. If `getUpdates` is empty, send another message to the bot and retry. For groups, add the bot to the group and send a message there; group IDs often begin with `-100` for supergroups/channels.

## 2. Create the GitHub repository

1. Create a new **private** GitHub repository, e.g. `indologus-telegram-pdf`.
2. Upload the contents of this project (not the parent folder) to the repository, preserving `.github/workflows/indologus.yml` and `state/state.json`.
3. In **Settings → Secrets and variables → Actions → Secrets**, create:
   - `TELEGRAM_BOT_TOKEN` — token from BotFather.
   - `TELEGRAM_CHAT_ID` — the private chat/group/channel ID to receive PDFs.
4. Optional: under **Settings → Secrets and variables → Actions → Variables**, add `YOUTUBE_CHANNEL_URL` with `https://www.youtube.com/@IndoLogus`. The workflow uses this by default if omitted.
5. Under **Settings → Actions → General → Workflow permissions**, allow read and write permissions if your organization/repository setting requires it. The workflow requests `contents: write` so it can save processed IDs.
6. In **Actions**, enable workflows if GitHub asks.

## 3. Schedule

The workflow uses UTC cron because GitHub Actions cron is UTC:

- `30 8 * * *` = 2:00 PM Asia/Kolkata.
- `30 15 * * *` = 9:00 PM Asia/Kolkata.

GitHub scheduled workflows can start late, and public-repository schedules may be disabled after inactivity. Use a private repository and check the Actions page periodically. Manual testing is available through **Actions → IndoLogus lecture PDF automation → Run workflow**.

## 4. Test safely

1. Add the secrets and push all files to the repository's default branch.
2. Open **Actions**, select the workflow, and click **Run workflow**.
3. Inspect the log. A first run may process up to two unseen recent uploads, so it can take a while and may consume GitHub Actions time. To limit the first run to one, use the manual `max_videos` input and enter `1`.
4. Check Telegram for a PDF. Open it and inspect the transcript and screenshots. Check the action log for any download, caption, transcription, or upload errors.
5. If the channel scan works but processing fails, the `failures` section in `state/state.json` records the latest error and the video remains eligible for retry. The script does not mark a video as processed until Telegram confirms the PDF upload.

## 5. Local test (optional)

Requires Python 3.11+, `ffmpeg`, and a working Telegram bot/chat. On Ubuntu/Debian:

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='your-token'
export TELEGRAM_CHAT_ID='your-chat-id'
export YOUTUBE_CHANNEL_URL='https://www.youtube.com/@IndoLogus'
python main.py
```

Keep real credentials in environment variables or GitHub Secrets; never commit them. A local run writes PDFs to `output/` (ignored by Git) and updates `state/state.json`.

## Notes and limitations

- **Transcript:** tries available English/Hindi captions first. If no usable captions exist, `faster-whisper` tiny model runs on CPU. Recognition may be imperfect, especially with mixed Hindi/English, technical names, or poor audio. Set `WHISPER_MODEL=base` for better potential accuracy at the cost of much longer runs and more memory.
- **Slides:** samples one frame per second, then uses perceptual hashing to drop near-duplicates. It cannot guarantee every unique slide. It may retain a frame with a presenter overlay or miss a very brief/animated slide. For very long videos, frame extraction and PDF creation may be slow.
- **Telegram limit:** the script refuses PDFs larger than a conservative 49 MiB because the hosted Telegram Bot API has an upload-size limit. If this occurs, reduce image quality/resolution or split the PDF in a future revision.
- **Retries:** failed videos are not marked processed. Up to two unseen videos are attempted per run by default; older unseen items are attempted first. Change `MAX_VIDEOS_PER_RUN` if needed.
- **Channel scan:** checks the latest 30 entries by default. If the channel has more unseen uploads than one run can process, later scheduled runs continue through the backlog.
- **State:** the workflow commits `state/state.json`. Do not delete this file unless you intentionally want previously processed videos to be treated as new.
- **Usage/cost:** GitHub Actions time is limited. Whisper fallback on long lectures can exceed a run's practical time budget. Monitor failed jobs and action minutes.

## Troubleshooting

- **`Sign in to confirm you're not a bot` / YouTube extraction errors:** update `yt-dlp` (already installed from requirements) and retry later. Do not attempt to bypass access restrictions.
- **No captions:** the script falls back to Whisper. If Whisper cannot load or run, check the action logs and available runner memory.
- **Telegram `chat not found`:** start the bot, verify the chat ID, or ensure the bot has posting permission in the group/channel.
- **Telegram `file is too big`:** PDF exceeds the safe limit; lower screenshot resolution/quality or split the output.
- **Repeated videos:** ensure `state/state.json` is committed after each run and that the workflow has write permission.
