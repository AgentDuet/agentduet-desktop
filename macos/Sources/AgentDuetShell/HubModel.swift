import AVFoundation
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

    /// NOT A PERSON, so pinned above the list rather than sorted into it.
    static let assistant = "__assistant__"
    /// What the window opens on: the assistant — or, in the recorder, which has none, the newest
    /// person (`load` picks them).
    static let home: String? = Edition.recorder ? nil : assistant

    @Published private(set) var people: [JSON] = []
    @Published private(set) var panel: JSON = [:]
    @Published var picked: String? = HubModel.home
    #if !RECORDER
    /// The assistant's conversation (run/owner_chat.json, on the daemon).
    @Published private(set) var turns: [JSON] = []
    /// What the assistant proposes and is waiting on the owner to approve.
    @Published private(set) var proposals: [JSON] = []
    #endif
    /// Whether someone is on a call now, or one that has just ended — the phone's to say.
    var onLine: (String) -> Bool = { _ in false }
    /// A call's recording, played from its card.
    let player = CallPlayer()
    #if !RECORDER
    /// The message box's text, and the question the assistant is answering right now.
    @Published var draft = ""
    @Published private(set) var busy = false
    @Published private(set) var pendingQuestion = ""
    #endif
    /// The last PERSON opened, so "her" and "this person" in a question resolve to them.
    private(set) var lastPerson = ""
    @Published var search = ""
    /// Settings, which the app opens — at a section, or where it was.
    var openSettings: ((String?) -> Void)?
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

    /// Closing the window stops a recording that is playing, too.
    func stop() { timer?.invalidate(); timer = nil; player.stop() }

    func load() async {
        async let t = api.get("/api/threads")
        async let p = api.get("/api/panel")
        let (threads, panel) = await (t, p)
        people = threads["people"] as? [JSON] ?? []
        self.panel = panel
        #if !RECORDER
        async let h = api.get("/api/chat_history")
        // PROPOSALS ARE ON DISK, not only in a chat reply: a card must survive a restart.
        async let q = api.get("/api/proposals")
        let (history, pending) = await (h, q)
        // NOT WHILE A TURN IS IN FLIGHT: the pending question is this window's own state.
        if !busy {
            turns = history["turns"] as? [JSON] ?? turns
            proposals = pending["proposals"] as? [JSON] ?? proposals
        }
        #endif
        // A CALLER ON THE LINE IS NOT YET IN THE LIST — a first-time caller is filed when the call
        // ends — so "not in the list" must not send the page back to the assistant while their
        // call is live or just ended (2026-10-01: in a demo it snapped back every five seconds).
        if picked != Self.assistant, !people.contains(where: { $0.str("who") == picked }),
           !(picked.map { onLine($0) } ?? false) {
            picked = Self.home
        }
        // THE RECORDER OPENS ON SOMEONE: it has no assistant page to stand on.
        if picked == nil, Edition.recorder { picked = people.first?.str("who") }
        markSeen()
    }

    // MARK: - the list

    var shown: [JSON] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return people }
        // A NUMBER AS PEOPLE TYPE IT — "9855 4074" finds +6598554074 — so digits match digits.
        let digits = q.filter(\.isNumber)
        return people.filter {
            $0.str("who").lowercased().contains(q) || $0.str("display").lowercased().contains(q)
                || (digits.count >= 3 && $0.str("who").filter(\.isNumber).contains(digits))
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
        #if !RECORDER
        if c.str("transcript").isEmpty { return "Transcript pending." }
        #endif
        return nil
    }

    /// What kind of call, and how long. Files and bytes are the recorder's business, not the
    /// owner's.
    static func callMeta(_ c: JSON) -> String {
        #if RECORDER
        let kind = "Voice call"
        #else
        let kind = c.str("mode") == "answered" ? "Answered by assistant" : "Voice call"
        #endif
        return c.num("seconds") > 0 ? "\(kind) · \(clock(c.num("seconds")))" : kind
    }

    static func clock(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    // MARK: - the connection

    /// The number this install is reached on: yours where set, else the line.
    var myNumber: String { panel.str("phone").isEmpty ? panel.str("line") : panel.str("phone") }
    var connected: Bool { ["live", "connecting", "retrying"].contains(panel.str("channel")) }
    /// NOT SIGNED IN IS NOT "NOT CONNECTED": with no credential the fix is to sign in.
    var connection: String {
        if connected { return "Connected" }
        return panel.str("channel") == "unset" ? "Not Signed In" : "Not Connected"
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
        guard !Quarantine.answerHere else { return }
        // ASKED NOW, not over a ringing call: macOS asks for the microphone the first time, and
        // that prompt belongs to the moment the owner opted in.
        if on, AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in AppDelegate.comeBack() }   // the prompt took the focus away
            }
        }
        Task {
            _ = await api.post("/api/setup/setting", ["field": "answer_here", "value": on ? "yes" : "no"])
            await load()
        }
    }

    // MARK: - actions

    /// ON SCREEN IS SEEN: up to the newest item this window has.
    private func markSeen() {
        guard let p = person, p.num("unread") > 0 else { return }
        // BY THE TIME THE DAEMON COUNTS WITH: a call's `at`, when it was filed at hang-up — not
        // `started`, which orders the page. Sent as `started`, the newest call was always later
        // than "seen" and its badge could not be cleared (Cen, 2026-09-30).
        let calls = (p["calls"] as? [JSON] ?? []).map { $0.str("at") }
        let msgs = (p["messages"] as? [JSON] ?? []).map { $0.str("at") }
        let newest = (calls + msgs).max() ?? ""
        Task { _ = await api.post("/api/seen", ["who": p.str("who"), "at": newest]) }
    }

    func pick(_ who: String?) {
        #if !RECORDER
        if who != picked { drawerOpen = false }
        #endif
        picked = who
        if let who, who != Self.assistant { lastPerson = who }
        notice = nil
        markSeen()
    }

    var onAssistant: Bool { picked == Self.assistant }

    #if !RECORDER
    /// The assistant's panel at the foot of a person's page: open over their history, or just its
    /// header and the box. Sending opens it, so the answer is seen.
    @Published var drawerOpen = false
    #endif

    #if !RECORDER
    // MARK: - the message box

    /// Whether the assistant can answer: a model is attached.
    var modelReady: Bool { panel.obj("model").bool("configured") }

    var placeholder: String {
        if modelReady {
            if let p = person { return "Ask about \(Self.name(p))…" }
            return "Ask about your calls…"
        }
        // `job` is JSON null when nothing is downloading, which is not Swift's nil.
        return panel.obj("model").obj("pick")["job"] is JSON ? "" : "Download the model in Settings"
    }

    var canSend: Bool {
        !busy && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && modelReady
    }

    /// ALWAYS A QUESTION TO THE ASSISTANT (2026-09-30), with the person open as its context. The
    /// owner never types straight to a person: the assistant prepares a reply or an email, and the
    /// owner approves it — one way to reach anyone, whichever channel it goes out on.
    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canSend else { return }
        draft = ""
        busy = true
        notice = nil
        if !onAssistant { drawerOpen = true }
        Task {
            defer { busy = false; pendingQuestion = "" }
            pendingQuestion = text
            let r = await api.post("/api/chat", ["message": text, "viewing": lastPerson])
            proposals = r["proposals"] as? [JSON] ?? proposals
            if r["reply"] == nil, r["ok"] as? Bool == false {
                notice = .init(ok: false, text: r.str("message"))
            }
            let history = await api.get("/api/chat_history")
            turns = history["turns"] as? [JSON] ?? turns
        }
    }

    #endif

    /// A phone number, as the daemon's identities are: an optional "+" and six or more digits.
    static func isNumber(_ who: String) -> Bool {
        let digits = who.hasPrefix("+") ? String(who.dropFirst()) : who
        return digits.count >= 6 && digits.allSatisfy(\.isNumber)
    }

    /// A new contact for this number, opened in Contacts' editor — or, without Contacts access,
    /// a card Contacts asks to add. Only a failure is worth saying: what opens is the outcome.
    func addContact(_ who: String, name: String = "", edit: Bool = true) {
        if ContactsAdd.addAndEdit(number: who, name: name, edit: edit) { return }
        Task {
            let r = await api.post("/api/contacts/add", ["who": who])
            if r["ok"] as? Bool == false { notice = .init(ok: false, text: r.str("message")) }
        }
    }

    #if !RECORDER
    // MARK: - the assistant's drafts and proposals

    /// Who a draft would go to: the last person opened, else the one person waiting for a reply.
    var replyTarget: JSON? {
        if !lastPerson.isEmpty { return people.first { $0.str("who") == lastPerson } }
        let waiting = people.filter { p in
            (p["messages"] as? [JSON] ?? []).contains { !$0.str("them").isEmpty && $0.str("us").isEmpty }
        }
        return waiting.count == 1 ? waiting[0] : nil
    }

    /// THROUGH THE SAME GUARDED PATH as typing "send it", so there is one set of checks.
    func sendDraft() {
        guard let who = replyTarget else { return }
        busy = true
        Task {
            _ = await api.post("/api/chat", ["message": "send it", "viewing": who.str("who")])
            let history = await api.get("/api/chat_history")
            turns = history["turns"] as? [JSON] ?? turns
            busy = false
            await load()
        }
    }

    func decide(_ proposal: JSON, approve: Bool) {
        Task {
            let r = await api.post("/api/proposal", ["id": proposal.str("id"), "approve": approve])
            proposals = r["proposals"] as? [JSON] ?? []
            // SAY WHAT HAPPENED, or a refused tool looks exactly like one that worked.
            let m = r.str("message")
            if approve, !m.isEmpty {
                let bad = ["Not opened", "Could not", "Cannot", "tool error", "Unknown tool", "That proposal"]
                    .contains { m.hasPrefix($0) }
                notice = .init(ok: !bad, text: m)
            }
        }
    }

    /// Correct… on the Summary card. The model rewrites the summary around the owner's words, so
    /// this takes seconds; nil on success, else what went wrong.
    func correctSummary(_ who: String, _ correction: String) async -> String? {
        let r = await api.post("/api/summary/correct", ["who": who, "correction": correction])
        await load()
        return r["ok"] as? Bool == true ? nil : (r.str("message").isEmpty ? "The summary was not changed." : r.str("message"))
    }
    #endif

    /// Edit's Save. Empty removes the typed name, so the Contacts name or the number shows again.
    func rename(_ name: String) async {
        guard let who = picked else { return }
        _ = await api.post("/api/name", ["who": who, "name": name])
        await load()
    }

    #if !RECORDER
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
    #endif

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
