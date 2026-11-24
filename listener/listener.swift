// listener — on-device live transcription for the interview copilot.
//
// Captures system audio (the other side of the call, src="them") via ScreenCaptureKit
// and/or the microphone (you, src="me") via AVAudioEngine, transcribes with Apple's
// on-device SpeechAnalyzer/SpeechTranscriber (macOS 26+), and prints JSON lines:
//
//   {"type":"ready","src":"them"}
//   {"type":"partial","src":"them","seg":3,"text":"so tell me about"}
//   {"type":"final","src":"them","seg":3,"text":"So tell me about a time you..."}
//   {"type":"level","src":"them","rms":0.012}
//   {"type":"error","src":"mic","msg":"...","hint":"..."}
//
// Usage:
//   listener [--source both|system|mic] [--locale en-US] [--vocab "Kubernetes,gRPC"]
//   listener --file question.wav [--realtime]      # transcribe a file (testing)
//   listener --check [--request]                   # permission / model self-test

import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit
import Speech

// MARK: - Output

let outLock = NSLock()

func emit(_ dict: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []) else { return }
    outLock.lock()
    defer { outLock.unlock() }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func log(_ s: String) {
    FileHandle.standardError.write("[listener] \(s)\n".data(using: .utf8)!)
}

struct ListenerError: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

// MARK: - Transcription pipeline (one per audio source)

final class Pipeline: @unchecked Sendable {
    let src: String
    let transcriber: SpeechTranscriber
    let analyzer: SpeechAnalyzer
    private var targetFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    private let stream: AsyncStream<AnalyzerInput>
    private let continuation: AsyncStream<AnalyzerInput>.Continuation
    private var resultsTask: Task<Void, Never>?

    // Rolling copy of the audio actually yielded to the analyzer (target format, channel 0),
    // indexed by analyzer frame so transcriber time ranges map straight onto it.
    private let ringLock = NSLock()
    private var ring: [Float] = []
    private var ringStartFrame: Int64 = 0
    private var fedFrames: Int64 = 0
    var sampleRate: Double { targetFormat?.sampleRate ?? 16000 }

    // level metering (touched only from the single feeding thread of this source)
    private var levelAccum: Double = 0
    private var levelFrames: Int = 0
    private var levelLastEmit = Date()
    private var everNonZero = false
    private var fedSeconds: Double = 0
    private var warnedZero = false
    var warnOnSilence = false

    init(src: String, locale: Locale) {
        self.src = src
        transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: []
        )
        analyzer = SpeechAnalyzer(
            modules: [transcriber],
            options: .init(priority: .userInitiated, modelRetention: .processLifetime)
        )
        (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
    }

    func start(vocab: [String]) async throws {
        guard let fmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw ListenerError("no compatible audio format for SpeechTranscriber")
        }
        targetFormat = fmt
        if !vocab.isEmpty {
            let ctx = AnalysisContext()
            ctx.contextualStrings[.general] = vocab
            do { try await analyzer.setContext(ctx) } catch { log("vocab ignored: \(error)") }
        }
        try await analyzer.prepareToAnalyze(in: fmt)

        let src = self.src
        let transcriber = self.transcriber
        resultsTask = Task {
            var seg = 0
            do {
                for try await r in transcriber.results {
                    let text = String(r.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                    var ev: [String: Any] = ["src": src, "seg": seg, "text": text]
                    let st = r.range.start.seconds, en = r.range.end.seconds
                    if st.isFinite { ev["start"] = (st * 1000).rounded() / 1000 }
                    if en.isFinite { ev["end"] = (en * 1000).rounded() / 1000 }
                    if r.isFinal {
                        if !text.isEmpty {
                            ev["type"] = "final"
                            emit(ev)
                        }
                        seg += 1
                    } else {
                        ev["type"] = "partial"
                        emit(ev)
                    }
                }
            } catch {
                emit(["type": "error", "src": src, "msg": "transcriber: \(error)"])
            }
        }
        try await analyzer.start(inputSequence: stream)
        emit(["type": "ready", "src": src])
    }

    func feed(_ buffer: AVAudioPCMBuffer) {
        meter(buffer)
        guard let out = convert(buffer) else { return }
        record(out)
        continuation.yield(AnalyzerInput(buffer: out))
    }

    private func record(_ b: AVAudioPCMBuffer) {
        let n = Int(b.frameLength)
        guard n > 0 else { return }
        var tmp = [Float](repeating: 0, count: n)
        if let ch = b.floatChannelData {
            for i in 0..<n { tmp[i] = ch[0][i] }
        } else if let ch = b.int16ChannelData {
            for i in 0..<n { tmp[i] = Float(ch[0][i]) / 32768 }
        } else if let ch = b.int32ChannelData {
            for i in 0..<n { tmp[i] = Float(ch[0][i]) / 2_147_483_648 }
        }
        let keep = Int(sampleRate * 120)
        ringLock.lock()
        ring.append(contentsOf: tmp)
        fedFrames += Int64(n)
        if ring.count > keep + Int(sampleRate * 10) {
            let drop = ring.count - keep
            ring.removeFirst(drop)
            ringStartFrame += Int64(drop)
        }
        ringLock.unlock()
    }

    /// Audio from `start` seconds (analyzer timeline) minus padding up to "now".
    /// Unknown start → last 15 s. Never longer than `maxSeconds`.
    func clip(from start: Double?, padBefore: Double = 0.5, maxSeconds: Double = 28) -> ([Float], Double)? {
        ringLock.lock()
        defer { ringLock.unlock() }
        let sr = sampleRate
        let head = fedFrames
        var s: Int64
        if let start, start.isFinite {
            s = Int64(max(0, start - padBefore) * sr)
        } else {
            s = head - Int64(15 * sr)
        }
        s = max(s, head - Int64(maxSeconds * sr), ringStartFrame)
        guard head > s else { return nil }
        let a = Int(s - ringStartFrame)
        return (Array(ring[a..<ring.count]), sr)
    }

    func finish() async {
        continuation.finish()
        do { try await analyzer.finalizeAndFinishThroughEndOfInput() } catch {
            emit(["type": "error", "src": src, "msg": "finalize: \(error)"])
        }
        await resultsTask?.value
    }

    private func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let targetFormat else { return nil }
        let inFmt = buffer.format
        if inFmt == targetFormat { return buffer }
        if converter == nil || converterInputFormat != inFmt {
            converter = AVAudioConverter(from: inFmt, to: targetFormat)
            converter?.primeMethod = .none
            converterInputFormat = inFmt
        }
        guard let converter else { return nil }
        let ratio = targetFormat.sampleRate / inFmt.sampleRate
        let cap = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 1024)
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: cap) else { return nil }
        var consumed = false
        var err: NSError?
        let status = converter.convert(to: out, error: &err) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            log("convert error (\(src)): \(err?.localizedDescription ?? "?")")
            return nil
        }
        return out.frameLength > 0 ? out : nil
    }

    private func meter(_ buffer: AVAudioPCMBuffer) {
        let n = Int(buffer.frameLength)
        guard n > 0 else { return }
        var sum: Double = 0
        if let ch = buffer.floatChannelData {
            let p = ch[0]
            for i in 0..<n { let v = Double(p[i]); sum += v * v }
        } else if let ch = buffer.int16ChannelData {
            let p = ch[0]
            for i in 0..<n { let v = Double(p[i]) / 32768.0; sum += v * v }
        }
        if sum > 0 { everNonZero = true }
        levelAccum += sum
        levelFrames += n
        fedSeconds += Double(n) / buffer.format.sampleRate
        if warnOnSilence, !everNonZero, !warnedZero, fedSeconds > 4 {
            warnedZero = true
            emit([
                "type": "error", "src": src,
                "msg": "audio is pure digital silence",
                "hint": "Microphone access is probably denied. System Settings > Privacy & Security > Microphone > enable your terminal app, then restart it.",
            ])
        }
        if Date().timeIntervalSince(levelLastEmit) >= 0.5 {
            let rms = levelFrames > 0 ? (levelAccum / Double(levelFrames)).squareRoot() : 0
            emit(["type": "level", "src": src, "rms": (rms * 10000).rounded() / 10000])
            levelAccum = 0
            levelFrames = 0
            levelLastEmit = Date()
        }
    }
}

// MARK: - Clip requests (server → listener over stdin)

final class Registry: @unchecked Sendable {
    private let lock = NSLock()
    private var map: [String: Pipeline] = [:]
    func set(_ src: String, _ p: Pipeline) { lock.lock(); map[src] = p; lock.unlock() }
    func get(_ src: String) -> Pipeline? { lock.lock(); defer { lock.unlock() }; return map[src] }
}

let registry = Registry()

func writeWav16k(_ samples: [Float], sampleRate: Double, to url: URL) throws {
    var s = samples
    if abs(sampleRate - 16000) > 1, !samples.isEmpty {
        let ratio = sampleRate / 16000
        let n = Int(Double(samples.count) / ratio)
        let last = samples.count - 1
        s = (0..<n).map { i in
            let x = Double(i) * ratio
            let i0 = min(Int(x), last)
            let f = Float(x - Double(i0))
            return samples[i0] + (samples[min(i0 + 1, last)] - samples[i0]) * f
        }
    }
    var pcm = [Int16](repeating: 0, count: s.count)
    for i in 0..<s.count { pcm[i] = Int16(max(-1, min(1, s[i])) * 32767) }
    var data = Data()
    func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
    func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
    let bytes = UInt32(pcm.count * 2)
    data.append(contentsOf: Array("RIFF".utf8)); u32(36 + bytes); data.append(contentsOf: Array("WAVE".utf8))
    data.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(16000); u32(32000); u16(2); u16(16)
    data.append(contentsOf: Array("data".utf8)); u32(bytes)
    pcm.withUnsafeBufferPointer { data.append(Data(buffer: $0)) }
    try data.write(to: url)
}

func handleClip(_ obj: [String: Any]) {
    let id = (obj["id"] as? NSNumber)?.intValue ?? 0
    let src = obj["src"] as? String ?? "them"
    let start = (obj["start"] as? NSNumber)?.doubleValue
    guard let p = registry.get(src), let (samples, sr) = p.clip(from: start), !samples.isEmpty else {
        emit(["type": "clip", "id": id, "error": "no audio for \(src)"])
        return
    }
    let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("copilot-clip-\(id % 8).wav")
    do {
        try writeWav16k(samples, sampleRate: sr, to: url)
        emit(["type": "clip", "id": id, "path": url.path, "dur": (Double(samples.count) / sr * 100).rounded() / 100])
    } catch {
        emit(["type": "clip", "id": id, "error": "\(error)"])
    }
}

/// Reads JSON commands from stdin. EOF just stops reading; the listener keeps running.
func startCommandReader() {
    Thread.detachNewThread {
        while let line = readLine() {
            guard let d = line.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
                  let cmd = obj["cmd"] as? String
            else { continue }
            if cmd == "clip" { handleClip(obj) }
        }
    }
}

// MARK: - Model assets

func resolveLocale(_ id: String) async throws -> Locale {
    guard SpeechTranscriber.isAvailable else {
        throw ListenerError("SpeechTranscriber is not available on this Mac")
    }
    guard let loc = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: id)) else {
        throw ListenerError("locale \(id) not supported by SpeechTranscriber")
    }
    return loc
}

func ensureAssets(for locale: Locale) async throws {
    let probe = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
    let status = await AssetInventory.status(forModules: [probe])
    if status == .installed { return }
    if status == .unsupported { throw ListenerError("speech model for \(locale.identifier) unsupported") }
    if let req = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
        emit(["type": "status", "msg": "downloading on-device speech model for \(locale.identifier)…"])
        try await req.downloadAndInstall()
        emit(["type": "status", "msg": "speech model installed"])
    }
}

// MARK: - Microphone

final class MicCapture {
    let engine = AVAudioEngine()

    func start(into p: Pipeline) async throws {
        let granted = await AVCaptureDevice.requestAccess(for: .audio)
        if !granted {
            throw ListenerError("microphone permission denied")
        }
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else {
            throw ListenerError("no microphone input device")
        }
        input.installTap(onBus: 0, bufferSize: 2048, format: fmt) { buf, _ in
            p.feed(buf)
        }
        engine.prepare()
        try engine.start()
    }
}

// MARK: - System audio (ScreenCaptureKit)

func pcmBuffer(from sb: CMSampleBuffer) -> AVAudioPCMBuffer? {
    guard let desc = CMSampleBufferGetFormatDescription(sb),
          let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(desc),
          let fmt = AVAudioFormat(streamDescription: asbd)
    else { return nil }
    let n = CMSampleBufferGetNumSamples(sb)
    guard n > 0, let pcm = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(n)) else { return nil }
    pcm.frameLength = AVAudioFrameCount(n)
    let st = CMSampleBufferCopyPCMDataIntoAudioBufferList(
        sb, at: 0, frameCount: Int32(n), into: pcm.mutableAudioBufferList)
    return st == noErr ? pcm : nil
}

final class SystemCapture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private var stream: SCStream?
    private let pipeline: Pipeline
    private let audioQueue = DispatchQueue(label: "listener.sc.audio")
    private let screenQueue = DispatchQueue(label: "listener.sc.screen")

    init(pipeline: Pipeline) { self.pipeline = pipeline }

    func start() async throws {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        } catch {
            throw ListenerError("screen/system-audio recording permission denied (\(error.localizedDescription))")
        }
        guard let display = content.displays.first else { throw ListenerError("no display found") }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 48000
        cfg.channelCount = 1
        // We only want audio; keep the video side as cheap as possible.
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.showsCursor = false
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: screenQueue)
        try await s.startCapture()
        stream = s
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid, let pcm = pcmBuffer(from: sb) else { return }
        pipeline.feed(pcm)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        emit(["type": "error", "src": "them", "msg": "system audio stream stopped: \(error.localizedDescription)"])
    }
}

// MARK: - Modes

func runFile(path: String, realtime: Bool, locale: Locale, vocab: [String]) async throws {
    let url = URL(fileURLWithPath: path)
    let file = try AVAudioFile(forReading: url)
    let fmt = file.processingFormat
    let p = Pipeline(src: "them", locale: locale)
    try await p.start(vocab: vocab)
    registry.set("them", p)
    let chunk = AVAudioFrameCount(fmt.sampleRate * 0.1)
    while file.framePosition < file.length {
        guard let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: chunk) else { break }
        try file.read(into: buf, frameCount: chunk)
        if buf.frameLength == 0 { break }
        p.feed(buf)
        if realtime { try await Task.sleep(nanoseconds: 100_000_000) }
    }
    if realtime {
        // trailing silence, like a real pause after a question
        for _ in 0..<20 {
            if let z = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: chunk) {
                z.frameLength = chunk
                p.feed(z)
            }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
    }
    await p.finish()
    emit(["type": "eof"])
    if realtime { try await Task.sleep(nanoseconds: 4_000_000_000) }  // serve late clip requests
}

func runCheck(request: Bool, localeId: String) async {
    var report: [String: Any] = ["type": "check"]
    report["speechTranscriberAvailable"] = SpeechTranscriber.isAvailable
    do {
        let loc = try await resolveLocale(localeId)
        report["locale"] = loc.identifier
        let probe = SpeechTranscriber(locale: loc, preset: .progressiveTranscription)
        let st = await AssetInventory.status(forModules: [probe])
        report["speechModel"] = "\(st)"
    } catch {
        report["localeError"] = "\(error)"
    }
    var mic = AVCaptureDevice.authorizationStatus(for: .audio)
    if request, mic == .notDetermined {
        _ = await AVCaptureDevice.requestAccess(for: .audio)
        mic = AVCaptureDevice.authorizationStatus(for: .audio)
    }
    report["microphone"] = ["notDetermined", "restricted", "denied", "authorized"][min(Int(mic.rawValue), 3)]
    var screen = CGPreflightScreenCaptureAccess()
    if request, !screen {
        screen = CGRequestScreenCaptureAccess()
    }
    report["screenAndSystemAudioRecording"] = screen ? "authorized" : "not authorized"
    emit(report)
}

func runLive(source: String, locale: Locale, vocab: [String]) async {
    var started = 0
    var keep: [AnyObject] = []

    if source == "both" || source == "system" {
        let p = Pipeline(src: "them", locale: locale)
        do {
            try await p.start(vocab: vocab)
            registry.set("them", p)
            let cap = SystemCapture(pipeline: p)
            try await cap.start()
            keep.append(cap)
            keep.append(p)
            started += 1
        } catch {
            emit([
                "type": "error", "src": "them", "msg": "\(error)",
                "hint": "System Settings > Privacy & Security > Screen & System Audio Recording > enable your terminal app (Terminal/iTerm/VS Code), then fully quit and reopen it.",
            ])
        }
    }
    if source == "both" || source == "mic" {
        let p = Pipeline(src: "me", locale: locale)
        p.warnOnSilence = true
        do {
            try await p.start(vocab: vocab)
            registry.set("me", p)
            let mic = MicCapture()
            try await mic.start(into: p)
            keep.append(mic)
            keep.append(p)
            started += 1
        } catch {
            emit([
                "type": "error", "src": "me", "msg": "\(error)",
                "hint": "System Settings > Privacy & Security > Microphone > enable your terminal app, then restart it.",
            ])
        }
    }
    if started == 0 {
        emit(["type": "fatal", "msg": "no audio source could be started"])
        exit(2)
    }
    withExtendedLifetime(keep) {}
    // Run until killed.
    while true {
        try? await Task.sleep(nanoseconds: 3_600_000_000_000)
        withExtendedLifetime(keep) {}
    }
}

// MARK: - Main

@main
struct Listener {
    static func main() async {
        setvbuf(stdout, nil, _IOLBF, 0)
        var args = Array(CommandLine.arguments.dropFirst())
        func take(_ flag: String) -> String? {
            guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
            let v = args[i + 1]
            args.removeSubrange(i...(i + 1))
            return v
        }
        func has(_ flag: String) -> Bool {
            guard let i = args.firstIndex(of: flag) else { return false }
            args.remove(at: i)
            return true
        }
        let localeId = take("--locale") ?? "en-US"
        let source = take("--source") ?? "both"
        let filePath = take("--file")
        let vocab = (take("--vocab") ?? "")
            .split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let realtime = has("--realtime")
        let check = has("--check")
        let request = has("--request")

        if check {
            await runCheck(request: request, localeId: localeId)
            return
        }
        startCommandReader()
        do {
            let locale = try await resolveLocale(localeId)
            try await ensureAssets(for: locale)
            if let filePath {
                try await runFile(path: filePath, realtime: realtime, locale: locale, vocab: vocab)
            } else {
                await runLive(source: source, locale: locale, vocab: vocab)
            }
        } catch {
            emit(["type": "fatal", "msg": "\(error)"])
            exit(1)
        }
    }
}
