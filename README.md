# Interview Copilot

Set up for a **research aptitude assessment** interview (maths/aptitude, concepts, research methodology, your projects). The answer style lives in `SYSTEM_PROMPT` in `server.py`.

The tool doesn't output audio; it only captures it.

Live transcription of a call plus instant suggested answers in a floating overlay.

- **Transcription:** fully on-device. No API keys, and audio never leaves the Mac.
  - Apple SpeechAnalyzer writes the live words as they're spoken and decides when a question has ended.
  - When a question ends, **Whisper large-v3-turbo** (whisper.cpp, Metal) re-transcribes that question's audio in about 0.5–0.6s. That's much more accurate on Indian English and technical terms: it gets "CTC" right where Apple hears "city say", and "Kubernetes" where Apple writes "Kubernets". If Whisper is missing, slow or fails, the tool falls back to Apple's text automatically.
  - The interviewer is captured from **system audio**, which works for Zoom, Meet and Teams, including in the browser, and with headphones or speakers.
  - With `--source both`, you're captured from the **microphone** too.
- **Answers:** your existing subscriptions, through their CLIs.
  - `claude -p` uses your Claude login (default model: **sonnet**, chosen for maths accuracy; about 1.5–2s to first words. Haiku is in the dropdown).
  - `codex exec` uses your ChatGPT login (default model: gpt-6.2-sol at low reasoning effort).
  - If Codex fails (for example, the usage limit is hit), the tool falls back to Claude automatically.

## Start

```bash
./run.sh                      # interviewer audio only; builds on first run, then opens the overlay
./run.sh --source both        # also transcribe your mic (headphones required)
./run.sh --model haiku        # slightly faster, weaker at maths
./run.sh --backend codex      # ChatGPT
./run.sh --no-whisper         # Apple transcription only (skip the Whisper pass)
```

Stop with Ctrl+C in the terminal.

## Install (one time)

```bash
brew install whisper-cpp
mkdir -p models && curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin   # 574 MB
```

## Before the interview (one time)

1. **Screen & System Audio Recording.** Run `./listener/listener --check`. If it reports `"screenAndSystemAudioRecording":"not authorized"`:
   - open System Settings > Privacy & Security > Screen & System Audio Recording;
   - enable your terminal app;
   - quit the terminal fully (⌘Q) and reopen it.
2. **Microphone (optional).** Only used with `--source both`. macOS asks the first time; click Allow.
3. **Your background.** Fill in `profile.md` (created from `profile.example.md` on first run) with your resume highlights and the job description, or use the overlay's **⋯** button. It's sent with every question so answers draw on your real experience.
4. **Headphones with `--source both`.** On speakers, the mic also picks up the interviewer.

## Using it

| | |
|---|---|
| Auto | Answers when the interviewer pauses after a question (on by default) |
| **⌃⌥A** | Answer now, using the latest transcript |
| **⌃⌥H** | Hide or show the overlay |
| **⌃⌥C** | Clear the transcript |
| **⌃⌥= / ⌃⌥-** | Make the background more or less see-through (the text stays sharp). Also under **⋯ > Background**. |
| Text box | Type any question and press Enter |
| Header bar | Drag the empty part of the header (left of the buttons) to move the overlay |
| **–** / **×** | Hide now (⌃⌥H brings it back) / quit the whole tool |
| **⏸ Pause / ▶ Resume** (or **⌃⌥S**) | Pause stops the current answer and all automatic answers (the border turns red; transcription continues). Resume answers the next question. The Answer button and ⌃⌥A still work while paused. |

The overlay is see-through by default (40% background, no blur), so you can see what's behind it. The setting is remembered.

The two dots in the top-left corner show audio activity: yellow for the interviewer, blue for you. A red dot means that source has a problem; hover over it to see why.

## Files

- `listener/listener.swift`: audio capture and on-device transcription. Prints JSON lines.
- `server.py`: question detection, the Claude/Codex runners, and the local web server on 127.0.0.1:8765. Python stdlib only.
- `web/index.html`: the UI.
- `models/`: the Whisper model (git-ignore it; it's 574 MB).
- `overlay/overlay.swift`: the always-on-top floating window and the global hotkeys.

## Testing without a call

```bash
say -o /tmp/q.aiff "What is the difference between a process and a thread?"
python3 server.py --test-file /tmp/q.aiff
```
