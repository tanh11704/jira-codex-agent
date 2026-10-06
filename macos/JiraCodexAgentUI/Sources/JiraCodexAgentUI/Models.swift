import Foundation

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
