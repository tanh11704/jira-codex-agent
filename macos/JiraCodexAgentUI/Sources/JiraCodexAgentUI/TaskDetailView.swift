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

private struct ReaderContent: Identifiable {
    let title: String
    let markdown: String
    var id: String { title }
}

struct TaskDetailView: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask
    @State private var reader: ReaderContent?

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
                        if let usage = task.parsedUsage {
                            Label("Claude \(usage.summary)", systemImage: "sparkles").help(usage.detail)
                        }
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
                if task.canAnswer {
                    section("Your decision") {
                        AnswerPanel(task: task).id(task.issueKey)
                    }
                }
                if !task.parsedDecisions.isEmpty || !task.parsedAllowedApprovals.isEmpty {
                    section("Decisions so far") {
                        ForEach(Array(task.parsedDecisions.enumerated()), id: \.offset) { _, decision in
                            ForEach(decision.answers, id: \.self) { item in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.question).font(.caption).foregroundStyle(.secondary)
                                    Text(item.answer).font(.callout)
                                }
                            }
                            if let note = decision.note { Label(note, systemImage: "text.bubble").font(.callout) }
                        }
                        ForEach(task.parsedAllowedApprovals, id: \.self) { approval in
                            Label(approval.title, systemImage: "checkmark.shield").font(.caption.monospaced())
                        }
                    }
                    .textSelection(.enabled)
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
                                MarkdownView(full, fontSize: 12.5)
                                    .padding(.top, 6)
                            }
                            expandButton("Read report") { reader = ReaderContent(title: "Codex report", markdown: full) }
                        }
                    }
                }
                if let review = task.parsedReview {
                    section("Claude review") {
                        let approved = review.verdict == "APPROVE"
                        Label(approved ? "Approved" : "Changes requested",
                              systemImage: approved ? "checkmark.seal.fill" : "exclamationmark.bubble.fill")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(approved ? .green : .orange)
                        if let summary = review.summary, !summary.isEmpty { Text(summary) }
                        ForEach(review.findings ?? [], id: \.self) { finding in
                            HStack(alignment: .top) {
                                Text(finding.severity.uppercased())
                                    .font(.caption2.weight(.bold))
                                    .foregroundStyle(["blocker", "major"].contains(finding.severity) ? .red : .secondary)
                                    .frame(width: 56, alignment: .leading)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(finding.location).font(.caption.monospaced()).foregroundStyle(.secondary)
                                    Text(finding.issue).font(.callout)
                                    Text(finding.fix).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .textSelection(.enabled)
                        }
                    }
                }
                if let plan = task.plan, !plan.isEmpty {
                    section("Claude plan", accessory: {
                        expandButton("Expand") { reader = ReaderContent(title: "Claude plan", markdown: plan) }
                    }) {
                        DisclosureGroup("Implementation plan") {
                            MarkdownView(plan, fontSize: 12.5).padding(.top, 6)
                        }
                    }
                }
                section("Description", accessory: {
                    if let description = task.description, !description.isEmpty {
                        expandButton("Expand") { reader = ReaderContent(title: "Description", markdown: description) }
                    }
                }) {
                    if let description = task.description, !description.isEmpty {
                        MarkdownView(description, fontSize: 13)
                            .padding(12)
                            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                            .overlay(RoundedRectangle(cornerRadius: 8).stroke(.separator.opacity(0.4)))
                    } else {
                        Text("No description in Jira.").font(.callout).foregroundStyle(.secondary)
                    }
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
        .sheet(item: $reader) { content in
            MarkdownReaderSheet(title: task.summary, subtitle: "\(task.issueKey) · \(content.title)",
                                markdown: content.markdown, link: store.jiraIssueURL(task))
        }
    }

    private func expandButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: "arrow.up.left.and.arrow.down.right").font(.caption)
        }
        .buttonStyle(.borderless)
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
        section(title, accessory: { EmptyView() }, content: content)
    }

    private func section<Accessory: View, Content: View>(_ title: String, @ViewBuilder accessory: () -> Accessory,
                                                          @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
                Spacer()
                accessory()
            }
            content()
        }
    }
}

/// Answers Codex's questions and allows declined requests, then resumes the same session and worktree.
struct AnswerPanel: View {
    @EnvironmentObject private var store: AgentStore
    let task: AgentTask
    @State private var answers: [String: String] = [:]
    @State private var note = ""
    @State private var allowed: Set<Int> = []
    @State private var isSending = false

    private var questions: [ReportQuestion] { task.parsedReport?.questions ?? [] }

    private var canSend: Bool {
        answers.values.contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            || !note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !allowed.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(questions.enumerated()), id: \.offset) { index, question in
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(index + 1). \(question.question)").font(.callout.weight(.semibold))
                    if let context = question.context, !context.isEmpty {
                        Text(context).font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(question.options ?? [], id: \.self) { option in
                        let selected = answers[question.id] == option
                        Button { answers[question.id] = option } label: {
                            Label(option, systemImage: selected ? "largecircle.fill.circle" : "circle")
                                .foregroundStyle(selected ? Color.accentColor : .primary)
                                .multilineTextAlignment(.leading)
                        }
                        .buttonStyle(.plain)
                    }
                    TextField((question.options ?? []).isEmpty ? "Your answer" : "Or write your own answer",
                              text: binding(for: question.id), axis: .vertical)
                        .lineLimit(1...4)
                }
            }
            let declined = task.parsedDeclinedApprovals
            if !declined.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Declined while nobody answered").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(Array(declined.enumerated()), id: \.offset) { index, approval in
                        Toggle(isOn: Binding(get: { allowed.contains(index) },
                                             set: { if $0 { allowed.insert(index) } else { allowed.remove(index) } })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(approval.title).font(.caption.monospaced())
                                if let reason = approval.reason, !reason.isEmpty {
                                    Text(reason).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .toggleStyle(.checkbox)
                    }
                    Text("Checked requests are approved automatically when Codex asks for them again in this task.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            TextField(questions.isEmpty ? "Tell Codex how to continue" : "Additional note (optional)",
                      text: $note, axis: .vertical)
                .lineLimit(1...4)
            HStack {
                Text("Resumes the same Codex session and worktree.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button(isSending ? "Sending…" : "Answer & resume") { Task { await send() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSend || isSending)
            }
        }
        .textFieldStyle(.roundedBorder)
    }

    private func binding(for id: String) -> Binding<String> {
        Binding(get: { answers[id] ?? "" }, set: { answers[id] = $0 })
    }

    private func send() async {
        isSending = true
        defer { isSending = false }
        let payload = questions.compactMap { question -> [String: String]? in
            guard let answer = answers[question.id], !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return ["id": question.id, "question": question.question, "answer": answer]
        }
        if await store.answerTask(task, answers: payload, note: note, allow: allowed.sorted()) {
            answers = [:]
            note = ""
            allowed = []
        }
    }
}
