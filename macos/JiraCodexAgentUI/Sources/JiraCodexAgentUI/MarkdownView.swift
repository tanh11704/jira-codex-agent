import AppKit
import SwiftUI

// A dependency-free Markdown renderer for Jira descriptions and Codex reports.
// Block structure is parsed here; inline syntax (bold, italic, code, links) uses Foundation.

enum MarkdownListMarker: Hashable {
    case bullet
    case ordered(Int)
    case task(done: Bool)
}

struct MarkdownListItem: Hashable {
    let depth: Int
    let marker: MarkdownListMarker
    let text: String
}

enum MarkdownBlock: Hashable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case list([MarkdownListItem])
    case code(language: String, code: String)
    case quote([MarkdownBlock])
    case table(header: [String], rows: [[String]])
    case rule
}

enum MarkdownParser {
    private static let listPattern = try! NSRegularExpression(pattern: #"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$"#)
    private static let headingPattern = try! NSRegularExpression(pattern: #"^(#{1,6})\s+(.*)$"#)
    private static let separatorPattern = try! NSRegularExpression(pattern: #"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$"#)

    static func parse(_ text: String) -> [MarkdownBlock] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var index = 0
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty {
                index += 1
            } else if trimmed.hasPrefix("```") {
                let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var code: [String] = []
                index += 1
                while index < lines.count, !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    code.append(lines[index])
                    index += 1
                }
                index += 1
                blocks.append(.code(language: language, code: code.joined(separator: "\n")))
            } else if let heading = match(headingPattern, trimmed) {
                blocks.append(.heading(level: heading[1].count, text: heading[2]))
                index += 1
            } else if isRule(trimmed) {
                blocks.append(.rule)
                index += 1
            } else if trimmed.hasPrefix("|") {
                var rows: [[String]] = []
                var hasHeader = false
                while index < lines.count, lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    let row = lines[index].trimmingCharacters(in: .whitespaces)
                    if rows.count == 1, match(separatorPattern, row) != nil {
                        hasHeader = true
                    } else {
                        rows.append(cells(row))
                    }
                    index += 1
                }
                let header = hasHeader ? rows.removeFirst() : []
                blocks.append(.table(header: header.allSatisfy(\.isEmpty) ? [] : header, rows: rows))
            } else if trimmed.hasPrefix(">") {
                var quoted: [String] = []
                while index < lines.count, lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    var content = String(lines[index].trimmingCharacters(in: .whitespaces).dropFirst())
                    if content.hasPrefix(" ") { content.removeFirst() }
                    quoted.append(content)
                    index += 1
                }
                blocks.append(.quote(parse(quoted.joined(separator: "\n"))))
            } else if match(listPattern, line) != nil {
                var items: [MarkdownListItem] = []
                while index < lines.count {
                    let current = lines[index]
                    if let parts = match(listPattern, current) {
                        items.append(listItem(indent: parts[1].count, marker: parts[2], text: parts[3]))
                    } else if !current.trimmingCharacters(in: .whitespaces).isEmpty,
                              current.hasPrefix("  "), let last = items.popLast() {
                        // Indented continuation of the previous item.
                        items.append(MarkdownListItem(depth: last.depth, marker: last.marker,
                                                      text: last.text + "\n" + current.trimmingCharacters(in: .whitespaces)))
                    } else {
                        break
                    }
                    index += 1
                }
                blocks.append(.list(items))
            } else {
                var paragraph: [String] = []
                while index < lines.count {
                    let current = lines[index]
                    let currentTrimmed = current.trimmingCharacters(in: .whitespaces)
                    if currentTrimmed.isEmpty || startsBlock(current) { break }
                    paragraph.append(currentTrimmed)
                    index += 1
                }
                blocks.append(.paragraph(paragraph.joined(separator: "\n")))
            }
        }
        return blocks
    }

    private static func startsBlock(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        return trimmed.hasPrefix("```") || trimmed.hasPrefix("|") || trimmed.hasPrefix(">") || isRule(trimmed)
            || match(headingPattern, trimmed) != nil || match(listPattern, line) != nil
    }

    private static func isRule(_ line: String) -> Bool {
        let compact = line.replacingOccurrences(of: " ", with: "")
        return compact.count >= 3 && ["-", "*", "_"].contains { mark in compact.allSatisfy { String($0) == mark } }
    }

    private static func listItem(indent: Int, marker: String, text: String) -> MarkdownListItem {
        let depth = min(indent / 2, 4)
        if marker.first?.isNumber == true {
            return MarkdownListItem(depth: depth, marker: .ordered(Int(marker.dropLast()) ?? 1), text: text)
        }
        for (prefix, done) in [("[ ] ", false), ("[x] ", true), ("[X] ", true)] where text.hasPrefix(prefix) {
            return MarkdownListItem(depth: depth, marker: .task(done: done), text: String(text.dropFirst(prefix.count)))
        }
        return MarkdownListItem(depth: depth, marker: .bullet, text: text)
    }

    private static func cells(_ row: String) -> [String] {
        var body = row
        if body.hasPrefix("|") { body.removeFirst() }
        if body.hasSuffix("|") && !body.hasSuffix("\\|") { body.removeLast() }
        let placeholder = "\u{0}"
        return body.replacingOccurrences(of: "\\|", with: placeholder)
            .components(separatedBy: "|")
            .map { $0.replacingOccurrences(of: placeholder, with: "|").trimmingCharacters(in: .whitespaces) }
    }

    private static func match(_ pattern: NSRegularExpression, _ text: String) -> [String]? {
        let range = NSRange(text.startIndex..., in: text)
        guard let result = pattern.firstMatch(in: text, range: range) else { return nil }
        return (0..<result.numberOfRanges).map { index in
            Range(result.range(at: index), in: text).map { String(text[$0]) } ?? ""
        }
    }
}

struct MarkdownView: View {
    let blocks: [MarkdownBlock]
    var fontSize: CGFloat = 13

    init(_ markdown: String, fontSize: CGFloat = 13) {
        self.blocks = MarkdownParser.parse(markdown)
        self.fontSize = fontSize
    }

    init(blocks: [MarkdownBlock], fontSize: CGFloat) {
        self.blocks = blocks
        self.fontSize = fontSize
    }

    var body: some View {
        VStack(alignment: .leading, spacing: fontSize * 0.75) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                view(for: block, first: index == 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func view(for block: MarkdownBlock, first: Bool) -> some View {
        switch block {
        case .heading(let level, let text):
            VStack(alignment: .leading, spacing: 4) {
                Text(inline(text))
                    .font(.system(size: headingSize(level), weight: level <= 2 ? .bold : .semibold))
                if level <= 2 { Divider() }
            }
            .padding(.top, first ? 0 : fontSize * 0.5)
        case .paragraph(let text):
            Text(inline(text))
                .font(.system(size: fontSize))
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        case .list(let items):
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        marker(item)
                        Text(inline(item.text, highlightGherkin: true))
                            .font(.system(size: fontSize))
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(item.depth) * 18)
                }
            }
        case .code(let language, let code):
            CodeBlockView(language: language, code: code, fontSize: fontSize - 1.5)
        case .quote(let inner):
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1.5).fill(AppTheme.accent.opacity(0.55)).frame(width: 3)
                MarkdownView(blocks: inner, fontSize: fontSize)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 6)
            .padding(.trailing, 8)
            .background(AppTheme.accent.opacity(0.05), in: RoundedRectangle(cornerRadius: 6))
        case .table(let header, let rows):
            tableView(header: header, rows: rows)
        case .rule:
            Divider().padding(.vertical, 2)
        }
    }

    @ViewBuilder
    private func marker(_ item: MarkdownListItem) -> some View {
        switch item.marker {
        case .bullet:
            Text(["•", "◦", "▪︎"][item.depth % 3])
                .font(.system(size: fontSize, weight: .bold))
                .foregroundStyle(AppTheme.accent)
                .frame(width: 12, alignment: .center)
        case .ordered(let number):
            Text("\(number).")
                .font(.system(size: fontSize).monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(minWidth: 18, alignment: .trailing)
        case .task(let done):
            Image(systemName: done ? "checkmark.square.fill" : "square")
                .foregroundStyle(done ? Color.green : Color.secondary)
                .frame(width: 14)
        }
    }

    private func tableView(header: [String], rows: [[String]]) -> some View {
        let columns = max(header.count, rows.map(\.count).max() ?? 0)
        func cell(_ text: String, bold: Bool) -> some View {
            Text(inline(text))
                .font(.system(size: fontSize - 1, weight: bold ? .semibold : .regular))
                .fixedSize(horizontal: false, vertical: true)
                .frame(minWidth: 60, maxWidth: 280, alignment: .leading)
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
        }
        return ScrollView(.horizontal, showsIndicators: true) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                if !header.isEmpty {
                    GridRow {
                        ForEach(0..<columns, id: \.self) { column in
                            cell(column < header.count ? header[column] : "", bold: true)
                        }
                    }
                    .background(Color.primary.opacity(0.07))
                    Divider()
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    GridRow {
                        ForEach(0..<columns, id: \.self) { column in
                            cell(column < row.count ? row[column] : "", bold: false)
                        }
                    }
                    .background(index % 2 == 1 ? Color.primary.opacity(0.03) : Color.clear)
                    if index < rows.count - 1 { Divider() }
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(.separator.opacity(0.6)))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
    }

    private func headingSize(_ level: Int) -> CGFloat {
        switch level {
        case 1: return fontSize + 7
        case 2: return fontSize + 4
        case 3: return fontSize + 2
        default: return fontSize + 0.5
        }
    }

    /// Inline Markdown with styled code spans; Acceptance Criteria keywords are emphasised.
    private func inline(_ text: String, highlightGherkin: Bool = false) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible)
        var source = text
        var keyword: AttributedString?
        if highlightGherkin {
            for word in ["Given", "When", "Then", "And", "But"] {
                for prefix in ["**\(word)** ", "\(word) "] where source.hasPrefix(prefix) {
                    var styled = AttributedString(word.uppercased())
                    styled.font = .system(size: fontSize - 2.5, weight: .heavy, design: .rounded)
                    styled.foregroundColor = gherkinColor(word)
                    keyword = styled + AttributedString("  ")
                    source = String(source.dropFirst(prefix.count))
                    break
                }
                if keyword != nil { break }
            }
        }
        var result = (try? AttributedString(markdown: source, options: options)) ?? AttributedString(source)
        for run in result.runs where run.inlinePresentationIntent?.contains(.code) == true {
            result[run.range].font = .system(size: fontSize - 1, design: .monospaced)
            result[run.range].foregroundColor = Color(nsColor: .systemPink)
            result[run.range].backgroundColor = Color.primary.opacity(0.07)
        }
        for run in result.runs where run.link != nil {
            result[run.range].foregroundColor = AppTheme.accent
            result[run.range].underlineStyle = .single
        }
        return (keyword ?? AttributedString()) + result
    }

    private func gherkinColor(_ word: String) -> Color {
        switch word {
        case "Given": return .purple
        case "When": return .blue
        case "Then": return .green
        default: return .secondary
        }
    }
}

private struct CodeBlockView: View {
    let language: String
    let code: String
    let fontSize: CGFloat
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language)
                    .font(.caption2.weight(.semibold).monospaced())
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption2)
                }
                .buttonStyle(.borderless)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(Color.primary.opacity(0.05))
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code)
                    .font(.system(size: fontSize, design: .monospaced))
                    .lineSpacing(2)
                    .padding(10)
                    .fixedSize(horizontal: true, vertical: true)
            }
        }
        .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.separator.opacity(0.5)))
    }
}

/// Large reading window for a long description or report.
/// Which long text of a task a reader window shows; the window looks the task up so its content stays live.
struct ReaderRequest: Codable, Hashable {
    enum Section: String, Codable { case description, plan, report }
    let taskID: String
    let section: Section

    var title: String {
        switch section {
        case .description: return "Description"
        case .plan: return "Claude plan"
        case .report: return "Codex report"
        }
    }

    func markdown(of task: AgentTask) -> String? {
        switch section {
        case .description: return task.description
        case .plan: return task.plan
        case .report: return task.parsedReport?.report
        }
    }
}

/// A resizable window reading one task's description, plan or report.
struct ReaderWindow: View {
    @EnvironmentObject private var store: AgentStore
    let request: ReaderRequest?

    var body: some View {
        if let request, let task = store.tasks.first(where: { $0.id == request.taskID }),
           let markdown = request.markdown(of: task), !markdown.isEmpty {
            MarkdownReader(title: task.summary, subtitle: "\(task.issueKey) · \(request.title)",
                           markdown: markdown, link: store.jiraIssueURL(task))
                .navigationTitle("\(task.issueKey) · \(request.title)")
        } else {
            Text("This content is no longer available.")
                .foregroundStyle(.secondary)
                .frame(minWidth: 480, maxWidth: .infinity, minHeight: 320, maxHeight: .infinity)
        }
    }
}

struct MarkdownReader: View {
    let title: String
    let subtitle: String
    let markdown: String
    let link: URL?
    @AppStorage("readerFontSize") private var fontSize: Double = 14
    @AppStorage("readerFullWidth") private var fullWidth = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(subtitle).font(.caption.monospaced()).foregroundStyle(.secondary)
                    Text(title).font(.title2.bold()).lineLimit(2).textSelection(.enabled)
                }
                Spacer()
                HStack(spacing: 4) {
                    Button { fontSize = max(11, fontSize - 1) } label: { Image(systemName: "textformat.size.smaller") }
                        .keyboardShortcut("-", modifiers: .command)
                        .help("Smaller text (⌘-)")
                    Button { fontSize = min(28, fontSize + 1) } label: { Image(systemName: "textformat.size.larger") }
                        .keyboardShortcut("=", modifiers: .command)
                        .help("Larger text (⌘+)")
                    Button { fullWidth.toggle() } label: {
                        Image(systemName: fullWidth ? "arrow.right.and.line.vertical.and.arrow.left"
                                                    : "arrow.left.and.line.vertical.and.arrow.right")
                    }
                    .help(fullWidth ? "Comfortable reading width" : "Use full window width")
                    if let link {
                        Button { NSWorkspace.shared.open(link) } label: { Image(systemName: "arrow.up.right.square") }
                            .help("Open in Jira")
                    }
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(markdown, forType: .string)
                    } label: { Image(systemName: "doc.on.doc") }
                        .help("Copy Markdown")
                }
                .buttonStyle(.borderless)
            }
            .padding(20)
            Divider()
            ScrollView {
                MarkdownView(markdown, fontSize: fontSize)
                    .frame(maxWidth: fullWidth ? .infinity : 760 * fontSize / 14, alignment: .leading)
                    .padding(.horizontal, 32)
                    .padding(.vertical, 24)
                    .frame(maxWidth: .infinity)
            }
            .background(Color(nsColor: .textBackgroundColor))
        }
        .frame(minWidth: 480, idealWidth: 860, maxWidth: .infinity, minHeight: 360, idealHeight: 760, maxHeight: .infinity)
    }
}
