import SwiftUI

/// A discard waiting for confirmation; shown as an alert by the dashboard.
struct PendingDiscard: Identifiable {
    let task: AgentTask
    let mode: String
    var id: String { task.id + mode }

    var title: String {
        switch mode {
        case "retry": return "Retry \(task.issueKey) from scratch?"
        case "skip": return "Discard and skip \(task.issueKey)?"
        default: return "Delete \(task.issueKey)?"
        }
    }

    var message: String {
        let loss = "The worktree (including uncommitted changes), the issue's local branch, logs and Codex thread are removed."
        switch mode {
        case "retry": return loss + " The task goes back to the queue. Optionally tell Codex what to do differently."
        case "skip": return loss + " The agent will not pick this issue up again until you unskip it."
        default: return loss + " If the issue still matches your JQL it will be queued again on the next sync; use Skip to prevent that."
        }
    }
}

struct TaskActionsMenu: View {
    let task: AgentTask

    var body: some View {
        Menu { TaskActionItems(task: task) } label: { Image(systemName: "ellipsis.circle") }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("Task actions")
    }
}

/// Menu items shared by the row's action menu and its context menu.
/// A separate view so SwiftUI injects the store wherever it is placed.
struct TaskActionItems: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask

    var body: some View {
        if task.state == "review" || task.state == "needs_human" {
            Button("Mark reviewed") { Task { await store.taskAction("review_done", task: task) } }
        }
        if ["interrupted", "failed", "needs_human"].contains(task.state) {
            Button("Resume in same worktree") { Task { await store.resumeTask(task) } }
        }
        if task.state == "skipped" {
            Button("Unskip") { Task { await store.taskAction("unskip", task: task) } }
        }
        if let url = store.jiraIssueURL(task) {
            Button("Open in Jira") { NSWorkspace.shared.open(url) }
        }
        if task.worktree != nil {
            Button("Copy worktree path") { store.copyPath(task) }
            Button("Reveal worktree in Finder") { store.revealWorktree(task) }
        }
        Divider()
        if task.canDiscard {
            Button("Retry from scratch…") { store.pendingDiscard = PendingDiscard(task: task, mode: "retry") }
            if task.state != "skipped" {
                Button("Discard and skip…") { store.pendingDiscard = PendingDiscard(task: task, mode: "skip") }
            }
        }
        Button("Clear logs") { Task { await store.taskAction("clear_logs", task: task) } }
        Button("Delete task…", role: .destructive) { store.pendingDiscard = PendingDiscard(task: task, mode: "delete") }
    }
}

struct TaskDetailView: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(task.issueKey).font(.headline.monospaced())
                        Spacer()
                        TaskStateBadge(task: task)
                    }
                    Text(task.summary).font(.title3.weight(.semibold)).textSelection(.enabled)
                    HStack(spacing: 10) {
                        if let type = task.issueType { Label(type, systemImage: "tag") }
                        if let priority = task.priority { Label(priority, systemImage: "flag") }
                        if let attempts = task.attempts, attempts > 0 { Label("Attempt \(attempts)", systemImage: "arrow.clockwise") }
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    if let url = store.jiraIssueURL(task) {
                        Link("Open in Jira", destination: url).font(.caption)
                    }
                }
                if let reason = task.blockedReason {
                    callout("Blocked in Jira", reason, symbol: "lock.fill", color: .purple)
                }
                if let error = task.error, !error.isEmpty {
                    callout(task.state == "needs_human" ? "Needs your decision" : "Last error", error,
                            symbol: "exclamationmark.triangle.fill", color: task.state == "needs_human" ? .orange : .red)
                }
                if let note = task.retryNote {
                    callout("Note for this attempt", note, symbol: "text.bubble", color: .blue)
                }
                if let report = task.parsedReport {
                    section("Codex report — \(report.status)") {
                        if let summary = report.summary, !summary.isEmpty { Text(summary) }
                        if let branch = report.branch, !branch.isEmpty {
                            Label(branch, systemImage: "arrow.triangle.branch").font(.caption.monospaced())
                        }
                        ForEach(report.tests ?? [], id: \.self) { test in
                            HStack(alignment: .top) {
                                Image(systemName: test.result == "PASS" ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundStyle(test.result == "PASS" ? .green : (test.result == "FAIL" ? .red : .orange))
                                Text(test.command).font(.caption.monospaced())
                            }
                        }
                        if let full = report.report, !full.isEmpty {
                            DisclosureGroup("Full handoff report") {
                                Text(full).font(.callout).textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                }
                section("Description") {
                    Text(task.description?.isEmpty == false ? task.description! : "No description in Jira.")
                        .font(.callout)
                        .foregroundStyle(task.description?.isEmpty == false ? .primary : .secondary)
                        .textSelection(.enabled)
                }
                if let worktree = task.worktree {
                    section("Worktree") {
                        Text(worktree).font(.caption.monospaced()).textSelection(.enabled)
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(AppTheme.panel, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.separator.opacity(0.45)))
    }

    private func callout(_ title: String, _ text: String, symbol: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(title, systemImage: symbol).font(.caption.weight(.semibold)).foregroundStyle(color)
            Text(text).font(.callout).textSelection(.enabled)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
            content()
        }
    }
}
