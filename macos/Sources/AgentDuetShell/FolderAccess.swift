import AppKit

/// The owner's Documents folder, the SANDBOX way (App Store spike, 2026-09-29).
///
/// A sandboxed app has no entitlement for Documents, and cannot trigger the plain "would like to
/// access your Documents folder" prompt the Developer ID build does. The one way in is a folder
/// the owner chooses in the system open panel — for a sandboxed app, THAT panel is the permission
/// prompt. So setup's Documents row opens it, already in Documents, with the button labelled
/// Allow: one click, like the prompt it replaces.
///
/// The grant lasts only as long as this process unless it is kept, so the choice is stored as a
/// security-scoped bookmark and resolved on every launch — BEFORE the daemon starts, because
/// access this process gains is not handed to a child it already launched. That is also why the
/// daemon is restarted right after the owner presses Allow.
///
/// What the daemon learns is the PATH, in `run/documents-folder`; the bookmark stays with the
/// shell, which is the only process that can resolve it.
enum FolderAccess {

    /// The user's real home. In the sandbox `homeDirectoryForCurrentUser` is the CONTAINER, and
    /// the open panel must start in the real Documents, not the container's empty one.
    static var realHome: URL {
        if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir {
            return URL(fileURLWithPath: String(cString: dir))
        }
        return FileManager.default.homeDirectoryForCurrentUser
    }

    private static var accessing: URL?

    private static func bookmarkFile(_ home: URL) -> URL { home.appendingPathComponent("run/documents.bookmark") }
    private static func folderFile(_ home: URL) -> URL { home.appendingPathComponent("run/documents-folder") }

    /// Resolve the stored bookmark and keep access for the life of the app. Call before the
    /// daemon starts. Quiet when there is none — the owner has not been asked yet.
    static func restore(home: URL) {
        guard let data = try? Data(contentsOf: bookmarkFile(home)) else { return }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: data, options: [.withSecurityScope],
                                 relativeTo: nil, bookmarkDataIsStale: &stale) else { return }
        if accessing != url {
            accessing?.stopAccessingSecurityScopedResource()
            accessing = url.startAccessingSecurityScopedResource() ? url : nil
        }
        // A STALE bookmark still resolves; saving it again keeps it resolving next time.
        if stale { try? save(url, home: home) }
        try? url.path.write(to: folderFile(home), atomically: true, encoding: .utf8)
    }

    static func save(_ url: URL, home: URL) throws {
        let data = try url.bookmarkData(options: [.withSecurityScope],
                                        includingResourceValuesForKeys: nil, relativeTo: nil)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("run"),
                                                withIntermediateDirectories: true)
        try data.write(to: bookmarkFile(home), options: .atomic)
        try url.path.write(to: folderFile(home), atomically: true, encoding: .utf8)
    }

    /// The panel. `done` gets the folder chosen, or nil if the owner cancelled.
    /// The folder allowed now, from `run/documents-folder`, or nil.
    static func current(home: URL) -> URL? {
        guard let path = try? String(contentsOf: folderFile(home), encoding: .utf8),
              !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    static func ask(over window: NSWindow?, startingIn start: URL? = nil,
                    done: @escaping (URL?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        // IN DOCUMENTS, with nothing selected: pressing the button with no selection chooses
        // the folder the panel is showing, so the owner allows Documents in one click.
        panel.directoryURL = start ?? realHome.appendingPathComponent("Documents")
        panel.prompt = "Allow"
        panel.message = "Location to store AgentDuet \(Edition.kept)"
        let finish: (NSApplication.ModalResponse) -> Void = { r in done(r == .OK ? panel.url : nil) }
        if let window { panel.beginSheetModal(for: window, completionHandler: finish) }
        else { finish(panel.runModal()) }
    }
}
