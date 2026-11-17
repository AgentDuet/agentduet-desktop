#if !RECORDER && !TEAM
import SwiftUI
import UniformTypeIdentifiers

/// Saves a snapshot of the existing brief, without asking the model to regenerate it.
struct SaveSummaryButton: View {
    let summary: String

    var body: some View {
        Button("Save Summary…") { Self.save(summary) }
        .buttonStyle(.borderedProminent)
    }

    @MainActor private static func save(_ text: String) {
        // Capture before the panel opens: polling may replace the person's brief while it is up.
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let panel = NSSavePanel()
        panel.title = "Save Summary"
        panel.nameFieldStringValue = "summary.txt"
        panel.allowedContentTypes = [.plainText]
        panel.allowsOtherFileTypes = false
        NSApp.activate(ignoringOtherApps: true)
        // Keep SwiftUI's event loop free while the native panel is open.
        panel.begin { response in
            guard response == .OK, let destination = panel.url else { return }
            do {
                try text.write(to: destination, atomically: true, encoding: .utf8)
                NSWorkspace.shared.activateFileViewerSelecting([destination])
            } catch {
                let alert = NSAlert(error: error)
                alert.messageText = "Could not save the summary"
                alert.runModal()
            }
        }
    }
}
#endif
