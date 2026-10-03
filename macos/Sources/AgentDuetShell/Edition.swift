import Foundation

/// Which product this shell is (2026-10-03) — the daemon's `edition.py` says why there are two.
///
/// The RECORDER build is compiled with `-D RECORDER`, and every AI view — the assistant, its
/// message box, summaries, suggestions, captions, model downloads, speech settings — sits behind
/// `#if !RECORDER`. So in that build they are not hidden, they are not there: the code is never
/// compiled, and nothing of it is in the binary for a reviewer to find.
enum Edition {
    #if RECORDER
    static let recorder = true
    #else
    static let recorder = false
    #endif

    /// The app's own name, from its bundle — so a partner's build is named as its plist names it.
    static var appName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String
            ?? (recorder ? "AgentDuet Recorder" : "AgentDuet Desktop")
    }

    /// What the app keeps in the owner's folder, for the sentences that name it.
    #if RECORDER
    static let kept = "recordings"
    static let notInLogs = "Recordings and passwords are not included."
    #else
    static let kept = "recordings and transcripts"
    static let notInLogs = "Recordings, transcripts and passwords are not included."
    #endif
}
