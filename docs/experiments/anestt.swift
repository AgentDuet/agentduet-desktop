import Speech
import AVFoundation
import Foundation

@available(macOS 26.0, *)
enum Runner {
    static func transcribe(_ url: URL, locale: Locale) async throws -> String {
        let transcriber = SpeechTranscriber(locale: locale,
                                            transcriptionOptions: [],
                                            reportingOptions: [],
                                            attributeOptions: [])
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let file = try AVAudioFile(forReading: url)

        let collected = Task { () -> String in
            var out = ""
            for try await result in transcriber.results {
                out += String(result.text.characters)
            }
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
    static func main() async {
        guard #available(macOS 26.0, *) else { print("needs macOS 26"); exit(2) }
        let args = CommandLine.arguments
        guard args.count >= 2 else { print("usage: anestt <file.wav> [locale]"); exit(2) }
        let locale = Locale(identifier: args.count > 2 ? args[2] : "en-SG")
        let t0 = Date()
        do {
            let text = try await Runner.transcribe(URL(fileURLWithPath: args[1]), locale: locale)
            let secs = Date().timeIntervalSince(t0)
            FileHandle.standardError.write("elapsed \(String(format: "%.2f", secs))s\n".data(using: .utf8)!)
            print(text)
        } catch {
            print("ERROR: \(error)")
            exit(1)
        }
    }
}
