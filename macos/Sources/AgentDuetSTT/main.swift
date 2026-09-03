// Apple's on-device speech recognition, as a command the Python daemon can call.
//
// WHY A SEPARATE BINARY. SpeechAnalyzer is a Swift-first async API and the daemon is Python in a
// PyInstaller bundle, so there is no in-process route. A tiny command that takes a file and
// prints text is the whole bridge — and it is a COMMAND rather than part of the shell so it
// works under the pywebview build, from the CLI, and on a headless `run` with no window at all.
//
// WHY IT IS WORTH THE BRIDGE, measured on an M5 against faster-whisper large-v3-turbo on a real
// 222-second call from the bank sample:
//
//     Whisper   21.5s wall   88.5s CPU     729 chars
//     this       1.1s wall    0.06s CPU     617 chars
//
// Nineteen times faster and about fifteen hundred times less CPU, for comparable output. On a
// laptop transcribing calls all day that is the difference between fans and silence.
//
// WHAT IT CANNOT DO, and why Whisper stays: thirty locales, none of them Malay, Vietnamese,
// Tamil, Thai, Indonesian or Hindi — and no language detection at all. Told the wrong language
// it produces confident nonsense rather than an error, so the caller decides the locale and
// falls back when this cannot serve it.
import Speech
import AVFoundation
import Foundation

@available(macOS 26.0, *)
enum Engine {
    /// Installed locales, best-matching first for a bare language code like "en".
    ///
    /// PREFER THE MACHINE'S OWN REGION. "en" is nine different models here, and en-SG hears
    /// Singaporean English measurably better than en-US does. The system region is the closest
    /// thing to a free answer about which one an owner wants.
    static func resolve(_ wanted: String) async -> Locale? {
        let installed = await SpeechTranscriber.installedLocales
        // An exact BCP-47 match wins outright.
        if let hit = installed.first(where: {
            $0.identifier(.bcp47).caseInsensitiveCompare(wanted) == .orderedSame }) {
            return hit
        }
        let lang = wanted.split(separator: "-").first.map(String.init)?.lowercased() ?? wanted
        let sameLanguage = installed.filter {
            $0.identifier(.bcp47).lowercased().hasPrefix(lang + "-") }
        guard !sameLanguage.isEmpty else { return nil }
        let region = Locale.current.region?.identifier.uppercased()
        if let region, let mine = sameLanguage.first(where: {
            $0.identifier(.bcp47).uppercased().hasSuffix("-" + region) }) {
            return mine
        }
        return sameLanguage.sorted { $0.identifier(.bcp47) < $1.identifier(.bcp47) }.first
    }

    static func transcribe(_ url: URL, locale: Locale) async throws -> String {
        let transcriber = SpeechTranscriber(locale: locale,
                                            transcriptionOptions: [],
                                            reportingOptions: [],
                                            attributeOptions: [])
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let file = try AVAudioFile(forReading: url)
        let collected = Task { () -> String in
            var out = ""
            for try await result in transcriber.results { out += String(result.text.characters) }
            return out
        }
        if let last = try await analyzer.analyzeSequence(from: file) {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            await analyzer.cancelAndFinishNow()
        }
        return try await collected.value
    }
}

@main
struct Main {
    static func usage() -> Never {
        FileHandle.standardError.write("""
        usage: agentduet-stt --locales
               agentduet-stt <audio-file> [language-or-locale]

        --locales prints the installed locale ids, one per line, so the caller can decide
        whether this engine can serve a language before handing it a file.
        """.data(using: .utf8)!)
        exit(2)
    }

    static func main() async {
        guard #available(macOS 26.0, *) else {
            FileHandle.standardError.write("needs macOS 26\n".data(using: .utf8)!)
            exit(3)                      // DISTINCT from a usage or transcription failure, so
        }                                // the caller can tell "too old" from "went wrong".
        let args = Array(CommandLine.arguments.dropFirst())
        guard let first = args.first else { usage() }

        if first == "--locales" {
            for l in await SpeechTranscriber.installedLocales
                .map({ $0.identifier(.bcp47) }).sorted() { print(l) }
            exit(0)
        }

        let wanted = args.count > 1 ? args[1] : "en"
        guard let locale = await Engine.resolve(wanted) else {
            FileHandle.standardError.write(
                "no installed locale for '\(wanted)'\n".data(using: .utf8)!)
            exit(4)                      // The caller falls back to Whisper on this one.
        }
        do {
            let text = try await Engine.transcribe(URL(fileURLWithPath: first), locale: locale)
            FileHandle.standardError.write(
                "locale \(locale.identifier(.bcp47))\n".data(using: .utf8)!)
            print(text)
        } catch {
            FileHandle.standardError.write("failed: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
}
