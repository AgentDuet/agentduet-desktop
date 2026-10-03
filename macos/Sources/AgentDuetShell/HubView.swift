import SwiftUI

/// The hub (2026-09-29), on the Contacts layout: a plain list on the left — the Personal Assistant
/// pinned first, then everyone who has called or written — and the chosen one in a rounded box on
/// the right, with the message box at its foot. The toolbar holds AgentDuet's own controls.
struct HubView: View {
    @ObservedObject var model: HubModel
    @ObservedObject var phone: PhoneModel

    var body: some View {
        VStack(spacing: 0) {
            CallBar(phone: phone, model: model)
            HStack(spacing: 0) {
                PeopleList(model: model, phone: phone)
                    .frame(width: 260)
                PersonBox(model: model, phone: phone)
                    .padding(10)
            }
        }
        .frame(minWidth: 820, minHeight: 520)
        // A CALL INTERRUPTS A RECORDING, and holds it paused until the call ends. (The player
        // lives only while this window does — closing it stops playback — so this is enough.)
        .onAppear { model.player.setBlocked(phone.busy) }
        .onChange(of: phone.busy) { model.player.setBlocked($0) }
        // CONTACTS' LOOK, AGENTDUET'S CONTROLS (Stanley, 2026-09-29): whether you can be heard,
        // answering here, and Settings.
        // TWO CONTROLS, EACH IN ITS OWN PILL, as Contacts groups its toolbar: answering here
        // (with whether you can be heard), and Settings. Side by side in one group they crowded.
        .toolbar {
            // THE CONNECTION, IN THE TITLE BAR beside the name (Stanley, 2026-09-29): whether
            // anything can reach you, and the way to change it — Settings at Account.
            // The window's own title is hidden (HubWindow), so the name and the status read in
            // order: "AgentDuet ● Connected".
            // A TITLE, NOT A BUTTON: macOS 26 puts every toolbar item in a glass pill, and the
            // name and status sat in one that ended at the last letter. No pill for the title.
            if #available(macOS 26.0, *) {
                ToolbarItem(placement: .navigation) { titleAndStatus }
                    .sharedBackgroundVisibility(.hidden)
            } else {
                ToolbarItem(placement: .navigation) { titleAndStatus }
            }
            // THE STRETCH the hidden title used to give, so the controls below keep to the right.
            if #available(macOS 26.0, *) { ToolbarSpacer(.flexible) }
            if model.carry {
                ToolbarItem(placement: .primaryAction) {
                    HStack(spacing: 8) {
                        MicLight(ok: model.micOK, reason: model.micReason)
                        // THE LABEL AS TEXT: a toolbar hides a toggle's own label.
                        Text("Answer Calls Here")
                        Toggle("Answer Calls Here", isOn: Binding(
                            get: { model.answerHere }, set: { model.setAnswerHere($0) }))
                            .toggleStyle(.switch).labelsHidden().controlSize(.small)
                    }
                    // ONE PILL, THE SYSTEM'S: macOS draws it around the item, and a capsule of our
                    // own inside it made a second, cramped border. The padding is its room.
                    .padding(.horizontal, 8)
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Button { model.openSettings?(nil) } label: { Label("Settings", systemImage: "gearshape") }
                    .help("Settings")
            }
        }
    }
}

extension HubView {
    /// "AgentDuet ● Connected": the name, and whether anything can reach you — which opens
    /// Settings at Account, with the number on hover.
    var titleAndStatus: some View {
        HStack(spacing: 10) {
            Text(Edition.product).font(.headline).fixedSize()
            Button { model.openSettings?("account") } label: {
                HStack(spacing: 6) {
                    Circle().fill(model.connected ? Color.green : Color.secondary)
                        .frame(width: 7, height: 7)
                    // ITS OWN WIDTH: the toolbar gives an item a width, and cut
                    // "Connected" to "Connect…".
                    Text(model.connection).foregroundStyle(.secondary).fixedSize()
                }
            }
            .buttonStyle(.plain)
            .help(model.connected && !model.myNumber.isEmpty ? model.myNumber : model.connection)
        }
    }
}

// MARK: - the microphone light

/// Green when you can be heard, red when not, and the reason the moment the pointer is over it —
/// a tooltip waits a second, which is a second too long for the one thing it explains.
private struct MicLight: View {
    let ok: Bool
    let reason: String
    @StateObject private var hovering = Local(false)

    var body: some View {
        Image(systemName: ok ? "mic.fill" : "mic.slash.fill")
            .foregroundStyle(ok ? Color.green : Color.red)
            .onHover { hovering.value = $0 }
            .popover(isPresented: $hovering.value, arrowEdge: .bottom) {
                Text(reason).padding(.horizontal, 12).padding(.vertical, 8)
            }
    }
}

// MARK: - the call bar

/// A call ringing here, or live: who, how long, and what can be done about it.
private struct CallBar: View {
    @ObservedObject var phone: PhoneModel
    @ObservedObject var model: HubModel

    var body: some View {
        if phone.state == "ringing" || phone.state == "live" {
            HStack(spacing: 10) {
                Image(systemName: "phone.fill").foregroundStyle(.green)
                Text(phone.state == "ringing" ? "Incoming call"
                     : phone.mine ? "On a call with" : "Answered in another window")
                Text(name).bold()
                if phone.state == "live" { Elapsed(since: phone.since).foregroundStyle(.secondary) }
                if !phone.error.isEmpty { Text(phone.error).foregroundStyle(.red) }
                Spacer()
                if phone.state == "ringing" {
                    Button("Decline") { phone.decline() }
                    Button("Answer") { phone.answer() }.keyboardShortcut(.defaultAction).tint(.green)
                        .buttonStyle(.borderedProminent)
                } else if phone.mine {
                    Button(phone.muted ? "Unmute" : "Mute") { phone.muted.toggle() }
                    Button("Hang Up") { phone.hangUp() }.tint(.red).buttonStyle(.borderedProminent)
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(Color.green.opacity(0.12))
        } else if !phone.error.isEmpty {
            // THE ANSWER FAILED and the call passed through: the bar stays to say why, or the
            // owner sees nothing happen while the caller keeps ringing.
            HStack(spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Text("Could not answer").bold()
                Text(phone.error).foregroundStyle(.secondary)
                Spacer()
                if phone.needsMicSetting { Button("Open System Settings") { phone.openMicSettings() } }
                Button("Dismiss") { phone.dismissError() }
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(Color.orange.opacity(0.12))
        }
    }

    /// The caller's name where there is one.
    private var name: String {
        if let p = model.people.first(where: { $0.str("who") == phone.from }) { return HubModel.name(p) }
        return phone.from
    }
}

private struct Elapsed: View {
    let since: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            let s = max(0, Int(ctx.date.timeIntervalSince1970 - since))
            Text(String(format: "%d:%02d", s / 60, s % 60)).monospacedDigit()
        }
    }
}

// MARK: - the list

private struct PeopleList: View {
    @ObservedObject var model: HubModel
    @ObservedObject var phone: PhoneModel

    /// THE ASSISTANT AND THE SEARCH STAY; ONLY THE PEOPLE SCROLL (Stanley, 2026-09-30). The search
    /// sits where the separator was — the assistant's row is set apart enough by its own look.
    var body: some View {
        VStack(spacing: 6) {
            #if !RECORDER
            row(on: model.onAssistant, tap: { model.pick(HubModel.assistant) }) {
                HStack(spacing: 10) {
                    ZStack {
                        Circle().fill(LinearGradient(colors: [.purple, .blue], startPoint: .top, endPoint: .bottom))
                        Image(systemName: "sparkles").foregroundStyle(.white).font(.system(size: 15))
                    }
                    .frame(width: 32, height: 32)
                    Text("Personal Assistant").font(.body.weight(.semibold))
                    Spacer()
                }
                .padding(.vertical, 6)
            }
            #endif
            SearchField(text: $model.search)
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(everyone.indices, id: \.self) { i in
                        let p = everyone[i]
                        let on = p.str("who") == model.picked
                        row(on: on, tap: { model.pick(p.str("who")) }) {
                            PersonRow(person: p, picked: on, onCall: phone.onACall(p.str("who")))
                        }
                    }
                    if everyone.isEmpty {
                        Text(model.search.isEmpty ? "Nobody yet." : "No one matches.")
                            .foregroundStyle(.secondary).padding(.top, 8)
                    }
                }
            }
        }
        .padding(8)
    }

    /// EVERYONE, plus anyone on a call right now who has no history yet — a first-time caller
    /// has no row until the call is filed, and the whole point is to show them while it is on.
    private var everyone: [JSON] {
        var list = model.shown
        // A caller on the line is shown whatever the search: they are why the window came forward.
        for c in phone.live.values where !c.ended && !list.contains(where: { $0.str("who") == c.who }) {
            list.insert(["who": c.who, "display": "", "calls": [JSON](), "messages": [JSON](), "last": ""], at: 0)
        }
        return list
    }

    /// ROWS WITH THEIR OWN HIGHLIGHT, as Contacts draws them: a rounded grey band on the chosen one.
    private func row<C: View>(on: Bool, tap: @escaping () -> Void, @ViewBuilder _ content: () -> C) -> some View {
        content()
            .padding(.horizontal, 8)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(on ? Color.primary.opacity(0.12) : Color.clear))
            .contentShape(Rectangle())
            .onTapGesture(perform: tap)
    }
}

private struct PersonRow: View {
    let person: JSON
    let picked: Bool
    let onCall: Bool

    var body: some View {
        HStack(spacing: 10) {
            Avatar(person: person, size: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text(HubModel.name(person)).font(.body.weight(.semibold)).lineLimit(1)
                if onCall {
                    Label("On a call", systemImage: "phone.fill").font(.caption).foregroundStyle(.green)
                } else {
                    Text("\(HubModel.counts(person)) · \(HubModel.when(person.str("last")))")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 4)
            // NEW SINCE LAST LOOKED, until the owner opens that person.
            let unread = Int(person.num("unread"))
            if unread > 0 && !picked {
                Text(unread > 99 ? "99+" : "\(unread)")
                    .font(.caption2.bold()).foregroundStyle(.white)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(Color.blue))
            }
        }
        .padding(.vertical, 6)
    }
}

/// Initials where there is a name; a person glyph for a bare number.
private struct Avatar: View {
    let person: JSON
    let size: CGFloat

    var body: some View {
        let initials = HubModel.initials(person)
        ZStack {
            Circle().fill(LinearGradient(colors: [Color(white: 0.62), Color(white: 0.45)],
                                         startPoint: .top, endPoint: .bottom))
            if initials.isEmpty {
                Image(systemName: "person.fill").font(.system(size: size * 0.5)).foregroundStyle(.white)
            } else {
                Text(initials).font(.system(size: size * 0.4, weight: .semibold)).foregroundStyle(.white)
            }
        }
        .frame(width: size, height: size)
    }
}

/// The list's search, drawn as Contacts draws its own: a rounded pill with the magnifier, as
/// tall as the rows around it. (AppKit's search field keeps a fixed height, which sat cramped.)
/// Cmd-F comes to it; Esc clears it and leaves.
private struct SearchField: View {
    @Binding var text: String
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("Search", text: $text).textFieldStyle(.plain).focused($focused)
            if !text.isEmpty {
                Button { text = "" } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.borderless).foregroundStyle(.secondary).help("Clear")
            }
        }
        .padding(.horizontal, 10)
        .frame(height: 30)
        .background(Capsule().fill(Color.primary.opacity(0.07)))
        .overlay(Capsule().strokeBorder(Color.accentColor.opacity(focused ? 0.6 : 0), lineWidth: 2))
        .onExitCommand { text = ""; focused = false }
        .background { Button("") { focused = true }.keyboardShortcut("f").opacity(0) }
        // NOT THE FIRST THING TYPED INTO: a window hands its first field the focus, which put the
        // owner's first keystrokes in the search rather than the message box.
        .onAppear { DispatchQueue.main.async { focused = false } }
    }
}

// MARK: - the box

private struct PersonBox: View {
    @ObservedObject var model: HubModel
    @ObservedObject var phone: PhoneModel

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
                .shadow(color: .black.opacity(0.12), radius: 3, y: 1)
            VStack(spacing: 0) {
                #if !RECORDER
                if model.onAssistant {
                    AssistantPane(model: model)
                    Composer(model: model)
                }
                #endif
                if model.onAssistant {
                    EmptyView()
                } else if let p = model.person {
                    Conversation(model: model, phone: phone, person: p)
                        .assistantPanel(model)
                } else if let who = model.picked {
                    // A first-time caller, on a call: nothing filed yet but the call itself.
                    Conversation(model: model, phone: phone,
                                 person: ["who": who, "display": "", "calls": [JSON](), "messages": [JSON]()])
                        .assistantPanel(model)
                } else {
                    // THE RECORDER, before anyone has called: there is no assistant to open on.
                    Text("Nobody yet.").foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
    }
}

extension View {
    /// The assistant at the foot of a person's page — in the full edition. The recorder has none.
    @ViewBuilder func assistantPanel(_ model: HubModel) -> some View {
        #if RECORDER
        self
        #else
        modifier(WithAssistant(model: model))
        #endif
    }
}

// MARK: - the message box

#if !RECORDER
/// One box, whose meaning is whoever is open: a question to the assistant, or a reply to a person.
private struct Composer: View {
    @ObservedObject var model: HubModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 6) {
            if let n = model.notice {
                Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(model.placeholder, text: $model.draft, axis: .vertical)
                    .lineLimit(1...6)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(Color.secondary.opacity(0.35)))
                    .focused($focused)
                    .onSubmit { model.send() }
                    .disabled(!model.modelReady)
                Button("Send") { model.send() }
                    .disabled(!model.canSend)
                    .keyboardShortcut(.return, modifiers: .command)
            }
        }
        .padding(12)
        .background(.bar)
        .onChange(of: model.picked) { _ in focused = true }
    }
}

#endif

#if !RECORDER
// MARK: - the assistant

private struct AssistantPane: View {
    @ObservedObject var model: HubModel
    /// In the panel over a person's page, whose header already says whose it is.
    var compact = false

    var body: some View {
        ScrollViewReader { reader in
            ScrollView {
                VStack(spacing: 12) {
                    if !compact {
                    VStack(spacing: 6) {
                        ZStack {
                            Circle().fill(LinearGradient(colors: [.purple, .blue], startPoint: .top, endPoint: .bottom))
                            Image(systemName: "sparkles").foregroundStyle(.white).font(.system(size: 36))
                        }
                        .frame(width: 88, height: 88)
                        Text("Personal Assistant").font(.title.bold())
                    }
                    .padding(.top, 24).padding(.bottom, 6)
                    }
                    Downloads(model: model)
                    // ONLY THE NEWEST DRAFT IS STILL A DRAFT: "send it" always takes the most recent,
                    // so an older one is just an answer now and loses the label.
                    let newestDraft = model.turns.lastIndex { $0.bool("draft") }
                    // EACH CARD UNDER THE TURN THAT MADE IT (Stanley, 2026-09-30: "why are the email
                    // cards always at the bottom?" — they were all drawn after every turn). A
                    // proposal is filed a moment before its turn is recorded, so it belongs to the
                    // first turn at or after it; one whose turn is still being answered has none
                    // yet and stays at the foot.
                    let cardTurn = model.proposals.map { p in
                        model.turns.firstIndex { $0.str("at") >= p.str("at") }
                    }
                    ForEach(model.turns.indices, id: \.self) { i in
                        TurnView(model: model, turn: model.turns[i], last: i == model.turns.count - 1,
                                 newestDraft: i == newestDraft)
                        ForEach(model.proposals.indices.filter { cardTurn[$0] == i }, id: \.self) { j in
                            ProposalCard(model: model, proposal: model.proposals[j])
                        }
                    }
                    if !model.pendingQuestion.isEmpty {
                        Balloon(text: model.pendingQuestion, mine: true, caption: "")
                        HStack { ProgressView().controlSize(.small); Spacer() }
                    }
                    ForEach(model.proposals.indices.filter { cardTurn[$0] == nil }, id: \.self) { j in
                        ProposalCard(model: model, proposal: model.proposals[j])
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .frame(maxWidth: 640)
                .padding(.horizontal, 24).padding(.top, compact ? 12 : 0).padding(.bottom, 16)
                .frame(maxWidth: .infinity)
            }
            .onAppear { reader.scrollTo("end", anchor: .bottom) }
            .onChange(of: model.turns.count) { _ in withAnimation { reader.scrollTo("end", anchor: .bottom) } }
            .onChange(of: model.pendingQuestion) { _ in reader.scrollTo("end", anchor: .bottom) }
        }
    }
}

/// THE ASSISTANT AT THE FOOT OF A PERSON'S PAGE (Stanley, 2026-09-30): its header and the box
/// when closed; open, it covers most of the page. One assistant and one box everywhere, so asking
/// about someone happens beside them rather than on another page.
private struct WithAssistant: ViewModifier {
    @ObservedObject var model: HubModel

    func body(content: Content) -> some View {
        // STACKED, NOT LAID OVER: the history ends where the panel begins, so nothing of it —
        // content or scroll bar — is ever behind the panel (Stanley, 2026-09-30).
        GeometryReader { geo in
            VStack(spacing: 0) {
                content.frame(maxWidth: .infinity, maxHeight: .infinity)
                VStack(spacing: 0) {
                    header
                    if model.drawerOpen {
                        Divider()
                        AssistantPane(model: model, compact: true)
                            .frame(maxHeight: .infinity)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                    Composer(model: model)
                }
                // Open, the WHOLE panel is two thirds of the page (Stanley, 2026-09-30).
                .frame(height: model.drawerOpen ? geo.size.height * 2 / 3 : nil)
                .background(Color(nsColor: .controlBackgroundColor))
                // Edge to edge; the box already rounds the bottom, so only the top is rounded here.
                .clipShape(TopRounded(radius: 12))
                .overlay(TopRounded(radius: 12).stroke(Color.secondary.opacity(0.25)))
                .shadow(color: .black.opacity(0.12), radius: 4, y: -1)
            }
        }
    }

    private var header: some View {
        Button {
            withAnimation(.easeOut(duration: 0.2)) { model.drawerOpen.toggle() }
        } label: {
            HStack(spacing: 8) {
                ZStack {
                    Circle().fill(LinearGradient(colors: [.purple, .blue], startPoint: .top, endPoint: .bottom))
                    Image(systemName: "sparkles").foregroundStyle(.white).font(.system(size: 11))
                }
                .frame(width: 22, height: 22)
                Text("Personal Assistant").font(.headline)
                Spacer()
                Image(systemName: model.drawerOpen ? "chevron.down" : "chevron.up")
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(.bar)
        .help(model.drawerOpen ? "Hide the assistant" : "Show the assistant")
        .background {
            // Esc closes it, as it would a sheet.
            if model.drawerOpen {
                Button("") { withAnimation(.easeOut(duration: 0.2)) { model.drawerOpen = false } }
                    .keyboardShortcut(.cancelAction).opacity(0)
            }
        }
    }
}

/// A rectangle with only its top corners rounded. (UnevenRoundedRectangle is macOS 14.)
private struct TopRounded: Shape {
    let radius: CGFloat
    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.minX, y: r.maxY))
        p.addLine(to: CGPoint(x: r.minX, y: r.minY + radius))
        p.addArc(center: CGPoint(x: r.minX + radius, y: r.minY + radius), radius: radius,
                 startAngle: .degrees(180), endAngle: .degrees(270), clockwise: false)
        p.addLine(to: CGPoint(x: r.maxX - radius, y: r.minY))
        p.addArc(center: CGPoint(x: r.maxX - radius, y: r.minY + radius), radius: radius,
                 startAngle: .degrees(270), endAngle: .degrees(0), clockwise: false)
        p.addLine(to: CGPoint(x: r.maxX, y: r.maxY))
        return p
    }
}

/// The two models a first install fetches, while either is still arriving.
private struct Downloads: View {
    @ObservedObject var model: HubModel
    var body: some View {
        let pick = model.panel.obj("model").obj("pick"), stt = model.panel.obj("stt")
        VStack(spacing: 8) {
            if let job = pick["job"] as? JSON, !pick.bool("downloaded") {
                bar(pick.str("name"), job.num("done_mb"), job.num("total_mb"))
            }
            if stt.bool("running"), !stt.bool("cached") {
                bar(stt.str("name"), stt.num("got_mb"), stt.num("mb"))
            }
            let dec = model.panel.obj("decider")
            if dec.bool("running"), !dec.bool("ready") {
                bar("Decision model", dec.num("got_mb"), dec.num("mb"))
            }
        }
    }
    private func bar(_ name: String, _ done: Double, _ total: Double) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(name); Spacer()
                Text("\(Int(done)) / \(Int(total)) MB").monospacedDigit()
            }
            .font(.caption).foregroundStyle(.secondary)
            ProgressView(value: total > 0 ? min(done / total, 1) : 0)
        }
    }
}

private struct TurnView: View {
    @ObservedObject var model: HubModel
    let turn: JSON
    let last: Bool
    let newestDraft: Bool

    var body: some View {
        if turn["break"] != nil {
            HStack { line; Text("New conversation").font(.caption).foregroundStyle(.secondary); line }
        } else {
            VStack(spacing: 8) {
                Balloon(text: turn.str("q"), mine: true, caption: "")
                if !turn.str("a").isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        if turn.bool("draft") && (newestDraft || turn.bool("sent") || turn.bool("held")) {
                            // ORANGE, NOT RED: not sent is waiting on the owner, not a failure.
                            Text(draftLabel).font(.caption.weight(.semibold))
                                .foregroundStyle(turn.bool("sent") ? Color.green
                                                 : turn.bool("held") ? Color.secondary : Color.orange)
                        }
                        Text(turn.str("a")).textSelection(.enabled)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(Color(nsColor: .quaternaryLabelColor)))
                    .frame(maxWidth: 460, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    // A ROW LIKE THE CALENDAR AND EMAIL CARDS (Stanley, 2026-09-30): what it is and
                    // the action at the right, under the balloon rather than a button inside it.
                    // Only under the last answer, and only for a draft not gone.
                    if last, turn.bool("draft"), !turn.bool("sent"), !turn.bool("held"),
                       let who = model.replyTarget {
                        HStack(spacing: 8) {
                            Image(systemName: "paperplane").foregroundStyle(Color.accentColor)
                            Text("Reply to \(HubModel.name(who))").lineLimit(1)
                            Spacer()
                            Button("Send") { model.sendDraft() }.disabled(model.busy)
                        }
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.secondary.opacity(0.3)))
                        .frame(maxWidth: 460, alignment: .leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }

    private var line: some View { Rectangle().fill(Color.secondary.opacity(0.3)).frame(height: 1) }

    private var draftLabel: String {
        let who = turn.str("draft_for_name")
        if turn.bool("sent") { return who.isEmpty ? "Sent" : "Sent to \(who)" }
        if turn.bool("held") { return who.isEmpty ? "Queued" : "Queued for \(who)" }
        return (who.isEmpty ? "Draft reply" : "Draft reply to \(who)") + " — not sent"
    }
}

/// A proposal reads differently per tool, and the difference is the point of the card: a skill
/// changes how the assistant WORKS, so its card shows the words and what the owner asked.
private struct ProposalCard: View {
    @ObservedObject var model: HubModel
    let proposal: JSON

    static let kinds: [String: (String, String, String)] = [
        "add_skill": ("Follow this from now on", "a new skill", "Do It"),
        "edit_skill": ("Change how you work", "edits a skill", "Do It"),
        "forget_skill": ("Stop following this", "removes a skill", "Do It"),
        "switch_skill": ("Switch this off", "keeps it but stops following it", "Do It"),
        "add_to_calendar": ("Open this calendar event", "", "Open It"),
        "draft_email": ("Open this email draft", "", "Open It"),
        "add_contact": ("Add to Contacts", "", "Open It"),
        "correct_brief": ("Correct what I know", "", "Correct It"),
    ]

    var body: some View {
        let tool = proposal.str("tool"), a = proposal.obj("args")
        let (title, kind, verb0) = Self.kinds[tool] ?? ("Add to your shared notes", a.str("file").isEmpty ? "knowledge" : a.str("file"), "Add It")
        // A CARD THAT ONLY OPENS SOMETHING STAYS after it is used (issue #9), and says so.
        let reopen = ["add_to_calendar", "draft_email", "add_contact"].contains(tool)
        VStack(alignment: .leading, spacing: 6) {
            Text(title).bold()
            if tool == "draft_email" { EmailFields(proposal: proposal); Divider() }
            else if tool == "add_contact" { ContactFields(args: a); Divider() }
            else { Text(body(tool, a)).textSelection(.enabled) }
            if !kind.isEmpty { Text(kind).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button(proposal.bool("opened") ? "Open Again" : verb0) {
                    // A NEW CONTACT OPENS IN CONTACTS' EDITOR where the app has Contacts access;
                    // the card stays, as the other cards that only open something do.
                    if tool == "add_contact", ContactsAdd.addAndEdit(number: a.str("number"), name: a.str("name")) { return }
                    model.decide(proposal, approve: true)
                }
                Button(reopen ? "Dismiss" : "Discard") { model.decide(proposal, approve: false) }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.blue.opacity(0.45)))
    }

    private func body(_ tool: String, _ a: JSON) -> String {
        switch tool {
        case "add_skill": return "\(a.str("name")) — \(a.str("how"))"
        case "edit_skill": return "\(a.str("name")): \(a.str("old")) → \(a.str("new").isEmpty ? "(deleted)" : a.str("new"))"
        case "forget_skill", "switch_skill": return a.str("name")
        case "add_to_calendar": return "\(a.str("title")) — \(a.str("start"))\(a.str("end").isEmpty ? "" : " to " + a.str("end"))"
        case "correct_brief": return "\(a.str("who")) — \(a.str("correction"))"
        case "draft_email": return "\(a.str("to")) — \(a.str("subject").isEmpty ? "(no subject)" : a.str("subject"))"
        default: return [a.str("fact"), a.str("new"), a.str("old")].first { !$0.isEmpty } ?? ""
        }
    }
}

/// A new contact as Contacts will open it: Name and Number.
private struct ContactFields: View {
    let args: JSON

    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 4) {
            GridRow {
                Text("Name").foregroundStyle(.secondary).gridColumnAlignment(.leading)
                Text(args.str("name").isEmpty ? "—" : args.str("name"))
            }
            GridRow {
                Text("Number").foregroundStyle(.secondary)
                Text(args.str("number"))
            }
        }
        .textSelection(.enabled)
    }
}

/// A draft email as the mail client will open it: To, Subject, then the message.
private struct EmailFields: View {
    let proposal: JSON

    var body: some View {
        let a = proposal.obj("args")
        // The address Contacts gave; else what the assistant was told, for the owner to fill in.
        let to = proposal.str("address").isEmpty ? a.str("to") : proposal.str("address")
        VStack(alignment: .leading, spacing: 8) {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 4) {
                GridRow {
                    Text("To").foregroundStyle(.secondary).gridColumnAlignment(.leading)
                    Text(to.isEmpty ? "—" : to)
                }
                GridRow {
                    Text("Subject").foregroundStyle(.secondary)
                    Text(a.str("subject").isEmpty ? "—" : a.str("subject"))
                }
            }
            if !a.str("body").isEmpty {
                Divider()
                Text(a.str("body")).fixedSize(horizontal: false, vertical: true)
            }
        }
        .textSelection(.enabled)
    }
}

#endif

// MARK: - a person's conversation

private struct Conversation: View {
    @ObservedObject var model: HubModel
    @ObservedObject var phone: PhoneModel
    let person: JSON
    @StateObject private var editing = Local(false)
    /// The big header has scrolled away, so the small one stands in at the top.
    @StateObject private var compact = Local(false)
    #if !RECORDER
    /// The summary, from the slim header, and the sheet that corrects it.
    @StateObject private var showingSummary = Local(false)
    @StateObject private var correcting = Local(false)
    #endif

    var body: some View {
        let items = model.items(person)
        let calls = person["calls"] as? [JSON] ?? []
        let liveNow = phone.liveCalls(for: person.str("who"), history: calls)
        ScrollViewReader { reader in
            ScrollView {
                VStack(spacing: 14) {
                    VStack(spacing: 6) {
                        Avatar(person: person, size: 88)
                        HStack(spacing: 6) {
                            Text(HubModel.name(person)).font(.title.bold()).textSelection(.enabled)
                            buttons(calls)
                        }
                        Text(HubModel.subtitle(person)).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    // WHERE THE BIG HEADER'S BOTTOM IS, so the small one shows once it has gone.
                    .background(GeometryReader { g in
                        let bottom = g.frame(in: .named("conversation")).maxY
                        Color.clear
                            .onAppear { setCompact(bottom < 0) }
                            .onChange(of: bottom) { setCompact($0 < 0) }
                    })
                    .padding(.top, 24).padding(.bottom, 6)
                    #if !RECORDER
                    if !person.str("summary").isEmpty {
                        SummaryBody(person: person) { correcting.value = true }
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(Color(nsColor: .windowBackgroundColor).opacity(0.6)))
                    }
                    #endif
                    ForEach(items) { item in
                        Group {
                            if let c = item.call {
                                #if RECORDER
                                CallCard(model: model, call: c, player: model.player)
                                #else
                                CallCard(model: model, call: c,
                                         captions: phone.captions[c.str("call_id")] ?? [],
                                         player: model.player)
                                #endif
                            } else if let m = item.message { MessageRows(model: model, message: m) }
                        }
                        .id(item.id)
                    }
                    // A CALL NOT YET IN THE HISTORY shows here, with its captions as they come.
                    ForEach(liveNow, id: \.0) { id, c in
                        VStack(alignment: .leading, spacing: 10) {
                            Label(c.ended ? "Call ended" : "On a call", systemImage: "phone.fill")
                                .font(.subheadline.weight(.semibold)).foregroundStyle(c.ended ? Color.secondary : Color.green)
                            #if !RECORDER
                            ForEach(phone.captions[id] ?? []) { cap in Balloon(text: cap.text, mine: cap.mine, caption: "") }
                            #endif
                        }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .strokeBorder(Color.green.opacity(c.ended ? 0.2 : 0.5)))
                    }
                    if items.isEmpty && liveNow.isEmpty { Text("Nothing yet.").foregroundStyle(.secondary) }
                    Color.clear.frame(height: 1).id("end")
                }
                .frame(maxWidth: 640)
                .padding(.horizontal, 24).padding(.bottom, 16)
                .frame(maxWidth: .infinity)
            }
            .coordinateSpace(name: "conversation")
            .overlay(alignment: .top) {
                if compact.value {
                    VStack(spacing: 0) {
                        HStack(spacing: 10) {
                            Avatar(person: person, size: 28)
                            VStack(alignment: .leading, spacing: 0) {
                                Text(HubModel.name(person)).font(.headline)
                                Text(HubModel.subtitle(person)).font(.caption).foregroundStyle(.secondary)
                            }
                            buttons(calls)
                            Spacer()
                            #if !RECORDER
                            if !person.str("summary").isEmpty {
                                Button { showingSummary.value = true } label: {
                                    Label("Summary", systemImage: "text.alignleft")
                                }
                                .help("Summary")
                                .popover(isPresented: $showingSummary.value, arrowEdge: .bottom) {
                                    SummaryBody(person: person) {
                                        showingSummary.value = false
                                        correcting.value = true
                                    }
                                    .padding(16)
                                    .frame(width: 400)
                                }
                            }
                            #endif
                        }
                        .padding(.horizontal, 16).padding(.vertical, 8)
                        Divider()
                    }
                    .background(.bar)
                    .transition(.opacity)
                }
            }
            .animation(.easeOut(duration: 0.15), value: compact.value)
            // THE LATEST AT THE BOTTOM, in view on opening and when something new arrives.
            .onAppear { reader.scrollTo("end", anchor: .bottom) }
            #if !RECORDER
            // AND WHEN THE ASSISTANT OPENS OVER IT: what is left in view is the latest, once the
            // panel has finished taking its space.
            .onChange(of: model.drawerOpen) { open in
                guard open else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    withAnimation { reader.scrollTo("end", anchor: .bottom) }
                }
            }
            #endif
            .onChange(of: items.count) { _ in withAnimation { reader.scrollTo("end", anchor: .bottom) } }
            .onChange(of: person.str("who")) { _ in reader.scrollTo("end", anchor: .bottom) }
            #if !RECORDER
            .onChange(of: liveNow.map { (phone.captions[$0.0] ?? []).count }.reduce(0, +)) { _ in
                reader.scrollTo("end", anchor: .bottom)
            }
            #endif
        }
        .sheet(isPresented: $editing.value) { RenameSheet(model: model) }
        #if !RECORDER
        .sheet(isPresented: $correcting.value) { CorrectSheet(model: model, who: person.str("who")) }
        #endif
    }

    private func setCompact(_ on: Bool) {
        if compact.value != on { compact.value = on }
    }

    /// Rename, and Open in Contacts — beside the name, big or small.
    @ViewBuilder private func buttons(_ calls: [JSON]) -> some View {
        if !person.str("display").isEmpty || !calls.isEmpty || !(person["messages"] as? [JSON] ?? []).isEmpty {
            Button { editing.value = true } label: { Image(systemName: "pencil") }
                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Rename")
        }
        // THE CARD THE NAME CAME FROM, opened in Contacts — the only app that edits it.
        // `addressbook://` takes a contact's identifier.
        let card = person.str("contact_id")
        if !card.isEmpty, let url = URL(string: "addressbook://" + card) {
            Button { NSWorkspace.shared.open(url) } label: { Image(systemName: "person.crop.square") }
                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Open in Contacts")
        }
    }
}



#if !RECORDER
// MARK: - the summary

/// WHAT THE APP KNOWS ABOUT THEM (2026-10-01): the running brief, as the notes sit on a Contacts
/// card. Correct… folds the owner's words in through the model; nobody edits the text itself.
/// One body, shown twice: as a card at the top of the page, and in a popover from the slim header
/// — the page opens at the newest call, so in a long history the card is where the owner is not.
private struct SummaryBody: View {
    let person: JSON
    let correct: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Summary").font(.headline)
                Spacer()
                Button("Correct…") { correct() }
            }
            ForEach(Array(person.str("summary").split(separator: "\n", omittingEmptySubsequences: true)
                            .enumerated()), id: \.offset) { _, line in
                Self.line(String(line)).fixedSize(horizontal: false, vertical: true)
            }
            if !person.str("summary_at").isEmpty {
                Text("Updated \(HubModel.when(person.str("summary_at")))")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .textSelection(.enabled)
    }

    /// "About:", "Open:" and "Last contact:" as labels, the rest as the text. "Who:" too: a
    /// summary written before About replaced it keeps that label until its next update.
    @ViewBuilder static func line(_ s: String) -> some View {
        if let colon = s.firstIndex(of: ":"),
           ["About", "Who", "Open", "Last contact"].contains(String(s[..<colon]).trimmingCharacters(in: .whitespaces)) {
            (Text(String(s[...colon])).bold() + Text(String(s[s.index(after: colon)...])))
        } else {
            Text(s)
        }
    }
}

private struct CorrectSheet: View {
    @ObservedObject var model: HubModel
    let who: String
    @Environment(\.dismiss) private var dismiss
    @StateObject private var text = Local("")
    @StateObject private var busy = Local(false)
    @StateObject private var failed = Local("")
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField("What's wrong?", text: $text.value, prompt: Text(""), axis: .vertical)
                        .lineLimit(3...6)
                        .focused($focused)
                } footer: {
                    if !failed.value.isEmpty { Text(failed.value).foregroundStyle(.red) }
                }
            }
            .formStyle(.grouped)
            HStack {
                if busy.value { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Correct") {
                    busy.value = true
                    failed.value = ""
                    Task {
                        let error = await model.correctSummary(who, text.value)
                        busy.value = false
                        if let error { failed.value = error } else { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy.value || text.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 460)
        .onAppear { focused = true }
    }
}

#endif

// MARK: - a call

private struct CallCard: View {
    @ObservedObject var model: HubModel
    let call: JSON
    #if !RECORDER
    /// This call's live captions, which stand in until its kept transcript lands.
    var captions: [PhoneModel.Caption] = []
    #endif

    @ObservedObject var player: CallPlayer

    var body: some View {
        let mine = player.callID == call.str("call_id")
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if !call.str("audio").isEmpty {
                    Button { player.toggle(callID: call.str("call_id"), path: call.str("audio")) } label: {
                        Image(systemName: mine && player.playing ? "pause.circle.fill" : "play.circle.fill")
                            .font(.system(size: 20))
                    }
                    .buttonStyle(.borderless)
                    .help(player.blocked ? "Not during a call" : mine && player.playing ? "Pause" : "Play")
                }
                Text(HubModel.when(call.str("started").isEmpty ? call.str("at") : call.str("started")))
                    .font(.subheadline.weight(.semibold))
                Spacer()
                let at = player.place(of: call.str("call_id"))
                Text(mine || at > 0 ? "\(HubModel.clock(at)) / \(HubModel.clock(call.num("seconds")))"
                                    : HubModel.callMeta(call))
                    .font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }
            if let problem = player.problem, problem.callID == call.str("call_id") {
                Text(problem.text).font(.caption).foregroundStyle(.orange)
            }
            #if RECORDER
            // A RECORDING, NOT A TRANSCRIPT: the recorder has no speech engine, so a call says
            // only what happened to it — missed, nothing recorded — and otherwise plays.
            if let state = HubModel.callState(call) {
                Text(state).foregroundStyle(.secondary).italic()
            }
            #else
            if call.str("transcript").isEmpty, !captions.isEmpty {
                ForEach(captions) { c in Balloon(text: c.text, mine: c.mine, caption: "") }
            } else if let state = HubModel.callState(call) {
                Text(state).foregroundStyle(.secondary).italic()
            } else {
                let turns = HubModel.turns(call.str("transcript"))
                if turns.isEmpty {
                    Text(call.str("transcript")).textSelection(.enabled)
                } else {
                    ForEach(turns) { t in Balloon(text: t.text, mine: t.mine, caption: "") }
                }
            }
            if let s = call["suggest"] as? JSON { SuggestionRow(model: model, suggestion: s) }
            #endif
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill(Color(nsColor: .windowBackgroundColor).opacity(0.6)))
    }
}

// MARK: - a message

private struct MessageRows: View {
    @ObservedObject var model: HubModel
    let message: JSON

    var body: some View {
        VStack(spacing: 8) {
            if !message.str("them").isEmpty {
                Balloon(text: message.str("them"), mine: false,
                        caption: "\(message.str("network")) · \(HubModel.when(message.str("at")))")
            }
            if !message.str("us").isEmpty {
                // NOT DELIVERED is a different thing from sent, and the caption is where it shows.
                let caption = message.str("by") == "agent" ? "Answered by the agent"
                    : (message.bool("held") ? "Not delivered · " : "You · ") + HubModel.when(message.str("at"))
                Balloon(text: message.str("us"), mine: true, caption: caption, held: message.bool("held"))
            }
            #if !RECORDER
            if let s = message["suggest"] as? JSON { SuggestionRow(model: model, suggestion: s) }
            #endif
        }
    }
}

/// The other party on the left, the owner on the right — the layout of Messages.
private struct Balloon: View {
    let text: String
    let mine: Bool
    let caption: String
    var held = false

    var body: some View {
        VStack(alignment: mine ? .trailing : .leading, spacing: 3) {
            Text(text)
                // WRAP, NEVER CUT: in a scrolling list SwiftUI sometimes gave a balloon one line
                // and an ellipsis ("…meet u…", 2026-10-01). It may always grow taller.
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .foregroundStyle(held ? Color.secondary : Color.primary)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                    // ONE GREY FOR BOTH SIDES (Stanley, 2026-09-29): blue for yours drew the eye to
                    // the half you already know. Left and right say whose it is.
                    .fill(held ? Color.gray.opacity(0.15) : Color(nsColor: .quaternaryLabelColor)))
            if !caption.isEmpty {
                Text(caption).font(.caption2).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: 460, alignment: mine ? .trailing : .leading)
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
    }
}

#if !RECORDER
/// What was arranged, under the words that arranged it: the event and its time, and nothing else.
private struct SuggestionRow: View {
    @ObservedObject var model: HubModel
    let suggestion: JSON

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "calendar").foregroundStyle(Color.accentColor)
            Text("\(suggestion.str("title")) · \(suggestion.str("when"))").lineLimit(2)
            Spacer()
            // OPENING KEEPS IT (issue #9): Google Calendar saves nothing until the owner presses Save.
            Button(suggestion.bool("opened") ? "Open Again" : "Add to Calendar") {
                model.suggestion(suggestion.str("key"), "add")
            }
            Button("Dismiss") { model.suggestion(suggestion.str("key"), "dismiss") }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(Color.secondary.opacity(0.3)))
    }
}

#endif

// MARK: - rename

/// The one thing about a person here the owner can change: the name.
/// Empty removes the typed name, so the Contacts name — or the number — shows again.
/// Rename — and, for someone Contacts has no card for, add them there under that name
/// (Stanley, 2026-09-30): one sheet, the cursor already in the name. Someone already in Contacts
/// is renamed in the hub only; their real card is edited in Contacts, through Open in Contacts.
private struct RenameSheet: View {
    @ObservedObject var model: HubModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var name = Local("")
    @StateObject private var busy = Local(false)
    @StateObject private var toContacts = Local(true)
    /// Off to start with: this changes the owner's real card, which may sync elsewhere.
    @StateObject private var intoContacts = Local(false)
    @FocusState private var focused: Bool

    /// The card this person's name came from, where the app can write to it.
    private var card: String {
        guard let p = model.person, p.str("name_from") == "contacts" || !p.str("contact_id").isEmpty,
              ContactsWatch.access() == "allowed" else { return "" }
        return p.str("contact_id")
    }

    /// A number Contacts has no card for.
    private var addable: Bool {
        guard let p = model.person else { return false }
        return p.str("contact_id").isEmpty && p.str("name_from") != "contacts"
            && HubModel.isNumber(p.str("who"))
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField("Name", text: $name.value, prompt: Text(""))
                        .focused($focused)
                    if let who = model.person?.str("who") {
                        LabeledContent("Number", value: who)
                    }
                    if addable {
                        Toggle("Add to Contacts", isOn: $toContacts.value)
                    } else if !card.isEmpty {
                        Toggle("Also change in Contacts", isOn: $intoContacts.value)
                    }
                }
            }
            .formStyle(.grouped)
            HStack {
                if busy.value { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    busy.value = true
                    let typed = name.value.trimmingCharacters(in: .whitespaces)
                    let who = model.person?.str("who") ?? ""
                    let add = addable && toContacts.value && !typed.isEmpty
                    let change = !card.isEmpty && intoContacts.value && !typed.isEmpty ? card : ""
                    Task {
                        await model.rename(typed)
                        if add { model.addContact(who, name: typed, edit: false) }
                        if !change.isEmpty, !ContactsAdd.rename(id: change, to: typed) {
                            model.notice = .init(ok: false, text: "Contacts did not take the new name.")
                        }
                        busy.value = false
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy.value)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 420)
        .onAppear {
            name.value = model.person?.str("display") ?? ""
            focused = true
        }
    }
}
