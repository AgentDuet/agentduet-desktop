import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// The native Settings window (2026-09-29): macOS's own sidebar and grouped forms, as System
/// Settings draws them — label on the left, control on the right, no Save buttons, and an
/// Edit… sheet wherever a change needs a moment of commitment.
struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    // SYSTEM SETTINGS' LAYOUT (Stanley, 2026-09-30): the sidebar a rounded box of its own, inset and
    // running top to bottom; the pane's title at the top with even space above and below, and Done
    // beside it; ONE background behind the title and the form — no band for a title bar.
    var body: some View {
        HStack(spacing: 0) {
            List(SettingsModel.Section.allCases, selection: Binding(
                get: { model.section }, set: { if let s = $0 { model.section = s } })) { s in
                Label(s.title, systemImage: s.symbol).tag(s)
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .frame(width: 210)
            // A LIGHTER LAYER OVER THE TINTED WINDOW, as System Settings' sidebar reads. The sidebar
            // material itself drew a flat grey inside a sheet.
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.primary.opacity(0.06)))
            .padding(8)

            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text(model.section.title).font(.title2.bold())
                    Spacer()
                    // DONE closes the sheet; Esc does the same.
                    Button("Done") { model.done?() }
                        .keyboardShortcut(.defaultAction)
                        .keyboardShortcut(.cancelAction)
                }
                .padding(.horizontal, 20).padding(.top, 16)
                Group {
                    switch model.section {
                    case .account: AccountPane(model: model)
                    case .calls: CallsPane(model: model)
                    case .permissions: PermissionsPane(model: model)
                    case .advanced: AdvancedPane(model: model)
                    case .about: AboutPane(model: model)
                    }
                }
                .formStyle(.grouped)
                .scrollContentBackground(.hidden)
                .modifier(NoTopMargin())
                // EVEN SPACE ABOVE AND BELOW THE TITLE: a grouped form still keeps room above its
                // first section, so it is drawn up to meet the title.
                .padding(.top, -6)
            }
            .frame(minWidth: 460)
        }
        // SYSTEM SETTINGS' OWN COLOURS: the window material, tinted by the wallpaper as the system's
        // windows are — a flat grey never matched it.
        .background(WindowMaterial(material: .windowBackground))
    }
}

/// A macOS material behind a view — the window's or the sidebar's — tinted by the wallpaper as the
/// system's own windows are.
struct WindowMaterial: NSViewRepresentable {
    let material: NSVisualEffectView.Material

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = .behindWindow
        view.state = .followsWindowActiveState
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) { view.material = material }
}

/// The grouped form's own top margin, taken off so the title has the same space below it as above.
private struct NoTopMargin: ViewModifier {
    func body(content: Content) -> some View {
        if #available(macOS 14.0, *) {
            content.contentMargins(.top, 0, for: .scrollContent)
        } else {
            content
        }
    }
}

/// A view's own local state. NOT `@State`: in the current SDK that is a macro whose plugin ships
/// with Xcode, and this package is built with the Command Line Tools alone (see Package.swift),
/// where `@State` does not compile. `@StateObject` is a plain property wrapper and does.
final class Local<Value>: ObservableObject {
    @Published var value: Value
    init(_ value: Value) { self.value = value }
}

/// What the last action said, under the section it belongs to. Outcomes only.
private struct NoticeFooter: View {
    let notice: SettingsModel.Notice?
    var body: some View {
        if let n = notice {
            Text(n.text).font(.callout).foregroundStyle(n.ok ? Color.secondary : Color.red)
                .textSelection(.enabled)
        }
    }
}

// MARK: - Account

private struct AccountPane: View {
    @ObservedObject var model: SettingsModel
    @StateObject private var editingKey = Local(false)
    @StateObject private var editingName = Local(false)
    @StateObject private var confirmSignOut = Local(false)

    var body: some View {
        Form {
            Section {
                // THE VALUE AND AN EDIT BUTTON (Stanley, 2026-09-30): an inline field looked like
                // plain text, with nothing to say it could be changed.
                LabeledContent("Name") {
                    HStack {
                        Text(model.name).textSelection(.enabled)
                        Button("Edit…") { editingName.value = true }
                    }
                }
                // LEARNED, NOT TYPED (Stanley, 2026-09-29): the number is mined from the first
                // call, so it is shown here and not edited — and until then there is no row.
                if !model.yourNumber.isEmpty {
                    LabeledContent("Your number") {
                        Text(model.yourNumber).textSelection(.enabled)
                    }
                }
            }
            Section {
                LabeledContent("Status") {
                    HStack(spacing: 6) {
                        Circle().fill(model.connected ? Color.green : Color.secondary)
                            .frame(width: 8, height: 8)
                        Text(model.connectionState)
                    }
                }
                if model.signedIn {
                    LabeledContent("Signed in as") {
                        HStack {
                            Text(model.oauth.str("email"))
                            Button("Sign Out…") { confirmSignOut.value = true }
                        }
                    }
                } else if model.byKey {
                    LabeledContent("API key") {
                        HStack {
                            Text(model.cur.str("key_hint")).monospaced()
                            Button("Change…") { editingKey.value = true }
                        }
                    }
                } else {
                    // ONE BUTTON, and the ways in behind it — the setup window's own panel.
                    LabeledContent("Account") {
                        Button("Sign In…") { editingKey.value = true }
                    }
                }
            } header: {
                Text("AgentDuet Connection")
            } footer: {
                NoticeFooter(notice: model.notice[.account])
            }
        }
        // LEAVING A FIELD SAVES IT, as Return does — there is no Save button to forget.
        .sheet(isPresented: $editingKey.value) { SignInSheet(model: model) }
        .sheet(isPresented: $editingName.value) { NameSheet(model: model) }
        .confirmationDialog("Sign out of AgentDuet?", isPresented: $confirmSignOut.value) {
            Button("Sign Out", role: .destructive) { model.signOut() }
        } message: {
            Text("Calls stop arriving until you sign in again or enter a key.")
        }
    }

}

/// The name, edited in a sheet with Cancel and Save.
private struct NameSheet: View {
    @ObservedObject var model: SettingsModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var name = Local("")

    var body: some View {
        VStack(spacing: 0) {
            Form {
                TextField("Name", text: $name.value, prompt: Text("Your name"))
            }
            .formStyle(.grouped)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") {
                    model.name = name.value.trimmingCharacters(in: .whitespaces)
                    model.commit("name")
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.value.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 420)
        .onAppear { name.value = model.name }
    }
}

/// Sign In…, or Change… for an install set up by key: the setup window's first step, as a sheet.
private struct SignInSheet: View {
    @ObservedObject var model: SettingsModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var signIn: SignInModel

    init(model: SettingsModel) {
        self.model = model
        _signIn = StateObject(wrappedValue: SignInModel(settings: model))
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(spacing: 18) {
                    VStack(spacing: 8) {
                        Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56)
                        Text("Sign in to AgentDuet").font(.title2.bold())
                    }
                    .padding(.top, 24)
                    SignInPanel(model: signIn)
                }
                .padding(.bottom, 20)
            }
            Divider()
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 20).padding(.vertical, 14)
        }
        .frame(width: 560, height: 520)
        .onAppear {
            signIn.prefill()
            signIn.onDone = { dismiss() }
            model.notice[.account] = nil
        }
    }
}

// MARK: - Record & Transcribe

private struct CallsPane: View {
    @ObservedObject var model: SettingsModel

    #if !RECORDER
    static let languages = [("en", "English"), ("vi", "Vietnamese"), ("zh", "Chinese"),
                            ("ms", "Malay"), ("th", "Thai")]
    #endif

    var body: some View {
        Form {
            Section {
                Toggle("Save calls to this Mac", isOn: Binding(
                    get: { model.recordCalls }, set: { model.setRecordCalls($0) }))
                // THE FOLDER'S NAME, NOT ITS PATH, as Safari's download folder does: a path is as
                // long as the owner made it and a row is not. The full path is on hover.
                LabeledContent("Folder") {
                    Menu {
                        Button("Show in Finder") { model.showFolder() }
                        Divider()
                        Button("Change…") { model.changeFolder() }
                    } label: {
                        Label {
                            Text(folderName)
                        } icon: {
                            Image(nsImage: folderIcon)
                        }
                    }
                    .fixedSize()
                    .help(abbreviated(model.storage))
                }
            }
            #if RECORDER
            Section {} footer: { NoticeFooter(notice: model.notice[.calls]) }
            #else
            // THE LANGUAGE IS THE SPEECH ENGINE'S HINT, so the recorder has no such row.
            Section {
                Picker("Language of your calls", selection: Binding(
                    get: { model.language }, set: { model.setLanguage($0) })) {
                    ForEach(Self.languages, id: \.0) { Text($0.1).tag($0.0) }
                }
            } footer: {
                NoticeFooter(notice: model.notice[.calls])
            }
            #endif
        }
    }

    private var folderName: String {
        model.storage.isEmpty ? "None" : FileManager.default.displayName(atPath: model.storage)
    }

    private var folderIcon: NSImage {
        let image = model.storage.isEmpty
            ? NSWorkspace.shared.icon(for: .folder)
            : NSWorkspace.shared.icon(forFile: model.storage)
        image.size = NSSize(width: 16, height: 16)
        return image
    }

    private func abbreviated(_ path: String) -> String {
        let home = FolderAccess.realHome.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}

// MARK: - Permissions

private struct PermissionsPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section {
                PermissionRow(title: "Documents folder", detail: "", state: model.documentsState,
                              allow: model.allowDocuments, openSettings: { model.openPrivacy("privacy") })
                // No microphone while answering in the app is quarantined — see `Quarantine`.
                if !Quarantine.answerHere {
                    PermissionRow(title: "Microphone", detail: "", state: model.mic,
                                  allow: model.allowMic, openSettings: { model.openPrivacy("privacy-mic") })
                }
                PermissionRow(title: "Contacts", detail: contactsNote, state: model.contacts,
                              allow: model.allowContacts,
                              openSettings: { model.openPrivacy("privacy-contacts") })
            }
            Section {
                Toggle("Start when I log in", isOn: Binding(
                    get: { model.startAtLogin }, set: { model.setStartAtLogin($0) }))
                if model.loginState == "pending" {
                    Text("Waiting for your approval in System Settings.").foregroundStyle(.secondary)
                }
            } footer: {
                NoticeFooter(notice: model.notice[.permissions])
            }
        }
    }

    private var contactsNote: String {
        let n = Int(model.perms.num("contacts_named"))
        guard model.contacts == "allowed", n > 0 else { return "" }
        return "Names for \(n) \(n == 1 ? "person" : "people")"
    }

}

/// One permission: Allowed, waiting, a way to System Settings after a refusal, or Allow. Shared by
/// Settings and the setup window, so the two cannot describe the same permission differently.
struct PermissionRow: View {
    let title: String
    let detail: String
    let state: String
    let allow: () -> Void
    let openSettings: () -> Void

    var body: some View {
        LabeledContent {
            switch state {
            case "allowed":
                Label("Allowed", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green).labelStyle(.titleAndIcon)
            case "asking":
                Text("Waiting for your answer…").foregroundStyle(.secondary)
            case "refused":
                Button("Open System Settings…") { openSettings() }
            default:
                Button("Allow") { allow() }
            }
        } label: {
            Text(title)
            if !detail.isEmpty { Text(detail) }
        }
    }
}

// MARK: - Advanced

private struct AdvancedPane: View {
    @ObservedObject var model: SettingsModel
    #if !RECORDER
    @StateObject private var editing = Local<Override?>(nil)

    enum Override: String, Identifiable {
        case model, speech
        var id: String { rawValue }
    }
    #endif

    var body: some View {
        Form {
            Section {
                LabeledContent("Walk through setup again") {
                    Button("Run Setup…") { model.runSetup() }
                }
            }
            #if RECORDER
            Section {} footer: { NoticeFooter(notice: model.notice[.advanced]) }
            #else
            Section {
                LabeledContent("AI model") {
                    HStack {
                        Text(model.modelOverride.isEmpty ? "Automatic" : model.modelOverride)
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Button("Edit…") { editing.value = .model }
                    }
                }
                LabeledContent("Speech model") {
                    HStack {
                        Text(model.sttOverride.isEmpty ? "Automatic" : model.sttOverride)
                            .foregroundStyle(.secondary)
                        Button("Edit…") { editing.value = .speech }
                    }
                }
                // A DEVELOPER'S RESET, not an owner's (Stanley, 2026-10-01): "start fresh" was judged
                // too hard a concept for the hub. It drops what the assistant replays of the chat,
                // and the flag a caller's words set; the log and the memory stay.
                LabeledContent("Assistant's conversation") {
                    Button("Start Fresh") { model.startFresh() }
                }
                if model.thinkingPossible {
                    Toggle(isOn: Binding(get: { model.thinking }, set: { model.setThinking($0) })) {
                        Text("Thinking mode")
                        if model.thinking {
                            Text("On — answers are much slower, and no better on simple questions")
                        }
                    }
                }
            } header: {
                Text("For developers")
            } footer: {
                NoticeFooter(notice: model.notice[.advanced])
            }
            #endif
        }
        #if !RECORDER
        .sheet(item: $editing.value) { which in
            OverrideSheet(model: model, which: which)
        }
        #endif
    }
}

#if !RECORDER
private struct OverrideSheet: View {
    @ObservedObject var model: SettingsModel
    let which: AdvancedPane.Override
    @Environment(\.dismiss) private var dismiss
    @StateObject private var value = Local("")
    @StateObject private var busy = Local(false)

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField(which == .model ? "AI model" : "Speech model", text: $value.value,
                              prompt: Text(which == .model ? "owner/repo or owner/repo:Q4_K_M"
                                                           : "qwen3-asr-1.7b, large-v3-turbo…"))
                        .monospaced()
                } footer: {
                    NoticeFooter(notice: model.notice[.advanced])
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
                        let name = value.value.trimmingCharacters(in: .whitespaces)
                        let ok = which == .model ? await model.setModelOverride(name)
                                                 : await model.setSttOverride(name)
                        busy.value = false
                        if ok { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(busy.value)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 480)
        .onAppear {
            value.value = which == .model ? model.modelOverride : model.sttOverride
            model.notice[.advanced] = nil
        }
    }
}
#endif

// MARK: - About

private struct AboutPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section {
                LabeledContent("Version") {
                    Text(model.version).textSelection(.enabled)
                }
                #if !RECORDER
                LabeledContent("AI model") { aiModel }
                LabeledContent("Speech model") { speechModel }
                #endif
            } footer: {
                NoticeFooter(notice: model.notice[.about])
            }
            Section {
                LabeledContent("Logs") { Button("Export Logs…") { model.exportLogs() } }
            }
            // AN APP STORE BUILD IS UPDATED BY THE STORE, so it has no row to check for one.
            if !model.storeUpdates {
                Section {
                    LabeledContent {
                        HStack {
                            if model.update.bool("newer"), !model.update.str("version").isEmpty,
                               let url = URL(string: model.update.str("url")) {
                                Link(model.update.str("version"), destination: url)
                            } else {
                                Text(model.update["checked"] == nil ? "Not checked yet" : "None found")
                                    .foregroundStyle(.secondary)
                            }
                            Button("Check Now") { model.checkForUpdate() }.disabled(model.checking)
                        }
                    } label: {
                        Text("Newer version")
                        if let when = lastLooked { Text(when) }
                    }
                }
            }
        }
    }

    #if !RECORDER
    @ViewBuilder private var aiModel: some View {
        let pick = model.pick
        if let job = model.pickJob {
            progress(done: job.num("done_mb"), total: job.num("total_mb"))
        } else if !pick.str("model").isEmpty && !pick.bool("downloaded") {
            HStack {
                Text(model.modelDescription).foregroundStyle(.secondary)
                Button("Download · \(gb(pick.num("dl_mb")))") { model.downloadPick() }
            }
        } else {
            Text(model.modelDescription).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var speechModel: some View {
        let s = model.stt
        if s.bool("running") {
            progress(done: model.sttGotMB, total: s.num("mb"))
        } else if !s.bool("cached") {
            HStack {
                Text("\(model.sttName), not downloaded").foregroundStyle(.secondary)
                Button("Download · \(gb(s.num("mb")))") { model.downloadStt() }
            }
        } else {
            Text(model.sttName).foregroundStyle(.secondary)
        }
    }

    private func progress(done: Double, total: Double) -> some View {
        HStack {
            ProgressView(value: total > 0 ? min(done / total, 1) : 0).frame(width: 160)
            Text("\(Int(done)) / \(Int(total)) MB").monospacedDigit().foregroundStyle(.secondary)
        }
    }

    private func gb(_ mb: Double) -> String { String(format: "%.1f GB", mb / 1024) }
    #endif

    /// "Last looked 5 minutes ago", from whatever the daemon stored: epoch seconds or ISO 8601.
    private var lastLooked: String? {
        let raw = model.update["checked"]
        var date: Date?
        if let n = raw as? NSNumber { date = Date(timeIntervalSince1970: n.doubleValue) }
        if let s = raw as? String {
            date = ISO8601DateFormatter().date(from: s)
                ?? { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"; return f.date(from: s) }()
        }
        guard let date else { return nil }
        let rel = RelativeDateTimeFormatter().localizedString(for: date, relativeTo: Date())
        return "Last looked \(rel)" + (model.update["reachable"] as? Bool == false
                                        ? " — could not reach GitHub" : "")
    }
}
