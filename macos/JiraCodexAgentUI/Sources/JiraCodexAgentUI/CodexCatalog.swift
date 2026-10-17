import Foundation

struct CodexReasoningEffort: Decodable, Hashable, Sendable {
    let reasoningEffort: String
    let description: String?
}

struct CodexServiceTier: Decodable, Hashable, Sendable {
    let id: String
    let name: String
    let description: String?
}

struct CodexModelOption: Decodable, Identifiable, Sendable {
    let id: String
    let model: String
    let displayName: String
    let isDefault: Bool
    // Optional so older Codex CLIs without these fields still decode.
    let description: String?
    let hidden: Bool?
    let supportedReasoningEfforts: [CodexReasoningEffort]?
    let defaultReasoningEffort: String?
    let serviceTiers: [CodexServiceTier]?

    var efforts: [CodexReasoningEffort] { supportedReasoningEfforts ?? [] }
    var tiers: [CodexServiceTier] { serviceTiers ?? [] }
}

enum ReasoningLevel {
    static func title(_ effort: String) -> String {
        switch effort {
        case "none": return "None"
        case "minimal": return "Minimal"
        case "low": return "Low"
        case "medium": return "Medium"
        case "high": return "High"
        case "xhigh": return "Extra high"
        case "max": return "Max"
        case "ultra": return "Ultra"
        default: return effort.prefix(1).uppercased() + effort.dropFirst()
        }
    }

    static func symbol(_ effort: String) -> String {
        switch effort {
        case "none", "minimal", "low": return "hare"
        case "medium": return "gauge.medium"
        case "high", "xhigh": return "brain"
        case "max", "ultra": return "brain.head.profile"
        default: return "brain"
        }
    }

    /// Levels that noticeably increase quota usage and run time.
    static func isExpensive(_ effort: String) -> Bool {
        ["xhigh", "max", "ultra"].contains(effort)
    }
}

private struct ModelPage: Decodable {
    let data: [CodexModelOption]
    let nextCursor: String?
}

enum CodexCatalog {
    static func load(executable: String) async throws -> [CodexModelOption] {
        try await Task.detached {
            let process = Process()
            let input = Pipe()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = ["app-server", "--listen", "stdio://"]
            process.standardInput = input
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            try process.run()
            let watchdog = DispatchWorkItem { if process.isRunning { process.terminate() } }
            DispatchQueue.global().asyncAfter(deadline: .now() + 20, execute: watchdog)
            defer {
                watchdog.cancel()
                if process.isRunning { process.terminate() }
            }
            func send(_ message: [String: Any]) throws {
                let bytes = try JSONSerialization.data(withJSONObject: message) + Data([10])
                try input.fileHandleForWriting.write(contentsOf: bytes)
            }
            func response(_ id: Int) throws -> Data {
                while true {
                    var line = Data()
                    while true {
                        guard let byte = try output.fileHandleForReading.read(upToCount: 1), !byte.isEmpty else {
                            throw AgentClientError.server("Codex model catalog unavailable or timed out.")
                        }
                        if byte[0] == 10 { break }
                        line.append(byte)
                    }
                    guard let message = try JSONSerialization.jsonObject(with: line) as? [String: Any],
                          message["id"] as? Int == id else { continue }
                    if let error = message["error"] as? [String: Any] {
                        throw AgentClientError.server(error["message"] as? String ?? "Codex request failed")
                    }
                    return try JSONSerialization.data(withJSONObject: message["result"] ?? [:])
                }
            }
            try send(["id": 1, "method": "initialize", "params": ["clientInfo": ["name": "jira-codex-agent", "version": "0.1.0"]]])
            _ = try response(1)
            try send(["method": "initialized"])
            var models: [CodexModelOption] = []
            var cursor: String?
            var seen = Set<String>()
            var id = 2
            repeat {
                var params: [String: Any] = ["limit": 100]
                if let cursor { params["cursor"] = cursor }
                try send(["id": id, "method": "model/list", "params": params])
                let page = try JSONDecoder().decode(ModelPage.self, from: response(id))
                models.append(contentsOf: page.data.filter { $0.hidden != true })
                cursor = page.nextCursor
                if let cursor, !seen.insert(cursor).inserted { throw AgentClientError.invalidResponse }
                id += 1
            } while cursor != nil
            return models
        }.value
    }
}
