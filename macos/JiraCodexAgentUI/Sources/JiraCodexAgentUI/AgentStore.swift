import AppKit
import Combine
import Foundation
import SwiftUI

@MainActor
final class AgentStore: ObservableObject {
    @Published private(set) var connection: AgentConnection = .connecting
    @Published private(set) var isPaused = false
    @Published private(set) var tasks: [AgentTask] = []
    @Published private(set) var lastUpdated: Date?
    @Published var selectedFilter: TaskFilter = .all
    @Published var selectedTaskID: AgentTask.ID?
    @Published var isWorking = false
    @Published var isShowingSettings = false

    @Published var jiraURL = ""
    @Published var jiraEmail = ""
    @Published var jiraToken = ""
    @Published var jiraJQL = "assignee = currentUser() AND statusCategory = 'To Do' ORDER BY priority DESC, created ASC"
    @Published var repositoryPath = ""
    @Published var baseBranch = "main"
    @Published var codexPath = "/opt/homebrew/bin/codex"
    @Published private(set) var codexStatus = "Checking…"
    @Published private(set) var configurationMessage: String?

    @AppStorage("socketPath") var socketPath = "~/.local/share/jira-codex-agent/agent.sock"

    private let client = AgentClient()
    private var refreshLoop: Task<Void, Never>?
    private var loginProcess: Process?

    private var configURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/jira-codex-agent/config.env")
    }

    init() {
        loadConfiguration()
        Task { await checkCodexLogin() }
    }

    var expandedSocketPath: String {
        NSString(string: socketPath).expandingTildeInPath
    }

    var currentTask: AgentTask? { tasks.first(where: { $0.state == "coding" }) }
    var reviewCount: Int { tasks.filter { $0.state == "review" }.count }
    var failedCount: Int { tasks.filter { $0.state == "failed" }.count }
    var queuedCount: Int { tasks.filter { $0.state == "queued" }.count }

    var filteredTasks: [AgentTask] {
        selectedFilter == .all ? tasks : tasks.filter { $0.state == selectedFilter.rawValue }
    }

    func startRefreshing() {
        guard refreshLoop == nil else { return }
        refreshLoop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    func stopRefreshing() {
        refreshLoop?.cancel()
        refreshLoop = nil
    }

    func refresh() async {
        do {
            async let status = client.request("status", socketPath: expandedSocketPath, as: StatusResponse.self)
            async let taskList = client.request("tasks", socketPath: expandedSocketPath, as: TasksResponse.self)
            let (newStatus, newTasks) = try await (status, taskList)
            isPaused = newStatus.paused
            tasks = newTasks.tasks
            connection = .online
            lastUpdated = Date()
        } catch {
            connection = .offline(error.localizedDescription)
            tasks = []
        }
    }

    func togglePaused() async {
        guard !isWorking else { return }
        isWorking = true
        defer { isWorking = false }
        do {
            let command = isPaused ? "resume" : "pause"
            let response = try await client.request(command, socketPath: expandedSocketPath, as: PauseResponse.self)
            isPaused = response.paused
            connection = .online
        } catch {
            connection = .offline(error.localizedDescription)
        }
    }

    func startDaemon() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-k", "gui/\(getuid())/com.jira-codex-agent"]
        try? process.run()
        Task {
            try? await Task.sleep(for: .seconds(1))
            await refresh()
        }
    }

    func saveConfiguration() {
        let values: [(String, String)] = [
            ("JCA_JIRA_URL", jiraURL),
            ("JCA_JIRA_EMAIL", jiraEmail),
            ("JCA_JIRA_API_TOKEN", jiraToken),
            ("JCA_JIRA_JQL", jiraJQL),
            ("JCA_REPOSITORY", repositoryPath),
            ("JCA_BASE_BRANCH", baseBranch),
            ("JCA_CODEX_COMMAND", codexPath),
            ("JCA_SOCKET_PATH", socketPath),
        ]
        let contents = values.map { "\($0.0)=\(dotenvQuoted($0.1))" }.joined(separator: "\n") + "\n"
        do {
            try FileManager.default.createDirectory(
                at: configURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try contents.write(to: configURL, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: configURL.path)
            configurationMessage = "Saved. Restart the daemon to apply changes."
        } catch {
            configurationMessage = "Could not save: \(error.localizedDescription)"
        }
    }

    func chooseRepository() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            repositoryPath = url.path
        }
    }

    func checkCodexLogin() async {
        let path = codexPath
        codexStatus = await Task.detached {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["login", "status"]
            process.standardOutput = pipe
            process.standardError = pipe
            do {
                try process.run()
                process.waitUntilExit()
                let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                if process.terminationStatus == 0 {
                    return output.contains("ChatGPT") ? "Connected with ChatGPT" : "Connected"
                }
                return "Not connected"
            } catch {
                return "Codex CLI not found"
            }
        }.value
    }

    func loginCodex() {
        guard loginProcess == nil else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: codexPath)
        process.arguments = ["login"]
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                self?.loginProcess = nil
                await self?.checkCodexLogin()
            }
        }
        do {
            try process.run()
            loginProcess = process
            codexStatus = "Complete sign-in in your browser…"
        } catch {
            codexStatus = "Could not start Codex: \(error.localizedDescription)"
        }
    }

    func revealWorktree(_ task: AgentTask) {
        guard let worktree = task.worktree else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: worktree)])
    }

    private func loadConfiguration() {
        guard let contents = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        var values: [String: String] = [:]
        for line in contents.split(separator: "\n", omittingEmptySubsequences: false) {
            guard !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<separator])
            var value = String(line[line.index(after: separator)...])
            if value.hasPrefix("\"") && value.hasSuffix("\"") && value.count >= 2 {
                value.removeFirst(); value.removeLast()
                value = value.replacingOccurrences(of: "\\\"", with: "\"")
                    .replacingOccurrences(of: "\\\\", with: "\\")
            }
            values[key] = value
        }
        jiraURL = values["JCA_JIRA_URL"] ?? jiraURL
        jiraEmail = values["JCA_JIRA_EMAIL"] ?? jiraEmail
        jiraToken = values["JCA_JIRA_API_TOKEN"] ?? jiraToken
        jiraJQL = values["JCA_JIRA_JQL"] ?? jiraJQL
        repositoryPath = values["JCA_REPOSITORY"] ?? repositoryPath
        baseBranch = values["JCA_BASE_BRANCH"] ?? baseBranch
        codexPath = values["JCA_CODEX_COMMAND"] ?? codexPath
        socketPath = values["JCA_SOCKET_PATH"] ?? socketPath
    }

    private func dotenvQuoted(_ value: String) -> String {
        "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}
