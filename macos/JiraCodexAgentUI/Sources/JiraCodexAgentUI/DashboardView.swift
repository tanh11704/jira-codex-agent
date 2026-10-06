import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var store: AgentStore

    var body: some View {
        HStack(spacing: 0) {
            sidebar
                .frame(width: 220)
            Divider()
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(minWidth: 920, minHeight: 600)
        .task { store.startRefreshing() }
        .onDisappear { store.stopRefreshing() }
        .sheet(isPresented: $store.isShowingSettings) {
            SettingsView().environmentObject(store)
        }
        .alert("Task recovery failed", isPresented: Binding(get: { store.taskActionError != nil }, set: { if !$0 { store.taskActionError = nil } })) {
            Button("OK") { store.taskActionError = nil }
        } message: { Text(store.taskActionError ?? "") }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                if let url = Bundle.main.url(forResource: "logo", withExtension: "png"),
                   let logo = NSImage(contentsOf: url) {
                    Image(nsImage: logo)
                        .resizable()
                        .scaledToFit()
                        .frame(width: 32, height: 32)
                } else {
                    Image(systemName: "cpu.fill")
                        .font(.title3)
                        .foregroundStyle(AppTheme.accent)
                }
                Text("Jira Codex")
                    .font(.title2.bold())
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.top, 18)
            .padding(.bottom, 12)

            List(TaskFilter.allCases, selection: $store.selectedFilter) { filter in
                Label(filter.rawValue, systemImage: filter.symbol)
                    .badge(count(for: filter))
                    .tag(filter)
            }
            .listStyle(.sidebar)

            Divider()

            VStack(spacing: 8) {
                Button {
                    store.isShowingSettings = true
                } label: {
                    Label("Accounts & Settings", systemImage: "gearshape")
                        .font(.system(size: 12, weight: .medium))
                        .lineLimit(1)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))

                Button {
                    Task { await store.fetchJiraNow() }
                } label: {
                    Label(store.isFetchingJira ? "Fetching Jira…" : "Fetch Jira now", systemImage: "arrow.triangle.2.circlepath")
                        .font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.plain)
                .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
                .disabled(store.connection != .online || store.isFetchingJira)
                if let message = store.fetchJiraMessage {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }

                Button {
                    Task { await store.refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                        .font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))

                Button {
                    Task { await store.togglePaused() }
                } label: {
                    Label(store.isPaused ? "Resume agent" : "Pause agent", systemImage: store.isPaused ? "play.fill" : "pause.fill")
                        .font(.system(size: 14, weight: .semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .tint(store.isPaused ? .green : AppTheme.accent)
                .disabled(store.connection != .online || store.isWorking)
                if store.connection == .online {
                    HStack {
                        Button("Stop") { Task { await store.stopDaemon() } }
                        Button("Restart") { Task { await store.stopDaemon(restart: true) } }
                    }
                    .buttonStyle(.bordered)
                    .disabled(store.isManagingDaemon)
                } else {
                    Button(store.isManagingDaemon ? "Starting…" : "Start daemon") { store.startDaemon() }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                        .disabled(store.isManagingDaemon)
                }
            }
            .padding(14)
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 20) {
            header
            metrics
            if case .offline(let message) = store.connection {
                offlineBanner(message)
            }
            taskList
        }
        .padding(24)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(store.selectedFilter.rawValue).font(.largeTitle.bold())
                Text(subtitle).foregroundStyle(.secondary)
            }
            Spacer()
            if let date = store.lastUpdated {
                Text("Updated \(date, style: .relative) ago")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private var metrics: some View {
        HStack(spacing: 12) {
            MetricCard(title: "Queued", value: "\(store.queuedCount)", symbol: "clock", color: .orange)
            MetricCard(title: "Active", value: store.currentTask == nil ? "0" : "1", symbol: "hammer.fill", color: .blue)
            MetricCard(title: "Review", value: "\(store.reviewCount)", symbol: "checkmark.circle.fill", color: .green)
            MetricCard(title: "Failed", value: "\(store.failedCount)", symbol: "exclamationmark.triangle.fill", color: .red)
        }
    }

    @ViewBuilder
    private var taskList: some View {
        if store.filteredTasks.isEmpty {
            EmptyState(filter: store.selectedFilter).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List(store.filteredTasks, selection: $store.selectedTaskID) { task in
                TaskRow(task: task)
                    .tag(task.id)
                    .contextMenu {
                        if task.worktree != nil {
                            Button("Reveal worktree in Finder") { store.revealWorktree(task) }
                        }
                    }
            }
            .listStyle(.inset(alternatesRowBackgrounds: true))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(.separator.opacity(0.45)))
        }
    }

    private func offlineBanner(_ message: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "bolt.horizontal.circle.fill").foregroundStyle(.red)
            VStack(alignment: .leading, spacing: 2) {
                Text("Daemon is offline").fontWeight(.semibold)
                Text(message).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Configure accounts") {
                store.isShowingSettings = true
            }
            Button("Start daemon") { store.startDaemon() }
        }
        .padding(14)
        .background(.red.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
    }

    private var subtitle: String {
        if let current = store.currentTask {
            return "Currently working on \(current.issueKey)"
        }
        return store.isPaused ? "Automation is paused" : "Agent is idle"
    }

    private func count(for filter: TaskFilter) -> Int {
        store.tasks.filter { filter.matches($0) }.count
    }
}

private struct TaskRow: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask
    @State private var confirmingDelete = false

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: task.symbol)
                .foregroundStyle(Color.taskTint(task.tintName))
                .font(.title3)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 8) {
                    Text(task.issueKey).font(.system(.body, design: .monospaced).weight(.semibold))
                    Text(task.summary).lineLimit(1)
                }
                if let error = task.error, !error.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.red).lineLimit(1)
                } else if let worktree = task.worktree {
                    Text(worktree).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            TaskStateBadge(task: task)
            if task.state == "review" {
                Button("Review done") { Task { await store.taskAction("review_done", task: task) } }
            }
            if task.state == "interrupted" || task.state == "failed" {
                Button("Resume task") { Task { await store.resumeTask(task) } }
            }
            if task.worktree != nil {
                Button { store.copyPath(task) } label: { Image(systemName: "doc.on.doc") }
                    .buttonStyle(.borderless)
                    .help("Copy worktree path")
                if task.state == "failed" || task.state == "done" {
                    Button { confirmingDelete = true } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless)
                        .help("Delete worktree")
                }
                Button { store.revealWorktree(task) } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.borderless)
                .help("Reveal worktree")
            }
        }
        .padding(.vertical, 7)
        .alert("Delete worktree for \(task.issueKey)?", isPresented: $confirmingDelete) {
            Button("Cancel", role: .cancel) { }
            Button("Delete", role: .destructive) { Task { await store.taskAction("delete_worktree", task: task) } }
        } message: {
            Text("All files in \(task.worktree ?? "") will be removed, including uncommitted changes. Commit or back up your work first. The Git branch and task history are kept.")
        }
    }
}
