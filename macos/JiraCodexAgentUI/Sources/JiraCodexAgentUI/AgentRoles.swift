import Foundation
import SwiftUI

/// A coding or reviewing agent the daemon can run.
enum AgentKind: String, CaseIterable, Identifiable {
    case codex, claude, antigravity

    var id: String { rawValue }

    var name: String {
        switch self {
        case .codex: return "Codex"
        case .claude: return "Claude"
        case .antigravity: return "Antigravity"
        }
    }

    /// Name for the agent that writes code, as used in reports.
    var coderName: String { self == .claude ? "Claude Code" : name }

    var symbol: String {
        switch self {
        case .codex: return "chevron.left.forwardslash.chevron.right"
        case .claude: return "sparkles"
        case .antigravity: return "atom"
        }
    }

    var tint: Color {
        switch self {
        case .codex: return AppTheme.accent
        case .claude: return .orange
        case .antigravity: return .purple
        }
    }
}

/// A job in the pipeline. Raw values match the daemon's role names and JCA_<ROLE>_AGENT keys.
enum AgentRole: String, CaseIterable, Identifiable {
    case plan, backend, frontend, review, decide

    var id: String { rawValue }

    var title: String {
        switch self {
        case .plan: return "Plan"
        case .backend: return "Backend"
        case .frontend: return "Frontend"
        case .review: return "Review"
        case .decide: return "Decisions"
        }
    }

    /// Short tag shown on a task, e.g. "PLAN", "BE", "FE".
    var tag: String {
        switch self {
        case .plan: return "PLAN"
        case .backend: return "BE"
        case .frontend: return "FE"
        case .review: return "REVIEW"
        case .decide: return "DECISION"
        }
    }

    var symbol: String {
        switch self {
        case .plan: return "list.bullet.clipboard"
        case .backend: return "server.rack"
        case .frontend: return "paintbrush.pointed"
        case .review: return "eye"
        case .decide: return "person.badge.shield.checkmark"
        }
    }

    /// Agents the daemon accepts for this job. Read-only passes need a structured read-only CLI run.
    var agents: [AgentKind] {
        switch self {
        case .plan, .review, .decide: return [.claude, .antigravity]
        case .backend: return [.codex, .claude, .antigravity]
        case .frontend: return [.antigravity, .claude, .codex]
        }
    }

    var defaultAgent: AgentKind {
        switch self {
        case .backend: return .codex
        case .frontend: return .antigravity
        default: return .claude
        }
    }

    /// Model saved for this role when none is configured yet; empty means the agent's default.
    var defaultModel: String { self == .plan ? "opus" : "" }
}

/// Who did one job on a task, recorded by the daemon.
struct AgentCredit: Codable, Hashable {
    let agent: String
    let model: String?
    let effort: String?
    let inputTokens: Int?
    let outputTokens: Int?
    let passes: Int?

    var kind: AgentKind? { AgentKind(rawValue: agent) }

    enum CodingKeys: String, CodingKey {
        case agent, model, effort, passes
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
    }
}

struct RoleCredit: Hashable {
    let role: AgentRole
    let credit: AgentCredit
}

/// One model an Antigravity plan offers, from `agy models`.
struct AntigravityModelOption: Identifiable, Hashable, Sendable {
    let id: String
    let displayName: String
}

enum ModelNames {
    static let claudeAliases: [(String, String)] = [("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable")]

    /// A readable model name: "claude-opus-5-5" → "Opus 5.5", "gemini-4-hydrogen-high" → "Gemini 4 Hydrogen (High)".
    static func pretty(_ model: String, agent: AgentKind?, catalog: [AntigravityModelOption] = [],
                       codex: [CodexModelOption] = []) -> String {
        if agent == .antigravity, let option = catalog.first(where: { $0.id == model }) { return option.displayName }
        if agent == .codex, let option = codex.first(where: { $0.model == model }) { return option.displayName }
        if let alias = claudeAliases.first(where: { $0.0 == model }) { return alias.1 }
        var parts = model.split(separator: "-").map(String.init)
        if parts.first == "claude" { parts.removeFirst() }
        var level: String?
        if agent == .antigravity, let last = parts.last, ["low", "medium", "high", "xhigh", "max"].contains(last) {
            level = ReasoningLevel.title(parts.removeLast())
        }
        var words: [String] = []
        for part in parts {
            if let previous = words.last, Int(part) != nil, previous.allSatisfy({ $0.isNumber || $0 == "." }) {
                words[words.count - 1] = previous + "." + part  // 5-5 → 5.5
            } else if part.lowercased() == "gpt" {
                words.append("GPT")
            } else {
                words.append(part.prefix(1).uppercased() + part.dropFirst())
            }
        }
        let name = words.joined(separator: " ")
        return level.map { "\(name) (\($0))" } ?? name
    }
}

enum AntigravityCatalog {
    /// Runs `agy models`: one "id<TAB>Display name" line per model the signed-in plan offers.
    static func load(executable: String) async throws -> [AntigravityModelOption] {
        try await Task.detached {
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: NSString(string: executable).expandingTildeInPath)
            process.arguments = ["models"]
            process.currentDirectoryURL = FileManager.default.temporaryDirectory
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = output
            process.standardError = output
            try process.run()
            let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
            DispatchQueue.global().asyncAfter(deadline: .now() + 30, execute: watchdog)
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            watchdog.cancel()
            let text = String(data: data, encoding: .utf8) ?? ""
            let models = text.split(separator: "\n").compactMap { line -> AntigravityModelOption? in
                let fields = line.split(separator: "\t", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                guard fields.count == 2, !fields[0].isEmpty, !fields[0].contains(" ") else { return nil }
                return AntigravityModelOption(id: fields[0], displayName: fields[1])
            }
            guard process.terminationStatus == 0, !models.isEmpty else {
                let reason = text.split(separator: "\n").last.map(String.init) ?? "no models listed"
                throw AgentClientError.server("Antigravity: \(reason)")
            }
            return models
        }.value
    }
}
