import AVFoundation
import AppKit
import Foundation

/// The in-app phone, natively (2026-09-29): a carried call rings here, and answering bridges the
/// caller to this Mac's microphone and speaker.
///
/// THE SAME SOCKET THE PAGE USED, `/api/phone`, so the daemon needs no change: JSON text frames
/// carry the call's state and the live captions; binary frames carry audio both ways as 24 kHz
/// mono 16-bit PCM, what the SDK carries. The daemon rings every open client, a page or this.
///
/// ECHO CANCELLATION is the audio engine's voice processing — what the browser's
/// `echoCancellation` gave the page — so the caller does not hear themselves from the speaker.
/// ANSWERING IN THE APP IS QUARANTINED (Stanley, 2026-09-30): the use case is mobile MITM — every
/// call is carried through to the owner's mobile and recorded in the middle; nobody talks into the
/// Mac. Mirrors `owner.ANSWER_HERE_QUARANTINED` in the daemon.
///
/// THE APP HOLDS NO MICROPHONE PERMISSION while this is true — no entitlement, no usage string —
/// and macOS KILLS an app that asks for the microphone without a usage string. So every place
/// that asks checks this first. Bringing the feature back is this flag, the daemon's, the
/// `audio-input` entitlement and `NSMicrophoneUsageDescription`, together.
enum Quarantine {
    static let answerHere = true
}

@MainActor final class PhoneModel: ObservableObject {

    /// "idle", "ringing" or "live", as the daemon says.
    @Published private(set) var state = "idle"
    @Published private(set) var from = ""
    @Published private(set) var since: Double = 0
    /// This app answered it, rather than another window.
    @Published private(set) var mine = false
    @Published var muted = false { didSet { audio?.muted = muted } }
    @Published var error = ""
    /// The last answer failed because microphone access is off.
    @Published private(set) var needsMicSetting = false
    /// Live captions by call id: who, when it began, whether it ended, and the captions so far.
    @Published private(set) var live: [String: LiveCall] = [:]

    struct Caption: Identifiable { let id: Int; let mine: Bool; let at: Double; let text: String }
    struct LiveCall { var who: String; var started: Double; var ended: Bool; var captions: [Caption] }

    /// A call is ringing, or on — here or carried through — so nothing else may play.
    var busy: Bool { state != "idle" || live.values.contains { !$0.ended } }

    /// A call has started ringing: the window must be in front to answer it.
    var onRing: (() -> Void)?

    private let api: DaemonAPI
    private var socket: URLSessionWebSocketTask?
    private var audio: PhoneAudio?
    private var ringTimer: Timer?
    private var stopped = false

    init(api: DaemonAPI) { self.api = api }

    // MARK: - the socket

    func start() {
        stopped = false
        connect()
    }

    func stop() {
        stopped = true
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        endAudio()
        ring(false)
    }

    private func connect() {
        guard !stopped else { return }
        var c = URLComponents(url: api.base.appendingPathComponent("/api/phone"), resolvingAgainstBaseURL: false)!
        c.scheme = c.scheme == "https" ? "wss" : "ws"
        c.queryItems = [URLQueryItem(name: "t", value: api.token)]
        let task = URLSession.shared.webSocketTask(with: c.url!)
        socket = task
        task.resume()
        receive(task)
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.socket === task else { return }
                switch result {
                case .success(.data(let data)): self.audio?.play(data)
                case .success(.string(let text)): self.handle(text)
                case .success: break
                case .failure:
                    // GONE: nothing is ringing any more, and the daemon may be restarting.
                    self.reset()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.connect() }
                    return
                }
                self.receive(task)
            }
        }
    }

    private func send(_ json: JSON) {
        guard let data = try? JSONSerialization.data(withJSONObject: json),
              let text = String(data: data, encoding: .utf8) else { return }
        socket?.send(.string(text)) { _ in }
    }

    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let m = (try? JSONSerialization.jsonObject(with: data)) as? JSON else { return }
        switch m.str("type") {
        // LIVE CAPTIONS share this socket and are not the phone's own state.
        case "live_calls":
            live = [:]
            for c in m["calls"] as? [JSON] ?? [] {
                live[c.str("call")] = LiveCall(who: c.str("who"), started: c.num("started"), ended: false,
                                               captions: (c["captions"] as? [JSON] ?? []).enumerated().map(Self.caption))
            }
        case "live_start":
            live[m.str("call")] = LiveCall(who: m.str("who"), started: m.num("started"), ended: false, captions: [])
        case "live_end":
            live[m.str("call")]?.ended = true
        case "caption":
            if var c = live[m.str("call")] {
                c.captions.append(Self.caption((c.captions.count, m)))
                live[m.str("call")] = c
            }
        default:
            state = m.str("type").isEmpty ? "idle" : m.str("type")
            from = m.str("from")
            since = m.num("since")
            if state != "live" { mine = false; muted = false; endAudio() }
            if state == "ringing" {
                dismissError()
                // IN FRONT, SO IT CAN BE ANSWERED: the call rings for only a few seconds.
                onRing?()
            }
            ring(state == "ringing")
        }
    }

    private static func caption(_ item: (Int, JSON)) -> Caption {
        Caption(id: item.0, mine: item.1.str("leg") == "callee", at: item.1.num("at"), text: item.1.str("text"))
    }

    private func reset() {
        state = "idle"; mine = false; muted = false
        endAudio(); ring(false)
    }

    // MARK: - the call

    func answer() {
        // The daemon never rings the app while quarantined; this is the belt to that brace.
        guard !Quarantine.answerHere else { return }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            startAnswering()
        case .notDetermined:
            // NEVER ASKED, which is where every new build's bundle id starts: ask now, while it
            // rings. Failing here instead turned the owner's Answer into a decline with no prompt
            // (2026-09-30, the dev build).
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                Task { @MainActor in
                    AppDelegate.comeBack()      // the prompt took the focus away
                    guard let self, self.state == "ringing" else { return }
                    if granted { self.startAnswering() } else { self.cannotAnswer(PhoneAudio.Failure.refused) }
                }
            }
        default:
            cannotAnswer(PhoneAudio.Failure.refused)
        }
    }

    private func startAnswering() {
        do {
            let audio = PhoneAudio()
            // THE TASK ITSELF, not this model: capture runs on the audio thread, and a
            // URLSessionWebSocketTask may be sent to from any thread.
            let task = socket
            audio.send = { data in task?.send(.data(data)) { _ in } }
            try audio.start()
            self.audio = audio
            mine = true
            send(["type": "answer"])
        } catch {
            cannotAnswer(error)
        }
    }

    /// The call cannot be taken here, so it passes through rather than ringing out. SAID, not
    /// swallowed: on screen until dismissed (the call bar would otherwise vanish with the call),
    /// and in the daemon's log, which only saw "decline in the app".
    private func cannotAnswer(_ failure: Error) {
        endAudio()
        needsMicSetting = (failure as? PhoneAudio.Failure) == .refused
        error = failure.localizedDescription
        send(["type": "diag", "answer": "failed", "error": error])
        send(["type": "decline"])
    }

    /// Access is off, so the fix is in System Settings.
    func openMicSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
        else { return }
        NSWorkspace.shared.open(url)
    }

    func dismissError() { error = ""; needsMicSetting = false }

    func decline() { send(["type": "decline"]) }
    func hangUp() { send(["type": "hangup"]) }

    private func endAudio() {
        audio?.stop()
        audio = nil
    }

    private func ring(_ on: Bool) {
        ringTimer?.invalidate()
        ringTimer = nil
        guard on else { return }
        let beep = { NSSound(named: "Glass")?.play() }
        _ = beep()
        ringTimer = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { _ in _ = beep() }
    }

    /// Live calls for one person, not yet in their history.
    func liveCalls(for who: String, history: [JSON]) -> [(String, LiveCall)] {
        live.filter { id, c in c.who == who && !history.contains { $0.str("call_id") == id } }
            .sorted { $0.value.started < $1.value.started }
    }

    func onACall(_ who: String) -> Bool { live.values.contains { $0.who == who && !$0.ended } }
}

/// The microphone and speaker for one call.
///
/// Capture: the input node with voice processing on, converted to the wire's 24 kHz mono Int16.
/// Playback: the caller's audio, as Float32 at 24 kHz into a player node the mixer resamples.
final class PhoneAudio {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let wire = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: true)!
    private let wireFloat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false)!
    private var converter: AVAudioConverter?
    var send: ((Data) -> Void)?
    /// Set from the main thread, read on the audio thread; a stale read costs one 20 ms frame.
    var muted = false

    /// Why the microphone could not be started, in words the call bar shows as they are.
    enum Failure: LocalizedError, Equatable {
        case refused
        case noInput
        case step(String, String)

        var errorDescription: String? {
            switch self {
            case .refused:
                return "Microphone access is off for this app — turn it on in System Settings"
            case .noInput:
                return "No microphone input is available"
            case .step(let step, let reason):
                return "Could not start the microphone (\(step): \(reason))"
            }
        }
    }

    func start() throws {
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            throw Failure.refused
        }
        let input = engine.inputNode
        do { try input.setVoiceProcessingEnabled(true) } catch {
            throw Failure.step("echo cancellation", error.localizedDescription)
        }
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, let converter = AVAudioConverter(from: inFormat, to: wire) else {
            throw Failure.noInput
        }
        converter.downmix = true
        self.converter = converter
        // ~20 ms at the device's rate: small enough for latency, large enough not to flood.
        let frames = AVAudioFrameCount(inFormat.sampleRate * 0.02)
        input.installTap(onBus: 0, bufferSize: frames, format: inFormat) { [weak self] buffer, _ in
            self?.capture(buffer)
        }
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: wireFloat)
        do { try engine.start() } catch {
            throw Failure.step("audio engine", error.localizedDescription)
        }
        player.play()
    }

    private func capture(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 24000 / buffer.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: capacity) else { return }
        var fed = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard err == nil, out.frameLength > 0, let samples = out.int16ChannelData?[0] else { return }
        let count = Int(out.frameLength)
        if muted { samples.update(repeating: 0, count: count) }
        send?(Data(bytes: samples, count: count * 2))
    }

    func play(_ data: Data) {
        let count = data.count / 2
        guard count > 0, let buffer = AVAudioPCMBuffer(pcmFormat: wireFloat, frameCapacity: AVAudioFrameCount(count)),
              let out = buffer.floatChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(count)
        data.withUnsafeBytes { raw in
            let pcm = raw.bindMemory(to: Int16.self)
            for i in 0..<count { out[i] = Float(pcm[i]) / 32768 }
        }
        player.scheduleBuffer(buffer, completionHandler: nil)
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        player.stop()
        engine.stop()
    }
}
