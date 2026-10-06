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
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "cpu.fill")
                    .font(.title3)
                    .foregroundStyle(AppTheme.accent)
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
                    Label("Accounts & Settings", systemImage: "person.crop.circle.badge.gearshape")
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)

                Button {
                    Task { await store.refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)

                Button {
                    Task { await store.togglePaused() }
                } label: {
                    Label(store.isPaused ? "Resume agent" : "Pause agent", systemImage: store.isPaused ? "play.fill" : "pause.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(store.isPaused ? .green : AppTheme.accent)
                .disabled(store.connection != .online || store.isWorking)
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
        filter == .all ? store.tasks.count : store.tasks.filter { $0.state == filter.rawValue }.count
    }
}

private struct TaskRow: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask

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
            if task.worktree != nil {
                Button { store.revealWorktree(task) } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.borderless)
                .help("Reveal worktree")
            }
        }
        .padding(.vertical, 7)
    }
}
