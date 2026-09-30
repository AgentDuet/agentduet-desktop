import Contacts
import Foundation

/// Names for the numbers that call or write, from the owner's Contacts (2026-09-29).
///
/// ONLY THE APP CAN ASK. Contacts is privacy-protected per app, and the prompt is raised by the
/// process that asks — so the shell holds the access, as it does the Documents bookmark.
/// Contacts on a Mac is every account macOS syncs: iCloud, a Google account added under
/// Internet Accounts, Exchange. So Google contacts come through here with no Google token.
///
/// THE ADDRESS BOOK STAYS IN MEMORY. The daemon writes the numbers it wants named
/// (`run/contacts-wanted.json`); this writes names for THOSE numbers only (`run/contacts.json`),
/// so what lands on disk is who has already called or written, never the owner's contacts.
/// For each of those it also writes the contact's email addresses — so the assistant can draft
/// an email to someone who called — and the contact's identifier, so the hub can open that card
/// in Contacts.
///
/// Looked up again when the wanted list changes, when Contacts changes (a sync counts), and when
/// access changes. Polled every three seconds for the first two, like MicWatch: one file date
/// and one status read.
final class ContactsWatch {

    private let wantedFile: URL
    private let outFile: URL
    private var timer: Timer?
    private var lastWanted: Date?
    private var lastAccess = ""
    private var dirty = true
    private var busy = false

    init(home: URL) {
        wantedFile = home.appendingPathComponent("run/contacts-wanted.json")
        outFile = home.appendingPathComponent("run/contacts.json")
    }

    func start() {
        NotificationCenter.default.addObserver(forName: .CNContactStoreDidChange, object: nil,
                                               queue: .main) { [weak self] _ in self?.dirty = true }
        tick()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    static func access() -> String {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return "not-asked"
        case .denied, .restricted: return "refused"
        default: return "allowed"      // .authorized, and a limited grant where macOS has one
        }
    }

    /// The macOS prompt. Answers at once, without asking, if the owner already decided.
    ///
    /// THE PAGE IS TOLD ONLY AFTER THE FILE IS WRITTEN. It re-reads the state the moment it hears,
    /// and telling it first (then writing on a background queue) had it read the old "not
    /// asked" and keep showing Allow over a grant that had worked.
    func ask(done: @escaping (Bool) -> Void) {
        CNContactStore().requestAccess(for: .contacts) { ok, _ in
            DispatchQueue.main.async {
                self.dirty = true
                self.tick(force: true) { done(ok) }
            }
        }
    }

    private func tick(force: Bool = false, then: (() -> Void)? = nil) {
        let access = Self.access()
        let wantedDate = (try? wantedFile.resourceValues(forKeys: [.contentModificationDateKey]))?
            .contentModificationDate
        guard (dirty || access != lastAccess || wantedDate != lastWanted) && (force || !busy) else {
            then?(); return
        }
        dirty = false
        lastAccess = access
        lastWanted = wantedDate
        let wanted = (try? Data(contentsOf: wantedFile))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String] } ?? []
        busy = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let (people, read) = access == "allowed" ? Self.resolve(wanted) : ([:], 0)
            let names = people.mapValues(\.name)
            let rows = people.mapValues { ["name": $0.name, "emails": $0.emails, "id": $0.id] as [String: Any] }
            // `read` is how many numbers Contacts gave us — a count, not the numbers — so "your
            // caller is not in Contacts" can be told apart from "Contacts could not be read".
            self.write(["access": access, "at": Date().timeIntervalSince1970, "names": names,
                        "people": rows, "read": read])
            DispatchQueue.main.async { self.busy = false; then?() }
        }
    }

    struct Match { let name: String; let emails: [String]; let id: String }

    /// Every number in Contacts, then each wanted number matched against them.
    static func resolve(_ wanted: [String]) -> (people: [String: Match], read: Int) {
        guard !wanted.isEmpty else { return ([:], 0) }
        let keys: [CNKeyDescriptor] = [
            CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
        ]
        var book: [(digits: String, name: String)] = []
        var byName: [String: Match] = [:]
        let request = CNContactFetchRequest(keysToFetch: keys)
        try? CNContactStore().enumerateContacts(with: request) { contact, _ in
            let person = CNContactFormatter.string(from: contact, style: .fullName) ?? ""
            let name = person.isEmpty ? contact.organizationName : person
            guard !name.isEmpty else { return }
            for number in contact.phoneNumbers {
                let digits = PhoneMatch.digits(number.value.stringValue)
                if !digits.isEmpty { book.append((digits, name)) }
            }
            // PhoneMatch answers with a NAME, and only when one name owns the number, so a name
            // is enough to find the card again. Two cards under one name leave no card at all.
            byName[name] = byName[name] == nil
                ? Match(name: name, emails: contact.emailAddresses.map { $0.value as String },
                        id: contact.identifier)
                : Match(name: name, emails: [], id: "")
        }
        var out: [String: Match] = [:]
        for w in wanted {
            if let name = PhoneMatch.best(PhoneMatch.digits(w), in: book) {
                out[w] = byName[name] ?? Match(name: name, emails: [], id: "")
            }
        }
        return (out, book.count)
    }

    private func write(_ row: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: row) else { return }
        try? FileManager.default.createDirectory(at: outFile.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? data.write(to: outFile, options: .atomic)
    }
}

/// Matching a number as it ARRIVES ("+6596918851") against numbers as PEOPLE SAVE THEM
/// ("9691 8851", "+65 9691-8851", "0065…").
///
/// Digits only, with an international "00" or a trunk "0" taken off the front. Then equal digits
/// match, and so does a saved number that is the TAIL of the caller's — "96918851" is
/// "+65 96918851" saved without its country code — but only when the shorter has at least seven
/// digits, so a short code cannot match half the world. Two different names for one number is a
/// question this cannot answer, so it shows none: the number is honest, a guess is not.
enum PhoneMatch {
    static func digits(_ s: String) -> String {
        var d = s.filter(\.isNumber).map(String.init).joined()
        if d.hasPrefix("00") { d.removeFirst(2) }
        while d.hasPrefix("0") { d.removeFirst() }
        return d
    }

    static func best(_ want: String, in book: [(digits: String, name: String)]) -> String? {
        guard want.count >= 7 else { return nil }
        let exact = Set(book.filter { $0.digits == want }.map(\.name))
        if exact.count == 1 { return exact.first }
        if exact.count > 1 { return nil }
        let tail = Set(book.filter { entry in
            let (short, long) = entry.digits.count <= want.count ? (entry.digits, want)
                                                                 : (want, entry.digits)
            return short.count >= 7 && long.hasSuffix(short)
        }.map(\.name))
        return tail.count == 1 ? tail.first : nil
    }
}
