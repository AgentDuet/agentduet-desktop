import SwiftUI

/// A Markdown document drawn with SwiftUI text (2026-10-05), for the Terms of Use and the Privacy
/// Policy. `AttributedString(markdown:)` reads only the INLINE parts as a SwiftUI `Text` shows
/// them: headings, lists, quotes and paragraphs all run into one line. So the blocks are split
/// here, and each block's inline text goes through it.
///
/// It reads what those two documents use, not all of Markdown: headings, paragraphs, quotes,
/// "-" and "1." lists with indented continuation lines, and tables. A table row is drawn as a
/// bullet ("**first cell** — header: cell; …"), which reads well at this width.
struct MarkdownDocument: View {
    let source: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(Self.blocks(source).enumerated()), id: \.offset) { _, block in
                view(block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    enum Block {
        case heading(Int, String)
        case paragraph(String)
        case quote(String)
        case item(String, String)            // its marker ("•" or "1."), its text
    }

    @ViewBuilder private func view(_ block: Block) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Self.inline(text)).font(level == 1 ? .title2.bold() : .headline)
                .padding(.top, level == 1 ? 0 : 8)
        case .paragraph(let text):
            Text(Self.inline(text)).fixedSize(horizontal: false, vertical: true)
        case .quote(let text):
            Text(Self.inline(text)).font(.callout).foregroundStyle(.secondary)
                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.1)))
        case .item(let marker, let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(marker).foregroundStyle(.secondary)
                Text(Self.inline(text)).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, 8)
        }
    }

    static func inline(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(
            interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
    }

    static func blocks(_ source: String) -> [Block] {
        var out: [Block] = []
        var para: [String] = []
        var quote: [String] = []
        var item: (String, [String])?
        var header: [String] = []

        func flush() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: " "))); para = [] }
            if !quote.isEmpty { out.append(.quote(quote.joined(separator: " "))); quote = [] }
            if let (m, lines) = item { out.append(.item(m, lines.joined(separator: " "))); item = nil }
        }
        func cells(_ line: String) -> [String] {
            line.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "|"))
                .components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
        }

        for raw in source.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { flush(); header = []; continue }
            if line.hasPrefix("|") {
                flush()
                let row = cells(line)
                if row.allSatisfy({ $0.allSatisfy { "-: ".contains($0) } }) { continue }
                if header.isEmpty { header = row; continue }
                var parts = ["**\(row.first ?? "")**"]
                for (i, cell) in row.enumerated().dropFirst() where !cell.isEmpty {
                    parts.append(i < header.count ? "\(header[i].lowercased()): \(cell)" : cell)
                }
                out.append(.item("•", parts.joined(separator: " — ")))
                continue
            }
            if let hashes = line.firstIndex(where: { $0 != "#" }), line.hasPrefix("#"),
               line[hashes] == " " {
                flush()
                out.append(.heading(line.distance(from: line.startIndex, to: hashes),
                                    String(line[hashes...]).trimmingCharacters(in: .whitespaces)))
                continue
            }
            if line.hasPrefix(">") {
                if !para.isEmpty || item != nil { flush() }
                quote.append(String(line.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            if line.hasPrefix("- ") {
                flush()
                item = ("•", [String(line.dropFirst(2))])
                continue
            }
            if let dot = line.firstIndex(of: "."), line[..<dot].allSatisfy(\.isNumber),
               dot != line.startIndex, line[line.index(after: dot)...].hasPrefix(" ") {
                flush()
                item = (String(line[...dot]), [String(line[line.index(dot, offsetBy: 2)...])])
                continue
            }
            // A CONTINUATION: an indented line carries on the list item above it.
            if item != nil && raw.hasPrefix(" ") { item!.1.append(line); continue }
            if item != nil || !quote.isEmpty { flush() }
            para.append(line)
        }
        flush()
        return out
    }
}
