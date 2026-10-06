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
    @Published private(set) var isManagingDaemon = false
    @AppStorage("daemonProjectDirectory") var daemonProjectDirectory = ""
    @AppStorage("daemonPythonPath") var daemonPythonPath = ""
    private var daemonProcess: Process?
    @Published var taskActionError: String?
    @Published private(set) var logEvents: [TaskLogEvent] = []
    @Published private(set) var logError: String?
    private var logTaskID: String?
    private var loadingLogs = false
    @Published private(set) var quota: QuotaResponse?
    @Published private(set) var quotaError: String?
    @Published private(set) var quotaUpdatedAt: Date?
    @Published private(set) var isLoadingQuota = false

    func refreshQuota() async {
        guard !isLoadingQuota else { return }
        isLoadingQuota = true
        defer { isLoadingQuota = false }
        do {
            let response = try await client.request("quota", socketPath: expandedSocketPath, as: QuotaResponse.self)
            quota = response
            quotaError = response.snapshot.error
            quotaUpdatedAt = Date()
        } catch {
            quota = nil
            quotaError = error.localizedDescription
            quotaUpdatedAt = nil
        }
    }
    @Published private(set) var approvals: [CodexApproval] = []
    @Published private(set) var answeringApprovals: Set<String> = []

    func refreshApprovals() async {
        do {
            let response = try await client.request("approvals", socketPath: expandedSocketPath, as: ApprovalsResponse.self)
            approvals = response.approvals
        } catch {
            approvals = []
        }
    }

    func answerApproval(_ approval: CodexApproval, accept: Bool) async {
        guard !answeringApprovals.contains(approval.id) else { return }
        answeringApprovals.insert(approval.id)
        defer { answeringApprovals.remove(approval.id) }
        do {
            _ = try await client.request("approval:\(approval.id):\(accept ? "accept" : "decline")", socketPath: expandedSocketPath, as: PauseResponse.self)
            await refreshApprovals()
        } catch { taskActionError = error.localizedDescription }
    }

    func refreshLogs() async {
        guard !loadingLogs else { return }
        guard let key = selectedTaskID ?? currentTask?.issueKey else {
            logEvents = []; logTaskID = nil
            return
        }
        loadingLogs = true
        defer { loadingLogs = false }
        if logTaskID != key { logEvents = []; logTaskID = key }
        do {
            let response = try await client.request("logs:\(key):\(logEvents.last?.id ?? 0)", socketPath: expandedSocketPath, as: TaskLogsResponse.self)
            guard key == (selectedTaskID ?? currentTask?.issueKey) else { return }
            logEvents.append(contentsOf: response.events)
            if logEvents.count > 2000 { logEvents.removeFirst(logEvents.count - 2000) }
            logError = nil
        } catch { logError = error.localizedDescription }
    }
    @Published private(set) var isFetchingJira = false
    @Published private(set) var fetchJiraMessage: String?

    func fetchJiraNow() async {
        guard !isFetchingJira else { return }
        isFetchingJira = true
        defer { isFetchingJira = false }
        do {
            let result = try await client.request("fetch_jira", socketPath: expandedSocketPath, as: FetchJiraResponse.self)
            fetchJiraMessage = "Synced \(result.count) Jira tasks"
            await refresh()
        } catch {
            fetchJiraMessage = nil
            taskActionError = "Jira sync failed: \(error.localizedDescription)"
        }
    }

    func copyPath(_ task: AgentTask) {
        guard let path = task.worktree else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(path, forType: .string)
    }

    func taskAction(_ action: String, task: AgentTask) async {
        do {
            _ = try await client.request(action + ":" + task.issueKey, socketPath: expandedSocketPath, as: PauseResponse.self)
            await refresh()
        } catch { taskActionError = error.localizedDescription }
    }

    func resumeTask(_ task: AgentTask) async {
        do {
            _ = try await client.request("resume_task:" + task.issueKey, socketPath: expandedSocketPath, as: PauseResponse.self)
            taskActionError = nil
            await refresh()
        } catch {
            taskActionError = error.localizedDescription
        }
    }

    @Published var jiraURL = ""
    @Published var jiraEmail = ""
    @Published var jiraToken = ""
    @Published var jiraJQL = "assignee = currentUser() AND statusCategory = 'To Do' ORDER BY priority DESC, created ASC"
    @Published var repositoryPath = ""
    @Published var baseBranch = "main"
    @Published var codexPath = "/opt/homebrew/bin/codex"
    @Published var codexModel = ""
    @Published private(set) var availableModels: [CodexModelOption] = []
    @Published private(set) var modelCatalogError: String?
    @Published private(set) var isLoadingModels = false
    @Published var quotaFiveHourThreshold = 30
    @Published var quotaWeeklyThreshold = 30
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
        if daemonProjectDirectory.isEmpty {
            daemonProjectDirectory = Bundle.main.object(forInfoDictionaryKey: "AgentProjectDirectory") as? String ?? ""
        }
        if daemonPythonPath.isEmpty && !daemonProjectDirectory.isEmpty {
            daemonPythonPath = daemonProjectDirectory + "/.venv/bin/python"
        }
        loadConfiguration()
        Task { await checkCodexLogin() }
    }

    var expandedSocketPath: String {
        NSString(string: socketPath).expandingTildeInPath
    }

    var currentTask: AgentTask? { tasks.first(where: { $0.state == "coding" }) }
    var reviewCount: Int { tasks.filter { $0.state == "review" }.count }
    var failedCount: Int { tasks.filter { $0.state == "failed" }.count }
    var queuedCount: Int { tasks.filter { $0.state == "queued" || $0.state == "resume_pending" }.count }

    var filteredTasks: [AgentTask] {
        tasks.filter { selectedFilter.matches($0) }
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
        guard !isManagingDaemon else { return }
        Task { await launchDaemon() }
    }

    private func launchDaemon() async {
        isManagingDaemon = true
        defer { isManagingDaemon = false }
        await refresh()
        if connection == .online { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: NSString(string: daemonPythonPath).expandingTildeInPath)
        process.currentDirectoryURL = URL(fileURLWithPath: NSString(string: daemonProjectDirectory).expandingTildeInPath)
        process.arguments = ["-m", "jira_codex_agent.main"]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/jira-codex-agent/startup.log")
        do {
            try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            let log = try FileHandle(forWritingTo: logURL)
            try log.seekToEnd()
            process.standardOutput = log
            process.standardError = log
            try process.run()
            daemonProcess = process
            try? log.close()
            for _ in 0..<30 {
                try? await Task.sleep(for: .milliseconds(300))
                await refresh()
                if connection == .online { return }
                if !process.isRunning { break }
            }
            taskActionError = "Daemon did not start. Check Python/project paths in Advanced and startup.log in Library/Logs/jira-codex-agent."
        } catch {
            taskActionError = "Could not start daemon: \(error.localizedDescription)"
        }
    }

    func stopDaemon(restart: Bool = false) async {
        guard !isManagingDaemon else { return }
        isManagingDaemon = true
        do {
            _ = try await client.request("stop", socketPath: expandedSocketPath, as: PauseResponse.self)
            for _ in 0..<60 {
                try? await Task.sleep(for: .milliseconds(300))
                if !FileManager.default.fileExists(atPath: expandedSocketPath) { break }
            }
            if FileManager.default.fileExists(atPath: expandedSocketPath) {
                throw AgentClientError.server("Daemon is still stopping; restart was deferred.")
            }
            await refresh()
            isManagingDaemon = false
            if restart { await launchDaemon() }
        } catch {
            taskActionError = "Could not stop daemon: \(error.localizedDescription). An older daemon may need one final restart from Terminal."
        }
        isManagingDaemon = false
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
            ("JCA_CODEX_MODEL", codexModel),
            ("JCA_SOCKET_PATH", socketPath),
            ("JCA_QUOTA_REMAINING_THRESHOLD", String(quotaFiveHourThreshold)),
            ("JCA_QUOTA_WEEKLY_REMAINING_THRESHOLD", String(quotaWeeklyThreshold)),
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
        codexModel = values["JCA_CODEX_MODEL"] ?? codexModel
        socketPath = values["JCA_SOCKET_PATH"] ?? socketPath
        quotaFiveHourThreshold = min(100, max(0, Int(values["JCA_QUOTA_REMAINING_THRESHOLD"] ?? "30") ?? 30))
        quotaWeeklyThreshold = min(100, max(0, Int(values["JCA_QUOTA_WEEKLY_REMAINING_THRESHOLD"] ?? "30") ?? 30))
    }

    private func dotenvQuoted(_ value: String) -> String {
        "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    func reloadModels() async {
        guard !isLoadingModels else { return }
        isLoadingModels = true
        defer { isLoadingModels = false }
        do {
            availableModels = try await CodexCatalog.load(executable: codexPath)
            modelCatalogError = nil
        } catch {
            modelCatalogError = error.localizedDescription
        }
    }
}
