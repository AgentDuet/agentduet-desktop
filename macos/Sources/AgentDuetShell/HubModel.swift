import AppKit
import Foundation

/// The native hub's state (2026-09-29): who has called or written, and what each conversation
/// holds. Read from `/api/threads`, the route the HTML hub reads, so the two show the same people.
///
/// This is the READING half. Replying and the assistant come next, then the in-app phone — so
/// until then the native hub is a preview window beside the working one.
@MainActor final class HubModel: ObservableObject {

    /// One call or one message, in the order the conversation happened.
    struct Item: Identifiable {
        let id: String
        let at: String
        let call: JSON?
        let message: JSON?
    }

    struct Turn: Identifiable {
        let id: Int
        let mine: Bool
        var text: String
    }

    @Published private(set) var people: [JSON] = []
    @Published private(set) var panel: JSON = [:]
    @Published var picked: String?
    @Published var search = ""
    /// Settings, which the app opens.
    var openSettings: (() -> Void)?
    @Published var notice: SettingsModel.Notice?

    let api: DaemonAPI
    private var timer: Timer?

    init(api: DaemonAPI) { self.api = api }

    func start() {
        Task { await load() }
        timer?.invalidate()
        // Five seconds, as the HTML hub polls: a message arriving, a transcript landing.
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.load() }
        }
    }

    func stop() { timer?.invalidate(); timer = nil }

    func load() async {
        async let t = api.get("/api/threads")
        async let p = api.get("/api/panel")
        let (threads, panel) = await (t, p)
        people = threads["people"] as? [JSON] ?? []
        self.panel = panel
        if picked == nil || !people.contains(where: { $0.str("who") == picked }) {
            picked = people.first?.str("who")
        }
        markSeen()
    }

    // MARK: - the list

    var shown: [JSON] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return people }
        return people.filter {
            $0.str("who").lowercased().contains(q) || $0.str("display").lowercased().contains(q)
        }
    }

    var person: JSON? { people.first { $0.str("who") == picked } }

    static func name(_ p: JSON) -> String {
        p.str("display").isEmpty ? p.str("who") : p.str("display")
    }

    static func initials(_ p: JSON) -> String {
        guard !p.str("display").isEmpty else { return "" }
        return p.str("display").split(separator: " ").prefix(2).compactMap { $0.first }
            .map(String.init).joined().uppercased()
    }

    static func counts(_ p: JSON) -> String {
        let c = (p["calls"] as? [JSON] ?? []).count, m = (p["messages"] as? [JSON] ?? []).count
        var bits: [String] = []
        if c > 0 { bits.append("\(c) call\(c == 1 ? "" : "s")") }
        if m > 0 { bits.append("\(m) message\(m == 1 ? "" : "s")") }
        return bits.isEmpty ? "No activity" : bits.joined(separator: " · ")
    }

    /// The number stays in sight under a name, so the owner can always check who it really is.
    static func subtitle(_ p: JSON) -> String {
        var bits: [String] = []
        let who = p.str("who")
        if !p.str("display").isEmpty, who.range(of: #"^\+?\d{6,15}$"#, options: .regularExpression) != nil {
            bits.append(who)
        }
        bits.append(counts(p))
        return bits.joined(separator: " · ")
    }

    // MARK: - the conversation

    /// INTERLEAVED BY TIME, since a person who rang and then wrote is one conversation. A call is
    /// placed by when it BEGAN, not when it was filed.
    func items(_ p: JSON) -> [Item] {
        let calls = (p["calls"] as? [JSON] ?? []).enumerated().map { i, c in
            Item(id: "c\(i)-\(c.str("call_id"))", at: c.str("started").isEmpty ? c.str("at") : c.str("started"),
                 call: c, message: nil)
        }
        let msgs = (p["messages"] as? [JSON] ?? []).enumerated().map { i, m in
            Item(id: "m\(i)-\(m.str("at"))", at: m.str("at"), call: nil, message: m)
        }
        return (calls + msgs).sorted { $0.at < $1.at }
    }

    /// A transcript as turns: one `you:` or `them:` line is a turn, and an unlabelled line
    /// belongs to the turn before it. None at all means an older, unlabelled transcript.
    static func turns(_ transcript: String) -> [Turn] {
        var out: [Turn] = []
        for line in transcript.split(separator: "\n", omittingEmptySubsequences: false) {
            let s = String(line)
            if s.hasPrefix("you:") || s.hasPrefix("them:") {
                let mine = s.hasPrefix("you:")
                let text = s.dropFirst(mine ? 4 : 5).trimmingCharacters(in: .whitespaces)
                out.append(Turn(id: out.count, mine: mine, text: text))
            } else if !out.isEmpty, !s.trimmingCharacters(in: .whitespaces).isEmpty {
                out[out.count - 1].text += "\n" + s
            }
        }
        return out
    }

    /// What a call with no transcript to show says instead, or nil when it has one.
    static func callState(_ c: JSON) -> String? {
        if c.bool("norecording") && c.bool("missed") { return c.bool("outgoing") ? "No answer." : "Missed call." }
        if c.bool("norecording") { return "No recording." }
        if c.bool("silent") { return "No audio." }
        if c.str("transcript").isEmpty { return "Transcript pending." }
        return nil
    }

    static func callMeta(_ c: JSON) -> String {
        var bits = [c.str("mode")]
        let files = Int(c.num("files"))
        if files > 0 { bits.append("\(files) file\(files == 1 ? "" : "s")") }
        if c.num("bytes") > 0 {
            bits.append(ByteCountFormatter.string(fromByteCount: Int64(c.num("bytes")), countStyle: .file))
        }
        return bits.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    // MARK: - the toolbar

    private var here: JSON { panel.obj("answer_here") }
    /// Only in carry mode, where there is a call to pass through.
    var carry: Bool { here.bool("carry") }
    var answerHere: Bool { here.bool("on") }
    /// The shell's own answer (MicWatch); "unknown" is treated as usable, as the daemon does.
    var micOK: Bool { ["ok", "unknown", ""].contains(here.str("mic")) }
    var micReason: String {
        switch here.str("mic") {
        case "lid": return "The lid is closed."
        case "none": return "No microphone found."
        case "blocked": return "Microphone access is off."
        case "silent": return "The microphone is silent."
        default: return "The microphone is ready."
        }
    }

    /// The owner's intent, saved as set; whether they can be heard is the light beside it.
    func setAnswerHere(_ on: Bool) {
        Task {
            _ = await api.post("/api/setup/setting", ["field": "answer_here", "value": on ? "yes" : "no"])
            await load()
        }
    }

    // MARK: - actions

    /// ON SCREEN IS SEEN: up to the newest item this window has.
    private func markSeen() {
        guard let p = person, p.num("unread") > 0 else { return }
        let newest = items(p).map(\.at).max() ?? ""
        Task { _ = await api.post("/api/seen", ["who": p.str("who"), "at": newest]) }
    }

    func pick(_ who: String?) {
        picked = who
        markSeen()
    }

    /// Edit's Save. Empty removes the typed name, so the Contacts name or the number shows again.
    func rename(_ name: String) async {
        guard let who = picked else { return }
        _ = await api.post("/api/name", ["who": who, "name": name])
        await load()
    }

    /// The calendar card: open it (it stays, and can be opened again) or dismiss it.
    func suggestion(_ key: String, _ action: String) {
        Task {
            let r = await api.post("/api/suggestion", ["key": key, "action": action])
            if action == "add", !r.str("message").isEmpty, !r.str("message").hasPrefix("Opened") {
                notice = .init(ok: false, text: r.str("message"))
            }
            await load()
        }
    }

    // MARK: - dates

    /// "29 Sep, 18:41", from the daemon's local ISO times ("2026-09-29T18:41:12", with or without
    /// fractional seconds, or with a space for the T).
    static func when(_ iso: String) -> String {
        guard iso.count >= 19 else { return iso }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let head = String(iso.prefix(19)).replacingOccurrences(of: " ", with: "T")
        guard let d = f.date(from: head) else { return iso }
        return d.formatted(.dateTime.day().month(.abbreviated).hour().minute())
    }
}
