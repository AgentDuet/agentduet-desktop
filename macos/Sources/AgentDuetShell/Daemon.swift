import Foundation

/// Owns the Python daemon: finds it, starts it, learns what it bound, and stops it.
///
/// THE HANDSHAKE ALREADY EXISTED. `web.py` writes what it actually bound to `run/site-url`, and
/// `shell.py` polls that same file for the pywebview window. Reusing it rather than inventing a
/// port protocol means the Swift shell and the Python window agree by construction, and neither
/// has to guess a port or a token.
final class Daemon {

    enum Failure: Error, LocalizedError {
        case binaryMissing(String)
        case neverCameUp(String)

        var errorDescription: String? {
            switch self {
            case .binaryMissing(let where_):
                return "The AgentDuet service is missing from this app.\n\nExpected it at:\n\(where_)"
            case .neverCameUp(let log):
                return "The AgentDuet service did not start.\n\n\(log)"
            }
        }
    }

    private var process: Process?
    /// Only stop what WE started. Someone running `agentduet-desktop run` in a terminal and then
    /// opening the app must not have their daemon killed when they close the window.
    private var weStartedIt = false

    // MARK: - where things are

    /// `$AGENTDUET_HOME`, default `~/.agentduet-desktop` — the same resolution as `paths.home()`.
    private var instanceHome: URL {
        if let explicit = ProcessInfo.processInfo.environment["AGENTDUET_HOME"], !explicit.isEmpty {
            return URL(fileURLWithPath: explicit)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".agentduet-desktop")
    }

    private var siteURLFile: URL { instanceHome.appendingPathComponent("run/site-url") }
    /// `run/daemon.log`, which is what the daemon actually writes — see secretary_agent.py and
    /// service.LOGFILE. This said `secretary.log` for as long as the shell existed, so the
    /// "service did not start" dialog tailed a file that has never been created and showed the
    /// owner an empty reason for the one failure it exists to explain.
    private var logFile: URL { instanceHome.appendingPathComponent("run/daemon.log") }

    /// The daemon binary, beside this one in `Contents/MacOS`.
    ///
    /// Falls back to a sibling of the built Swift binary so `swift run` works from a checkout,
    /// where there is no bundle at all — otherwise the only way to try the shell is a full
    /// packaging run.
    private var binary: URL? {
        let fm = FileManager.default
        var candidates: [URL] = []
        if let dir = Bundle.main.executableURL?.resolvingSymlinksInPath().deletingLastPathComponent() {
            candidates.append(dir.appendingPathComponent("agentduet-desktop"))
        }
        candidates.append(URL(fileURLWithPath: "/usr/local/bin/agentduet-desktop"))
        return candidates.first { fm.isExecutableFile(atPath: $0.path) }
    }

    // MARK: - lifecycle

    func start() -> Result<URL, Error> {
        // ALREADY RUNNING? Attach rather than spawn. A second daemon cannot bind the port and
        // now exits rather than limping on (that guard was added after two daemons ran for two
        // hours and one served stale code), so spawning blindly would just fail here instead.
        if let live = recordedURL(), responds(live) {
            weStartedIt = false
            return .success(live)
        }

        guard let bin = binary else {
            let expected = Bundle.main.executableURL?.deletingLastPathComponent()
                .appendingPathComponent("agentduet-desktop").path ?? "Contents/MacOS/agentduet-desktop"
            return .failure(Failure.binaryMissing(expected))
        }

        let p = Process()
        p.executableURL = bin
        // --no-window: THIS is the window. The daemon must not try to open one of its own.
        p.arguments = ["run", "--no-window"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            return .failure(error)
        }
        process = p
        weStartedIt = true

        // Poll for a URL THAT ANSWERS, not merely a file that exists: `run/site-url` survives a
        // crash, so a stale one would send the window at a port with nothing behind it.
        let deadline = Date().addingTimeInterval(45)
        while Date() < deadline {
            if let u = recordedURL(), responds(u) { return .success(u) }
            if !p.isRunning { break }          // it died; stop waiting and say why
            Thread.sleep(forTimeInterval: 0.25)
        }
        return .failure(Failure.neverCameUp(logTail()))
    }

    func stop() {
        guard weStartedIt, let p = process, p.isRunning else { return }
        p.terminate()                                   // SIGTERM
        // SIGTERM IS CAUGHT SOMEWHERE IN THE ASYNC STACK and does not always exit — the CLI's
        // own `stop` had to learn the same thing. Never report stopped on the strength of a
        // signal sent; wait, then insist.
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    // MARK: - is it still up?

    /// Did the daemon WE spawned survive? Free, synchronous, and no network — so it costs
    /// nothing to ask every time a menu opens, and it writes no request into daemon.log.
    ///
    /// nil when we did not start it (someone had one running and we attached), because then we
    /// hold no Process to ask and only the network knows — see `probe`.
    var spawnedAndAlive: Bool? {
        weStartedIt ? (process?.isRunning ?? false) : nil
    }

    /// Ask the network, OFF THE MAIN THREAD. `responds` blocks for up to three seconds, which
    /// would freeze the menu it is being drawn into.
    func probe(_ done: @escaping (Bool) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let up = self?.recordedURL().map { self?.responds($0) ?? false } ?? false
            DispatchQueue.main.async { done(up) }
        }
    }

    // MARK: - helpers

    private func recordedURL() -> URL? {
        guard let text = try? String(contentsOf: siteURLFile, encoding: .utf8) else { return nil }
        return URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// Is something actually serving there? BLOCKS — callers must be off the main thread.
    ///
    /// Any HTTP answer counts, including 401: the question is whether the daemon is listening,
    /// not whether this request carried the right token.
    private func responds(_ url: URL) -> Bool {
        var request = URLRequest(url: url)
        request.httpMethod = "HEAD"
        request.timeoutInterval = 2
        var ok = false
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode < 500 { ok = true }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 3)
        return ok
    }

    /// The end of the daemon's log, so a failure says what went wrong instead of "it did not
    /// start" — which is the message that sends someone to us rather than to the answer.
    private func logTail(lines: Int = 12) -> String {
        guard let text = try? String(contentsOf: logFile, encoding: .utf8) else {
            return "No log at \(logFile.path)."
        }
        return text.split(separator: "\n").suffix(lines).joined(separator: "\n")
    }
}
