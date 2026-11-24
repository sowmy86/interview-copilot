#!/bin/bash
# Start the interview copilot: transcription (Apple live + Whisper re-check) + overlay + answers.
#   ./run.sh                       # interviewer (system audio) only, Claude sonnet, Whisper on
#   ./run.sh --source both         # also your mic (headphones needed)
#   ./run.sh --model haiku         # slightly faster, weaker at maths
#   ./run.sh --backend codex       # ChatGPT via your Codex login (gpt-6.2-sol)
#   ./run.sh --no-whisper          # Apple transcription only
cd "$(dirname "$0")"
if [ ! -x listener/listener ] || [ ! -x overlay/overlay ] || [ listener/listener.swift -nt listener/listener ] || [ overlay/overlay.swift -nt overlay/overlay ]; then
  ./build.sh || exit 1
fi
[ -f profile.md ] || cp profile.example.md profile.md
exec python3 server.py "$@"
