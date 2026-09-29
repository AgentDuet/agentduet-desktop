import SwiftUI

/// The native hub (2026-09-29), on the Contacts layout: a plain list of people on the left, and
/// the chosen person in a rounded box on the right — their avatar, name and number, then the calls
/// and messages between you, with Edit in the toolbar.
struct HubView: View {
    @ObservedObject var model: HubModel

    var body: some View {
        HStack(spacing: 0) {
            PeopleList(model: model)
                .frame(width: 260)
            PersonBox(model: model)
                .padding(10)
        }
        .frame(minWidth: 820, minHeight: 520)
        // CONTACTS' LOOK, AGENTDUET'S CONTROLS (Stanley, 2026-09-29): the toolbar holds what the
        // hub's title bar holds — whether you can be heard, answering here, and Settings.
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if model.carry {
                    Image(systemName: model.micOK ? "mic.fill" : "mic.slash.fill")
                        .foregroundStyle(model.micOK ? Color.green : Color.red)
                        .help(model.micReason)
                    // THE LABEL AS TEXT: a toolbar hides a toggle's own label.
                    HStack(spacing: 6) {
                        Text("Answer Calls Here")
                        Toggle("Answer Calls Here", isOn: Binding(
                            get: { model.answerHere }, set: { model.setAnswerHere($0) }))
                            .toggleStyle(.switch).labelsHidden().controlSize(.small)
                    }
                }
                Button { model.openSettings?() } label: { Label("Settings", systemImage: "gearshape") }
                    .help("Settings")
            }
        }
    }
}

// MARK: - the list

private struct PeopleList: View {
    @ObservedObject var model: HubModel

    var body: some View {
        // ROWS WITH THEIR OWN HIGHLIGHT, as Contacts draws them: a rounded grey band on the one
        // chosen, the same whether or not the window is in front.
        ScrollView {
            LazyVStack(spacing: 2) {
                ForEach(model.shown.indices, id: \.self) { i in
                    let p = model.shown[i]
                    let on = p.str("who") == model.picked
                    PersonRow(person: p, picked: on)
                        .padding(.horizontal, 8)
                        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(on ? Color.primary.opacity(0.12) : Color.clear))
                        .contentShape(Rectangle())
                        .onTapGesture { model.pick(p.str("who")) }
                }
            }
            .padding(8)
        }
        .overlay {
            if model.shown.isEmpty {
                Text(model.search.isEmpty ? "Nobody yet." : "No one matches.").foregroundStyle(.secondary)
            }
        }
    }
}

private struct PersonRow: View {
    let person: JSON
    let picked: Bool

    var body: some View {
        HStack(spacing: 10) {
            Avatar(person: person, size: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text(HubModel.name(person)).font(.body.weight(.semibold)).lineLimit(1)
                Text("\(HubModel.counts(person)) · \(HubModel.when(person.str("last")))")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            // NEW SINCE LAST LOOKED, until the owner opens that person.
            let unread = Int(person.num("unread"))
            if unread > 0 && !picked {
                Text(unread > 99 ? "99+" : "\(unread)")
                    .font(.caption2.bold()).foregroundStyle(.white)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(Color.accentColor))
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

// MARK: - the box

private struct PersonBox: View {
    @ObservedObject var model: HubModel

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
                .shadow(color: .black.opacity(0.12), radius: 3, y: 1)
            if let p = model.person {
                Conversation(model: model, person: p)
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            } else {
                Text("Nobody has called or written yet.").foregroundStyle(.secondary)
            }
        }
    }
}

private struct Conversation: View {
    @ObservedObject var model: HubModel
    let person: JSON
    @StateObject private var editing = Local(false)

    var body: some View {
        let items = model.items(person)
        ScrollViewReader { reader in
            ScrollView {
                VStack(spacing: 14) {
                    VStack(spacing: 6) {
                        Avatar(person: person, size: 88)
                        HStack(spacing: 6) {
                            Text(HubModel.name(person)).font(.title.bold()).textSelection(.enabled)
                            Button { editing.value = true } label: { Image(systemName: "pencil") }
                                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Rename")
                        }
                        Text(HubModel.subtitle(person)).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    .padding(.top, 24).padding(.bottom, 6)
                    if let n = model.notice {
                        Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                    }
                    ForEach(items) { item in
                        Group {
                            if let c = item.call { CallCard(model: model, call: c) }
                            else if let m = item.message { MessageRows(model: model, message: m) }
                        }
                        .id(item.id)
                    }
                    if items.isEmpty { Text("Nothing yet.").foregroundStyle(.secondary) }
                    Color.clear.frame(height: 1).id("end")
                }
                .frame(maxWidth: 640)
                .padding(.horizontal, 24).padding(.bottom, 24)
                .frame(maxWidth: .infinity)
            }
            // THE LATEST AT THE BOTTOM, in view on opening and when something new arrives.
            .onAppear { reader.scrollTo("end", anchor: .bottom) }
            .onChange(of: items.count) { _ in withAnimation { reader.scrollTo("end", anchor: .bottom) } }
            .onChange(of: person.str("who")) { _ in reader.scrollTo("end", anchor: .bottom) }
        }
        .sheet(isPresented: $editing.value) { RenameSheet(model: model) }
    }
}

// MARK: - a call

private struct CallCard: View {
    @ObservedObject var model: HubModel
    let call: JSON

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(HubModel.when(call.str("started").isEmpty ? call.str("at") : call.str("started")))
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Text(HubModel.callMeta(call)).font(.caption).foregroundStyle(.secondary)
            }
            if let state = HubModel.callState(call) {
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
            if let s = message["suggest"] as? JSON { SuggestionRow(model: model, suggestion: s) }
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
                .textSelection(.enabled)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .foregroundStyle(mine && !held ? Color.white : Color.primary)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                    // BLUE, as Messages draws your side, whatever the system accent is.
                    .fill(mine ? (held ? Color.gray.opacity(0.25) : Color.blue)
                               : Color(nsColor: .quaternaryLabelColor)))
            if !caption.isEmpty {
                Text(caption).font(.caption2).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: 460, alignment: mine ? .trailing : .leading)
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
    }
}

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

// MARK: - rename

/// The one thing about a person here the owner can change: the name.
/// Empty removes the typed name, so the Contacts name — or the number — shows again.
private struct RenameSheet: View {
    @ObservedObject var model: HubModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var name = Local("")
    @StateObject private var busy = Local(false)

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField("Name", text: $name.value, prompt: Text(model.person?.str("who") ?? ""))
                    if let who = model.person?.str("who") {
                        LabeledContent("Number", value: who)
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
                    Task {
                        await model.rename(name.value.trimmingCharacters(in: .whitespaces))
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
        .onAppear { name.value = model.person?.str("display") ?? "" }
    }
}
