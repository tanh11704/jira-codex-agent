import Foundation

struct QuotaWindowInfo: Decodable {
    let used_percent: Int
    let window_minutes: Int?
    let resets_at: String?
    var remaining: Int { 100 - used_percent }
    var resetDate: Date? {
        guard let resets_at else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: resets_at) ?? ISO8601DateFormatter().date(from: resets_at)
    }
}

struct QuotaInfo: Decodable {
    let primary: QuotaWindowInfo?
    let secondary: QuotaWindowInfo?
    let ordinary_usage_allowed: Bool?
    let error: String?
    var fiveHour: QuotaWindowInfo? {
        [primary, secondary].compactMap { $0 }.first { $0.window_minutes == 300 }
    }
    var weekly: QuotaWindowInfo? {
        [primary, secondary].compactMap { $0 }.first { $0.window_minutes == 10080 }
    }
}

struct QuotaResponse: Decodable {
    let snapshot: QuotaInfo?
    let checkedAt: String?
    let fiveHourThreshold: Int
    let weeklyThreshold: Int
}

struct CodexApproval: Decodable, Identifiable {
    let id: String
    let kind: String
    let threadId: String?
    let command: String?
    let cwd: String
    let reason: String?
    let details: String
    let changes: String
    let scope: String
}

struct ApprovalsResponse: Decodable {
    let approvals: [CodexApproval]
}

struct TaskReportTest: Codable, Hashable {
    let command: String
    let result: String
}

/// Structured final report Codex returns through the turn output schema.
struct TaskReport: Codable, Hashable {
    let status: String
    let summary: String?
    let blockers: [String]?
    let tests: [TaskReportTest]?
    let branch: String?
    let report: String?
}

struct AgentTask: Codable, Identifiable, Hashable {
    let issueKey: String
    let summary: String
    let state: String
    let worktree: String?
    let sessionId: String?
    let error: String?
    let createdAt: String
    let updatedAt: String
    let description: String?
    let issueType: String?
    let priority: String?
    let blockedReason: String?
    let attempts: Int?
    let nextAttemptAt: String?
    let report: String?
    let retryNote: String?

    var id: String { issueKey }

    var parsedReport: TaskReport? {
        guard let report, let data = report.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(TaskReport.self, from: data)
    }

    var displayState: String {
        switch state {
        case "queued": return "Queued"
        case "blocked": return "Blocked"
        case "coding": return "Coding"
        case "review": return "Needs review"
        case "needs_human": return "Needs you"
        case "failed": return "Failed"
        case "interrupted": return nextAttemptAt == nil ? "Interrupted" : "Retry scheduled"
        case "resume_pending": return "Resume pending"
        case "skipped": return "Skipped"
        case "done": return "Reviewed"
        default: return state.capitalized
        }
    }

    var symbol: String {
        switch state {
        case "queued": return "clock"
        case "blocked": return "lock.fill"
        case "coding": return "hammer.fill"
        case "review": return "checkmark.circle.fill"
        case "needs_human": return "person.fill.questionmark"
        case "failed": return "exclamationmark.triangle.fill"
        case "interrupted": return "arrow.clockwise.circle"
        case "skipped": return "forward.end.circle"
        case "done": return "checkmark.seal"
        default: return "circle"
        }
    }

    var tintName: String {
        switch state {
        case "coding": return "blue"
        case "review": return "green"
        case "failed": return "red"
        case "needs_human": return "orange"
        case "blocked": return "purple"
        default: return "secondary"
        }
    }

    /// Discarding is allowed for anything that holds work or might be redone.
    var canDiscard: Bool { state != "excluded" }

    enum CodingKeys: String, CodingKey {
        case issueKey = "issue_key"
        case summary, state, worktree
        case sessionId = "session_id"
        case error, description, priority, attempts, report
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case issueType = "issue_type"
        case blockedReason = "blocked_reason"
        case nextAttemptAt = "next_attempt_at"
        case retryNote = "retry_note"
    }
}

struct StatusResponse: Decodable {
    let running: Bool
    let paused: Bool
    let current: AgentTask?
}

struct TasksResponse: Decodable {
    let tasks: [AgentTask]
}

struct PauseResponse: Decodable {
    let paused: Bool
}

struct DiscardResponse: Decodable {
    let deletedBranches: [String]
}

struct FetchJiraResponse: Decodable {
    let count: Int
}

struct TaskLogEvent: Decodable, Identifiable {
    let id: Int
    let timestamp: String
    let kind: String
    let message: String
}

struct TaskLogsResponse: Decodable {
    let events: [TaskLogEvent]
}

struct ErrorResponse: Decodable {
    let error: String
}

enum AgentConnection: Equatable {
    case connecting
    case online
    case offline(String)
}

enum TaskFilter: String, CaseIterable, Identifiable {
    case all = "All tasks"
    case queued = "Queue"
    case blocked = "Blocked"
    case coding = "In progress"
    case review = "Needs review"
    case needsHuman = "Needs you"
    case failed = "Failed"
    case interrupted = "Interrupted"
    case skipped = "Skipped"
    case done = "Reviewed"

    var id: String { rawValue }

    var states: Set<String> {
        switch self {
        case .all: return []
        case .queued: return ["queued", "resume_pending"]
        case .blocked: return ["blocked"]
        case .coding: return ["coding"]
        case .review: return ["review"]
        case .needsHuman: return ["needs_human"]
        case .failed: return ["failed"]
        case .interrupted: return ["interrupted"]
        case .skipped: return ["skipped"]
        case .done: return ["done"]
        }
    }

    func matches(_ task: AgentTask) -> Bool {
        self == .all || states.contains(task.state)
    }

    var symbol: String {
        switch self {
        case .all: return "tray.full"
        case .queued: return "clock"
        case .blocked: return "lock"
        case .coding: return "hammer"
        case .review: return "checkmark.circle"
        case .needsHuman: return "person.fill.questionmark"
        case .failed: return "exclamationmark.triangle"
        case .interrupted: return "pause.circle"
        case .skipped: return "forward.end.circle"
        case .done: return "checkmark.seal"
        }
    }
}
