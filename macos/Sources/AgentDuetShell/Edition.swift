import Foundation

/// Which product this shell is (2026-10-03) — the daemon's `edition.py` says why there are three.
///
/// The RECORDER build is compiled with `-D RECORDER`, and every AI view — the assistant, its
/// message box, summaries, suggestions, captions, model downloads, speech settings — sits behind
/// `#if !RECORDER`. So in that build they are not hidden, they are not there: the code is never
/// compiled, and nothing of it is in the binary for a reviewer to find.
///
/// AGENTDUET AI is compiled with `-D AI_ONLY`: the AI half with no phone line. It reads the
/// recorder's calls from the AgentDuet folder, so it has no sign-in, no connection and no calls of
/// its own (`calls` is false); everything else is the full app's.
enum Edition {
    #if RECORDER
    static let recorder = true
    #else
    static let recorder = false
    #endif

    #if AI_ONLY
    static let aiOnly = true
    #else
    static let aiOnly = false
    #endif

    /// Whether this app has a phone line: it signs in, connects, and takes calls.
    static let calls = !aiOnly

    /// THE PRODUCT'S NAME, as the app says it in its own text: "AgentDuet Recorder" and
    /// "AgentDuet AI" for the single-purpose apps (Stanley, 2026-10-03), "AgentDuet" for the full
    /// one. The AgentDuet ACCOUNT and the Documents › AgentDuet folder keep their own names — those
    /// are not the app.
    #if RECORDER
    static let product = "AgentDuet Recorder"
    #elseif AI_ONLY
    static let product = "AgentDuet AI"
    #else
    static let product = "AgentDuet"
    #endif

    /// The instance folder under the home directory — the same names as `edition.home_name()`.
    #if RECORDER
    static let homeName = ".agentduet-recorder"
    #elseif AI_ONLY
    static let homeName = ".agentduet-ai"
    #else
    static let homeName = ".agentduet-desktop"
    #endif

    /// The app's own name, from its bundle — so a partner's build is named as its plist names it.
    static var appName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String
            ?? (recorder ? "AgentDuet Recorder" : aiOnly ? "AgentDuet AI" : "AgentDuet Desktop")
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
