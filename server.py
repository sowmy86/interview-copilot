#!/usr/bin/env python3
"""Interview copilot server (stdlib only).

- runs ./listener/listener (on-device transcription) and reads its JSON lines
- detects when the interviewer ("them") finishes a question (silence debounce)
- asks Claude (claude -p, your subscription) or Codex (codex exec, your ChatGPT plan)
- streams everything to the overlay / browser UI over Server-Sent Events

Run:  python3 server.py [--source system|both|mic] [--port 8765] [--no-overlay]
"""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LISTENER = ROOT / "listener" / "listener"
OVERLAY = ROOT / "overlay" / "overlay"
WEB = ROOT / "web" / "index.html"
PROFILE = ROOT / "profile.md"
WORKDIR = ROOT / ".work"  # empty cwd for the CLIs (no repo / CLAUDE.md scanning)
WHISPER_MODEL = ROOT / "models" / "ggml-large-v3-turbo-q5_0.bin"
WHISPER_PROMPT = "Research aptitude interview in Indian English. Hypothesis, methodology, literature review, eigenvalue, Fourier transform, Laplace, convolution, probability, p-value, regression, CTC."

SYSTEM_PROMPT = """You are a real-time copilot for a candidate in a live RESEARCH APTITUDE ASSESSMENT interview (e.g. admission or selection for a research programme / research position). The candidate reads your reply on a small overlay while talking, so it must be correct, fast to scan, and sayable out loud.

You get the recent transcript (INTERVIEWER = the panel, ME = the candidate; speech-to-text, so expect errors and fix them mentally) and the candidate's background.

Answer the interviewer's most recent question. Always start with one bold line the candidate can say immediately, then 3-4 short bullets (never more than 4) in the order to say them. By question type:
- Maths / quantitative aptitude / probability / logic puzzle: compute BEFORE stating the result. The bold line is the compact calculation ending in the answer, e.g. "**150/15 = 10 m/s; (150+300)/10 = 45 → Answer: 45 seconds**". At most 1-2 bullets after it (assumption or a check). Never state a number you have not just computed.
- Technical concept (signals & systems, linear algebra, calculus, numerical methods, probability/statistics, programming, ML, the candidate's field): bold line = crisp definition; bullets = intuition, a small example or formula, where it is used.
- Research methodology (hypothesis, variables, controls, sampling, bias, validity, reproducibility, literature review, ethics, experiment design, interpreting data): a structured answer they can walk through.
- Open-ended "how would you investigate / design / improve X": bullets = define the question, hypothesis, method/data, how to measure, threats/limitations, next step.
- Estimation (Fermi): state assumptions, quick arithmetic, final estimate first.
- About the candidate (project, thesis, research interest, a paper they read, why research, why this programme, strengths): first person, drawn only from the candidate background (never invent projects, papers, results or institutions; if missing, give a realistic outline marked "(adapt)"). Show curiosity: problem, approach, result, limitation, what next.
- If the candidate genuinely may not know: give a short honest way to reason towards it out loud.
Keep it tight but complete: about 70-80 words total, each bullet under 18 words. Maths: the calculation line + 1-2 bullets. Write in first person where the candidate would speak. No preamble, no headings, no filler. Use plain text maths (x^2, sqrt(n), O(n log n)).

The interviewer likely speaks Indian English. Read through speech-to-text mistakes, e.g. "city say"/"CTC" = cost to company, "Kubernets" = Kubernetes, "eigen value" = eigenvalue, "four year" = Fourier, "plus" vs "class". Answer the question that was meant, not the literal transcript.

Reply with exactly NO_QUESTION only if the interviewer's latest turn clearly asks nothing: pure small talk or acknowledgement ("okay", "thank you", "fine", "let's move on", "can you hear me"). Instructions ("explain...", "solve...", "tell me...", "walk me through...", "calculate..."), puzzles, half-finished or garbled questions ARE questions: answer the most likely intended question. When unsure, answer."""

CLAUDE_MODELS = ["sonnet", "haiku", "opus"]
CODEX_MODELS = ["gpt-6.2-sol", "gpt-6-luna", "gpt-6.1-sol"]


# --------------------------------------------------------------------------- state

class Hub:
    """Fan-out of events to every connected SSE client."""

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, ev: dict):
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass


hub = Hub()


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.finals: list[dict] = []          # {"src","text","t"}
        self.partial = {"them": "", "me": ""}
        self.partial_seg = {"them": -1, "me": -1}
        self.last_them_activity = 0.0         # time of last change in interviewer text
        self.answered_upto = 0                # index into finals + partial already answered
        self.pending_them = False             # interviewer said something not yet answered
        self.fire_at = 0.0                    # when to auto-answer if nothing new arrives
        self.turn_start = None                # analyzer time (s) where the interviewer's current turn began
        self.turn_idx = 0                     # index into finals where the current turn began
        self.fired_turn = (None, 0)           # (turn_start, turn_idx) of the last fired answer
        self.settings = {
            "backend": "claude",
            "claude_model": "sonnet",
            "codex_model": "gpt-6.2-sol",
            "auto": True,
            "silence_ms": 1000,
            "listen_me": True,
        }
        self.status = {"them": "starting", "me": "starting", "listener": "starting", "whisper": "off"}

    def transcript_text(self, max_chars=3500, include_paused=True) -> str:
        with self.lock:
            lines = [f"{'INTERVIEWER' if f['src'] == 'them' else 'ME'}: {f['text']}" for f in self.finals
                     if include_paused or not f.get("paused")]
            for src in ("them", "me"):
                if self.partial[src]:
                    lines.append(f"{'INTERVIEWER' if src == 'them' else 'ME'}: {self.partial[src]}")
        out, n = [], 0
        for line in reversed(lines):
            n += len(line) + 1
            if n > max_chars:
                break
            out.append(line)
        return "\n".join(reversed(out))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "type": "snapshot",
                "finals": self.finals[-60:],
                "partial": self.partial,
                "settings": self.settings,
                "status": self.status,
                "models": {"claude": CLAUDE_MODELS, "codex": CODEX_MODELS},
            }


state = State()


# --------------------------------------------------------------------------- LLM backends

def child_env() -> dict:
    env = dict(os.environ)
    # force subscription auth instead of API billing
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return env


def build_prompt(manual_question: str | None, whisper_text: str | None = None, force: bool = False) -> str:
    profile = PROFILE.read_text().strip() if PROFILE.exists() else ""
    parts = []
    if profile:
        parts.append("## Candidate background\n" + profile)
    # speech heard while paused is left out of auto answers (kept for an explicit Answer press)
    include_paused = force or bool(manual_question)
    parts.append("## Transcript (most recent last)\n" + (state.transcript_text(include_paused=include_paused) or "(empty)"))
    if whisper_text:
        parts.append("## Interviewer's latest turn: accurate re-transcription (trust this over the transcript above)\n"
                     + whisper_text)
    if manual_question:
        parts.append("## Question to answer now\n" + manual_question)
    elif force:
        parts.append("The candidate pressed Answer: answer the most recent question or request in the transcript now. "
                     "Never reply NO_QUESTION.")
    else:
        parts.append("Answer the interviewer's latest question.")
    return "\n\n".join(parts)


class Whisper:
    """whisper.cpp server (Metal) kept warm; re-transcribes the interviewer's question audio."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.ok = False
        self.fails = 0
        self.port = 8766

    def start(self, port: int):
        self.port = port
        binary = shutil.which("whisper-server")
        if not binary or not WHISPER_MODEL.exists():
            print("[server] whisper: not installed (brew install whisper-cpp + models/) — using Apple transcription",
                  file=sys.stderr)
            return
        self.proc = subprocess.Popen(
            [binary, "-m", str(WHISPER_MODEL), "--host", "127.0.0.1", "--port", str(port), "-l", "en", "-t", "4"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state.status["whisper"] = "loading"
        threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self):
        for _ in range(240):
            if self.proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=0.5).read()
                break
            except Exception:
                time.sleep(0.25)
        WORKDIR.mkdir(exist_ok=True)
        wav = WORKDIR / "warmup.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        if self.transcribe(str(wav), timeout=20) is not None:
            self.ok = True
            state.status["whisper"] = "ready"
            print("[server] whisper: ready (large-v3-turbo, Metal)", file=sys.stderr)
        else:
            state.status["whisper"] = "failed"
            print("[server] whisper: warm-up failed — using Apple transcription", file=sys.stderr)
        hub.send({"type": "status", "src": "whisper", "value": state.status["whisper"]})

    def transcribe(self, path: str, timeout: float = 1.5) -> str | None:
        boundary = uuid.uuid4().hex
        fields = {"response_format": "json", "temperature": "0", "prompt": WHISPER_PROMPT}
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n").encode() + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return (d.get("text") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"[server] whisper request failed: {e}", file=sys.stderr)
            return None

    def failed(self):
        self.fails += 1
        if self.fails >= 2 and self.ok:
            self.ok = False
            state.status["whisper"] = "disabled"
            hub.send({"type": "info", "text": "Whisper failed twice — using Apple transcription for this session"})
            hub.send({"type": "status", "src": "whisper", "value": "disabled"})

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


whisper = Whisper()
CLIP_WAIT: dict[int, list] = {}
CLIP_LOCK = threading.Lock()
CLIP_SEQ = [0]
LISTENER_LOCK = threading.Lock()


def listener_send(obj: dict) -> bool:
    with LISTENER_LOCK:
        p = LISTENER_PROC
        if not p or p.poll() is not None or not p.stdin:
            return False
        try:
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False


def whisper_for_turn(turn_start, apple_text: str) -> tuple[str | None, float]:
    """Re-transcribe the interviewer's current turn. Returns (text or None, seconds spent)."""
    t0 = time.time()
    with CLIP_LOCK:
        CLIP_SEQ[0] += 1
        cid = CLIP_SEQ[0]
        ev = threading.Event()
        CLIP_WAIT[cid] = [ev, None]
    if not listener_send({"cmd": "clip", "src": "them", "start": turn_start, "id": cid}) or not ev.wait(0.6):
        with CLIP_LOCK:
            CLIP_WAIT.pop(cid, None)
        whisper.failed()
        return None, time.time() - t0
    with CLIP_LOCK:
        res = CLIP_WAIT.pop(cid, [None, None])[1] or {}
    if res.get("error") or not res.get("path"):
        return None, time.time() - t0
    text = whisper.transcribe(res["path"], timeout=2.0)
    if text is None:
        whisper.failed()
        return None, time.time() - t0
    whisper.fails = 0
    # sanity: empty, prompt echo, or wildly different length → keep Apple's text
    aw, ww = len(WORD_RE.findall(apple_text)), len(WORD_RE.findall(text))
    if (not text or text.lower().strip(" .") in WHISPER_PROMPT.lower() or text.startswith("[")
            or (4 <= aw <= 60 and ww < aw * 0.4) or ww > aw * 2.5 + 6):
        print(f"[server] whisper output rejected: {text!r} vs apple {apple_text!r}", file=sys.stderr)
        return None, time.time() - t0
    return text, time.time() - t0


class Answerer:
    """Runs at most one LLM process at a time; a new request cancels the old one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.gen = 0
        self.started_at = 0.0
        self.active = False

    def busy_since(self) -> float | None:
        with self.lock:
            return self.started_at if self.active else None

    def cancel(self):
        with self.lock:
            self.gen += 1
            self.active = False
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def ask(self, manual_question: str | None = None, reason: str = "auto"):
        self.cancel()
        with self.lock:
            gen = self.gen
            self.active = True
            self.started_at = time.time()
        with state.lock:
            turn_start, turn_idx = state.turn_start, state.turn_idx
            state.fired_turn = (turn_start, turn_idx)
            state.turn_start = None
            apple_turn = " ".join(f["text"] for f in state.finals[turn_idx:] if f["src"] == "them")
            if state.partial["them"]:
                apple_turn = (apple_turn + " " + state.partial["them"]).strip()
        backend = state.settings["backend"]
        threading.Thread(target=self._run, args=(gen, backend, reason, manual_question, turn_start, apple_turn),
                         daemon=True).start()

    # ---- runners

    def _run(self, gen, backend, reason, manual_question, turn_start, apple_turn, preset_whisper=None, preset_wsec=0.0):
        try:
            self._run_inner(gen, backend, reason, manual_question, turn_start, apple_turn, preset_whisper, preset_wsec)
        finally:
            with self.lock:
                if gen == self.gen:
                    self.active = False

    def _run_inner(self, gen, backend, reason, manual_question, turn_start, apple_turn, preset_whisper, preset_wsec):
        t0 = time.time()
        model = state.settings["codex_model"] if backend == "codex" else state.settings["claude_model"]
        hub.send({"type": "answer_start", "gen": gen, "backend": backend, "model": model,
                  "reason": reason, "question": preset_whisper or manual_question or apple_turn or state.partial["them"] or
                  next((f["text"] for f in reversed(state.finals) if f["src"] == "them"), "")})
        if preset_whisper:
            hub.send({"type": "answer_question", "gen": gen, "text": preset_whisper, "whisper_s": round(preset_wsec, 2)})
        elif not manual_question and whisper.ok and state.settings.get("whisper", True):
            # Don't wait for Whisper: answer from Apple's text now, and only restart if Whisper heard something different.
            threading.Thread(target=self._whisper_check, args=(gen, backend, reason, turn_start, apple_turn),
                             daemon=True).start()
        prompt = build_prompt(manual_question, preset_whisper, force=(reason == "manual"))
        try:
            if backend == "codex" and time.time() < CODEX_BLOCKED["until"]:
                hub.send({"type": "answer_note", "gen": gen, "text": "Codex unavailable (" + CODEX_BLOCKED["msg"][:120] + ") — using Claude haiku"})
                self._run_claude(gen, prompt, "haiku", t0)
            elif backend == "codex":
                ok = self._run_codex(gen, prompt, model, t0)
                if not ok and self._current(gen):
                    hub.send({"type": "answer_note", "gen": gen, "text": "Codex failed, falling back to Claude haiku…"})
                    self._run_claude(gen, prompt, "haiku", t0)
            else:
                self._run_claude(gen, prompt, model, t0)
        except Exception as e:  # noqa: BLE001
            if self._current(gen):
                hub.send({"type": "answer_error", "gen": gen, "text": str(e)})

    def _whisper_check(self, gen, backend, reason, turn_start, apple_turn):
        wtext, wsec = whisper_for_turn(turn_start, apple_turn)
        if not wtext or not self._current(gen):
            return
        if same_words(wtext, apple_turn):
            hub.send({"type": "answer_question", "gen": gen, "text": wtext, "whisper_s": round(wsec, 2)})
            return
        print(f"[server] whisper differs, re-answering: {apple_turn!r} -> {wtext!r}", file=sys.stderr)
        self.cancel()
        with self.lock:
            ngen = self.gen
            self.active = True
            self.started_at = time.time()
        threading.Thread(target=self._run, args=(ngen, backend, reason, None, None, apple_turn, wtext, wsec),
                         daemon=True).start()

    def _current(self, gen) -> bool:
        with self.lock:
            return gen == self.gen

    def _spawn(self, gen, cmd, stdin_text=None):
        WORKDIR.mkdir(exist_ok=True)
        p = subprocess.Popen(
            cmd, cwd=WORKDIR, env=child_env(), text=True, bufsize=1,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with self.lock:
            if gen != self.gen:
                p.kill()
                return None
            self.proc = p
            self.started_at = time.time()
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text)
                p.stdin.close()
            except BrokenPipeError:
                pass
        return p

    def _run_claude(self, gen, prompt, model, t0) -> bool:
        cmd = [
            "claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--effort", "low",  # user's global effort is xhigh; don't inherit it
        ]
        p = self._spawn(gen, cmd, stdin_text=prompt)
        if not p:
            return False
        got_text, first = False, None
        for line in p.stdout:
            if not self._current(gen):
                return False
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            if t == "stream_event":
                ev = e.get("event", {})
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    if first is None:
                        first = time.time() - t0
                    got_text = True
                    hub.send({"type": "answer_delta", "gen": gen, "text": ev["delta"]["text"]})
            elif t == "result":
                if e.get("is_error"):
                    hub.send({"type": "answer_error", "gen": gen, "text": str(e.get("result") or e)[:400]})
                    return False
                if not got_text and e.get("result"):
                    hub.send({"type": "answer_delta", "gen": gen, "text": e["result"]})
                hub.send({"type": "answer_done", "gen": gen, "first_token_s": round(first or 0, 2),
                          "total_s": round(time.time() - t0, 2)})
                return True
        p.wait()
        if self._current(gen):
            err = (p.stderr.read() or "").strip()[-400:]
            hub.send({"type": "answer_error", "gen": gen, "text": f"claude exited ({p.returncode}) {err}"})
        return False

    def _run_codex(self, gen, prompt, model, t0) -> bool:
        if not shutil.which("codex"):
            hub.send({"type": "answer_note", "gen": gen, "text": "codex CLI not found"})
            return False
        full = SYSTEM_PROMPT + "\n\n" + prompt + "\n\nReply with the answer text only. Do not run commands or read files."
        cmd = [
            "codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "-m", model, "-c", 'model_reasoning_effort="low"', full,
        ]
        p = self._spawn(gen, cmd)
        if not p:
            return False
        sent = ""  # text already streamed for the current agent message
        first = None
        for line in p.stdout:
            if not self._current(gen):
                return False
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type", "")
            item = e.get("item") or {}
            if t in ("item.started", "item.updated", "item.completed") and item.get("type") == "agent_message":
                text = item.get("text") or ""
                if text.startswith(sent) and len(text) > len(sent):
                    if first is None:
                        first = time.time() - t0
                    hub.send({"type": "answer_delta", "gen": gen, "text": text[len(sent):]})
                    sent = text
            elif t.endswith("delta") and isinstance(e.get("delta"), str):  # in case a future CLI streams deltas
                if first is None:
                    first = time.time() - t0
                sent += e["delta"]
                hub.send({"type": "answer_delta", "gen": gen, "text": e["delta"]})
            elif t == "turn.failed":
                msg = (e.get("error") or {}).get("message") or "turn failed"
                if "limit" in msg.lower():
                    CODEX_BLOCKED.update(until=time.time() + 600, msg=msg)
                hub.send({"type": "answer_note", "gen": gen, "text": "Codex: " + msg})
                return False
            elif t == "turn.completed":
                if sent:
                    hub.send({"type": "answer_done", "gen": gen, "first_token_s": round(first or 0, 2),
                              "total_s": round(time.time() - t0, 2)})
                    return True
                return False
            # item.completed with item.type == "error" (e.g. hook warnings) is not fatal: ignore
        p.wait()
        if sent and self._current(gen):
            hub.send({"type": "answer_done", "gen": gen, "first_token_s": round(first or 0, 2),
                      "total_s": round(time.time() - t0, 2)})
            return True
        return False


def same_words(a: str, b: str) -> bool:
    import difflib
    wa = [w.lower() for w in WORD_RE.findall(a)]
    wb = [w.lower() for w in WORD_RE.findall(b)]
    return difflib.SequenceMatcher(None, wa, wb).ratio() >= 0.9


answerer = Answerer()
CODEX_BLOCKED = {"until": 0.0, "msg": ""}


# --------------------------------------------------------------------------- question trigger

WORD_RE = re.compile(r"[A-Za-z0-9']+")


def trigger_loop():
    """Fire an answer once the interviewer seems done (see on_them_activity / final handling)."""
    while True:
        time.sleep(0.05)
        s = state.settings
        if not s["auto"]:
            continue
        with state.lock:
            pending = state.pending_them
            due = time.time() >= state.fire_at
            text = state.partial["them"] or next(
                (f["text"] for f in reversed(state.finals) if f["src"] == "them"), "")
        if pending and due:
            with state.lock:
                state.pending_them = False
            if len(WORD_RE.findall(text)) >= 3:
                answerer.ask(reason="auto")


def on_them_activity(text: str = ""):
    """Interviewer is (still) talking. If we just fired an answer, they weren't done: cancel."""
    since = answerer.busy_since()
    if since is not None and time.time() - since < 2.5 and state.settings["auto"]:
        answerer.cancel()
        hub.send({"type": "answer_cancelled"})
        with state.lock:  # same question continues: keep its original start
            state.turn_start, state.turn_idx = state.fired_turn
    now = time.time()
    # transcriber partials arrive in ~1s bursts, so plain silence needs > 1s;
    # a trailing "?" is a strong end-of-question signal
    wait = 0.45 if text.rstrip().endswith("?") else state.settings["silence_ms"] / 1000
    with state.lock:
        state.last_them_activity = now
        state.fire_at = now + wait
        state.pending_them = True


# --------------------------------------------------------------------------- listener process

LISTENER_PROC: subprocess.Popen | None = None
SERVER: list = [None]


def listener_loop(source: str, vocab: str, test_file: str | None = None):
    global LISTENER_PROC
    if not LISTENER.exists():
        hub.send({"type": "status", "src": "listener", "value": "missing — run ./build.sh"})
        state.status["listener"] = "missing — run ./build.sh"
        return
    cmd = [str(LISTENER), "--source", source]
    if test_file:
        cmd = [str(LISTENER), "--file", test_file, "--realtime"]
    if vocab:
        cmd += ["--vocab", vocab]
    while True:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
        LISTENER_PROC = p
        state.status["listener"] = "running"
        hub.send({"type": "status", "src": "listener", "value": "running"})
        for line in p.stdout:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            handle_listener_event(e)
        p.wait()
        if test_file:
            return
        state.status["listener"] = f"exited ({p.returncode}), restarting"
        hub.send({"type": "status", "src": "listener", "value": state.status["listener"]})
        if p.returncode == 2:  # no audio source at all; don't spin
            print("[server] listener could not start any audio source; see errors above", file=sys.stderr)
            return
        time.sleep(1.5)


def note_turn(e: dict):
    st = e.get("start")
    with state.lock:
        if state.turn_start is None and isinstance(st, (int, float)):
            state.turn_start = float(st)
            state.turn_idx = len(state.finals)


def handle_listener_event(e: dict):
    t, src = e.get("type"), e.get("src")
    if t == "clip":
        with CLIP_LOCK:
            w = CLIP_WAIT.get(e.get("id"))
            if w:
                w[1] = e
                w[0].set()
        return
    if src == "them" and t in ("partial", "final") and e.get("text"):
        note_turn(e)
    if t == "partial":
        if src == "me" and not state.settings["listen_me"]:
            return
        with state.lock:
            changed = state.partial.get(src) != e["text"]
            state.partial[src] = e["text"]
            state.partial_seg[src] = e.get("seg", -1)
        if changed:
            hub.send({"type": "partial", "src": src, "text": e["text"]})
            if src == "them" and e["text"]:
                on_them_activity(e["text"])
    elif t == "final":
        if src == "me" and not state.settings["listen_me"]:
            return
        item = {"src": src, "text": e["text"], "t": time.time(), "paused": not state.settings["auto"]}
        with state.lock:
            state.finals.append(item)
            state.partial[src] = ""
        hub.send({"type": "final", **item})
        if src == "them":
            with state.lock:
                # a finalized sentence = the speaker paused; answer quickly unless they continue
                state.pending_them = True
                state.fire_at = min(state.fire_at, time.time() + 0.2) if state.fire_at > time.time() else time.time() + 0.2
    elif t == "level":
        hub.send({"type": "level", "src": src, "rms": e.get("rms", 0)})
    elif t == "ready":
        state.status[src] = "listening"
        hub.send({"type": "status", "src": src, "value": "listening"})
    elif t in ("error", "fatal"):
        msg = e.get("msg", "")
        if e.get("hint"):
            msg += " — " + e["hint"]
        if src:
            state.status[src] = "error"
        hub.send({"type": "error", "src": src or "listener", "text": msg})
        print(f"[listener:{src or '-'}] {msg}", file=sys.stderr)
    elif t == "status":
        hub.send({"type": "info", "text": e.get("msg", "")})
        print(f"[listener] {e.get('msg', '')}", file=sys.stderr)


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = WEB.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                self._sse(state.snapshot())
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._sse(ev)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(q)
        elif self.path == "/profile":
            self._json(200, {"text": PROFILE.read_text() if PROFILE.exists() else ""})
        else:
            self._json(404, {"error": "not found"})

    def _sse(self, ev):
        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        b = self._body()
        if self.path == "/answer":
            q = (b.get("question") or "").strip() or None
            with state.lock:
                state.pending_them = False
            answerer.ask(manual_question=q, reason="manual")
            self._json(200, {"ok": True})
        elif self.path == "/cancel":
            answerer.cancel()
            hub.send({"type": "answer_cancelled", "manual": True})
            self._json(200, {"ok": True})
        elif self.path == "/clear":
            with state.lock:
                state.finals.clear()
                state.partial = {"them": "", "me": ""}
                state.pending_them = False
            hub.send(state.snapshot())
            self._json(200, {"ok": True})
        elif self.path == "/settings":
            with state.lock:
                for k, v in b.items():
                    if k in state.settings:
                        state.settings[k] = v
                if "auto" in b:
                    state.pending_them = False  # pause/resume: don't answer whatever was said meanwhile
                if b.get("auto") is True:  # resume: the next question starts now, not in the paused stretch
                    state.turn_start = None
                    state.turn_idx = len(state.finals)
            if b.get("auto") is False:  # paused: stop the answer that is generating right now
                answerer.cancel()
                hub.send({"type": "answer_cancelled", "manual": True})
            hub.send({"type": "settings", "settings": state.settings})
            self._json(200, state.settings)
        elif self.path == "/profile":
            PROFILE.write_text(b.get("text", ""))
            self._json(200, {"ok": True})
        elif self.path == "/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=SERVER[0].shutdown, daemon=True).start()
        elif self.path == "/inject":  # testing: pretend the interviewer said something
            handle_listener_event({"type": "final", "src": b.get("src", "them"), "text": b.get("text", "")})
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--source", default="system", choices=["both", "system", "mic", "none"])
    ap.add_argument("--vocab", default="", help="comma-separated words to bias transcription")
    ap.add_argument("--backend", default="claude", choices=["claude", "codex"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("--no-whisper", action="store_true", help="use only Apple's on-device transcription")
    ap.add_argument("--whisper-port", type=int, default=8766)
    ap.add_argument("--test-file", default=None, help="play an audio file through the transcriber instead of live audio")
    a = ap.parse_args()

    state.settings["backend"] = a.backend
    if a.model:
        state.settings["codex_model" if a.backend == "codex" else "claude_model"] = a.model

    import signal

    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    SERVER[0] = srv
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{a.port}/"
    print(f"[server] UI at {url}", file=sys.stderr)

    if not a.no_whisper:
        whisper.start(a.whisper_port)
    threading.Thread(target=trigger_loop, daemon=True).start()
    if a.source != "none":
        threading.Thread(target=listener_loop, args=(a.source, a.vocab, a.test_file), daemon=True).start()

    overlay = None
    if not a.no_overlay:
        if OVERLAY.exists():
            overlay = subprocess.Popen([str(OVERLAY), url])
        else:
            print(f"[server] overlay not built; open {url} in a browser", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if overlay:
            overlay.terminate()
        if LISTENER_PROC and LISTENER_PROC.poll() is None:
            LISTENER_PROC.terminate()
        answerer.cancel()
        whisper.stop()
        os._exit(0)  # don't let a stuck handler thread keep the process alive


if __name__ == "__main__":
    main()
