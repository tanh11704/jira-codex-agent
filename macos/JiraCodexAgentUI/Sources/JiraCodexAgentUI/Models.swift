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

struct AgentTask: Codable, Identifiable, Hashable {
    let issueKey: String
    let summary: String
    let state: String
    let worktree: String?
    let sessionId: String?
    let result: String?
    let error: String?
    let createdAt: String
    let updatedAt: String
    let progress: String?

    var id: String { issueKey }

    var displayState: String {
        switch state {
        case "queued": return "Queued"
        case "coding": return "Coding"
        case "review": return "Needs review"
        case "failed": return "Failed"
        case "interrupted": return "Interrupted"
        case "resume_pending": return "Resume pending"
        case "done": return "Reviewed"
        default: return state.capitalized
        }
    }

    var symbol: String {
        switch state {
        case "queued": return "clock"
        case "coding": return "hammer.fill"
        case "review": return "checkmark.circle.fill"
        case "failed": return "exclamationmark.triangle.fill"
        default: return "circle"
        }
    }

    var tintName: String {
        switch state {
        case "coding": return "blue"
        case "review": return "green"
        case "failed": return "red"
        default: return "secondary"
        }
    }

    enum CodingKeys: String, CodingKey {
        case issueKey = "issue_key"
        case summary, state, worktree
        case sessionId = "session_id"
        case result, error, progress
        case createdAt = "created_at"
        case updatedAt = "updated_at"
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
    case coding = "In progress"
    case review = "Needs review"
    case failed = "Failed"
    case interrupted = "Interrupted"
    case done = "Reviewed"

    var id: String { rawValue }

    var states: Set<String> {
        switch self {
        case .all: return []
        case .queued: return ["queued", "resume_pending"]
        case .coding: return ["coding"]
        case .review: return ["review"]
        case .failed: return ["failed"]
        case .interrupted: return ["interrupted"]
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
        case .coding: return "hammer"
        case .review: return "checkmark.circle"
        case .failed: return "exclamationmark.triangle"
        case .interrupted: return "pause.circle"
        case .done: return "checkmark.seal"
        }
    }
}
