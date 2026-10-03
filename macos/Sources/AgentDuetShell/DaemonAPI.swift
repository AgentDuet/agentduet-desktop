import Foundation

/// The daemon's owner API, as the native windows call it (2026-09-29).
///
/// THE API IS THE CONTRACT BETWEEN THE TWO UIS. The HTML pages and the native Settings window
/// read and write the same `/api/*` routes with the same bodies, so a setting behaves the same
/// whichever surface changed it. Each platform draws its own UI; none of them owns a rule.
///
/// Loosely typed on purpose: the routes return JSON dictionaries whose fields grow as the product
/// does, and a strict decoder would fail a whole screen over one new field. A missing field reads
/// as empty, which is what the pages do too.
typealias JSON = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func str(_ key: String) -> String { self[key] as? String ?? "" }
    func bool(_ key: String) -> Bool { self[key] as? Bool ?? false }
    func num(_ key: String) -> Double { (self[key] as? NSNumber)?.doubleValue ?? 0 }
    func obj(_ key: String) -> JSON { self[key] as? JSON ?? [:] }
}

struct DaemonAPI {
    let base: URL
    let token: String

    /// From the site URL the daemon wrote (`http://127.0.0.1:8899/?t=<token>`).
    init?(site: URL) {
        guard let parts = URLComponents(url: site, resolvingAgainstBaseURL: false),
              let token = parts.queryItems?.first(where: { $0.name == "t" })?.value,
              let scheme = parts.scheme, let host = parts.host else { return nil }
        var root = URLComponents()
        root.scheme = scheme
        root.host = host
        root.port = parts.port
        guard let base = root.url else { return nil }
        self.base = base
        self.token = token
    }

    /// A page of the site, with the token — for handing the main window to setup.
    func page(_ path: String, _ extra: [URLQueryItem] = []) -> URL {
        var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        c.queryItems = [URLQueryItem(name: "t", value: token)] + extra
        return c.url!
    }

    func get(_ path: String) async -> JSON {
        await send(URLRequest(url: page(path)))
    }

    func post(_ path: String, _ body: JSON = [:], query: [String: String] = [:]) async -> JSON {
        var request = URLRequest(url: page(path, query.map { URLQueryItem(name: $0.key, value: $0.value) }))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        return await send(request)
    }

    /// A route's raw bytes — the log bundle, which is a zip rather than JSON. nil on any failure.
    func data(_ path: String) async -> Data? {
        var request = URLRequest(url: page(path))
        request.timeoutInterval = 60
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    /// Never throws: a failure comes back as `{"ok": false, "message": …}`, the shape every
    /// route already uses for its own refusals, so the caller has one case to handle.
    private func send(_ request: URLRequest) async -> JSON {
        var request = request
        request.timeoutInterval = 30
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            if (response as? HTTPURLResponse)?.statusCode == 401 {
                return ["ok": false, "message": "This window is signed out. Reopen \(Edition.product)."]
            }
            return (try? JSONSerialization.jsonObject(with: data)) as? JSON ?? [:]
        } catch {
            return ["ok": false, "message": "\(Edition.product) is not running behind this window."]
        }
    }
}
