import AVFoundation
import Foundation

/// Plays a call's recording from its card (2026-09-30).
///
/// ONE AT A TIME. Playing another pauses this one, and each keeps its place, so going back to it
/// resumes rather than starting over.
///
/// NEVER OVER A CALL. While one rings or is on, playback is paused and cannot be resumed until it
/// ends — the recording would otherwise play into the owner's ear, or down the line.
@MainActor final class CallPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {

    /// The call loaded in the player, playing or paused; nil when none is.
    @Published private(set) var callID: String?
    @Published private(set) var playing = false
    @Published private(set) var position: Double = 0
    /// A call is ringing or on.
    @Published private(set) var blocked = false

    private var player: AVAudioPlayer?
    private var timer: Timer?
    /// Where each paused recording was left.
    private var places: [String: Double] = [:]

    /// Why the last press played nothing, shown on that call's card. A press that silently did
    /// nothing is what was reported (2026-09-30), so every refusal below says something.
    @Published private(set) var problem: (callID: String, text: String)?

    func toggle(callID: String, path: String) {
        problem = nil
        guard !blocked else {
            problem = (callID, "Not while a call is ringing or on")
            return
        }
        if self.callID == callID, let player {
            if player.isPlaying { pause() } else { resume() }
            return
        }
        pause()
        let p: AVAudioPlayer
        do { p = try AVAudioPlayer(contentsOf: URL(fileURLWithPath: path)) } catch {
            problem = (callID, "Cannot play this recording (\(error.localizedDescription))")
            return
        }
        p.delegate = self
        p.currentTime = places[callID] ?? 0
        player = p
        self.callID = callID
        resume()
    }

    /// Play this call's recording from `seconds` — a turn of its transcript was clicked.
    func play(callID: String, path: String, from seconds: Double) {
        places[callID] = seconds
        if self.callID == callID, let player {
            player.currentTime = seconds
            position = seconds
            if !player.isPlaying { resume() }
            return
        }
        if self.callID != nil { pause() }
        self.callID = nil                           // so toggle loads it, at the place just set
        toggle(callID: callID, path: path)
    }

    /// Where this call's recording is, played or paused.
    func place(of callID: String) -> Double {
        self.callID == callID ? position : places[callID] ?? 0
    }

    /// Set from the phone's state.
    func setBlocked(_ on: Bool) {
        guard on != blocked else { return }
        blocked = on
        if on { pause() }
    }

    private func resume() {
        guard let player, !blocked else { return }
        guard player.play() else {
            problem = callID.map { ($0, "Playback did not start — check the sound output") }
            return
        }
        playing = true
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.position = self?.player?.currentTime ?? 0 }
        }
    }

    private func pause() {
        timer?.invalidate(); timer = nil
        guard let player else { return }
        player.pause()
        playing = false
        position = player.currentTime
        if let callID { places[callID] = position }
    }

    /// The window closed: stop, and forget every place.
    func stop() {
        timer?.invalidate(); timer = nil
        player?.stop()
        player = nil
        callID = nil
        playing = false
        position = 0
        places = [:]
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            // Played to the end: next time it starts again.
            if let id = self.callID { self.places[id] = nil }
            self.timer?.invalidate(); self.timer = nil
            self.player = nil
            self.callID = nil
            self.playing = false
            self.position = 0
        }
    }
}
