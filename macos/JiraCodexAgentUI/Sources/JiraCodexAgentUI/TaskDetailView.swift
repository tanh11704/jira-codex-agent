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
        OpenTaskWindowItem(task: task)
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

private struct OpenTaskWindowItem: View {
    @Environment(\.openWindow) private var openWindow
    let task: AgentTask

    var body: some View {
        Button("Open in new window") { openWindow(id: "task", value: task.id) }
    }
}

/// A task's full detail in its own resizable window.
struct TaskWindow: View {
    @EnvironmentObject private var store: AgentStore
    let taskID: String?

    var body: some View {
        Group {
            if let task = store.tasks.first(where: { $0.id == taskID }) {
                TaskDetailView(task: task, showsWindowButton: false)
                    .padding(12)
                    .navigationTitle("\(task.issueKey) · \(task.summary)")
            } else {
                Text("This task no longer exists.").foregroundStyle(.secondary)
            }
        }
        .frame(minWidth: 480, idealWidth: 860, maxWidth: .infinity, minHeight: 420, idealHeight: 820, maxHeight: .infinity)
    }
}

struct TaskDetailView: View {
    @EnvironmentObject private var store: AgentStore
    @Environment(\.openWindow) private var openWindow
    let task: AgentTask
    /// False inside the task's own window, where opening another copy makes no sense.
    var showsWindowButton = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(task.issueKey).font(.headline.monospaced())
                        Spacer()
                        TaskStateBadge(task: task)
                        if showsWindowButton {
                            Button { openWindow(id: "task", value: task.id) } label: {
                                Image(systemName: "macwindow.badge.plus")
                            }
                            .buttonStyle(.borderless)
                            .help("Open in a separate, resizable window")
                        }
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
                if task.agentDecisionCount > 0 && !task.canAnswer {
                    callout("\(task.agentDecisionCount) decision\(task.agentDecisionCount == 1 ? "" : "s") made by the agent",
                            "Check them under Decisions so far before accepting this work. If one is wrong, retry the task with a note.",
                            symbol: "person.badge.shield.checkmark", color: .blue)
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
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack(spacing: 6) {
                                        if decision.isAgent {
                                            Label("Agent", systemImage: "sparkles")
                                                .font(.caption2.weight(.semibold))
                                                .padding(.horizontal, 6).padding(.vertical, 1)
                                                .background(Color.blue.opacity(0.15), in: Capsule())
                                                .foregroundStyle(.blue)
                                        }
                                        Text(item.question).font(.caption).foregroundStyle(.secondary)
                                    }
                                    Text(item.answer).font(.callout)
                                    if let rationale = item.rationale, !rationale.isEmpty {
                                        Text(rationale).font(.caption).foregroundStyle(.secondary)
                                    }
                                    if let assumptions = item.assumptions, !assumptions.isEmpty {
                                        Label(assumptions.joined(separator: "; "), systemImage: "questionmark.circle")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    if let acceptance = item.acceptance, !acceptance.isEmpty {
                                        Label(acceptance.joined(separator: "; "), systemImage: "checkmark.circle")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    if decision.isAgent {
                                        let meta = [item.confidenceLabel, item.riskLabel ?? ""]
                                            + (item.sources ?? [])
                                        Label(meta.filter { !$0.isEmpty }.joined(separator: " · "),
                                              systemImage: item.needsAttention ? "exclamationmark.triangle.fill" : "info.circle")
                                            .font(.caption2)
                                            .foregroundStyle(item.needsAttention ? Color.orange : Color.secondary)
                                    }
                                }
                            }
                            if let note = decision.note {
                                Label(note, systemImage: decision.isAgent ? "sparkles" : "text.bubble").font(.callout)
                            }
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
                        let images = report.images ?? []
                        if !images.isEmpty {
                            let toGenerate = images.filter { $0.source == "generate" }.count
                            let missingFiles = images.filter { $0.fileExists == false }.count
                            let undeclared = images.filter { $0.undeclared == true }.count
                            Text("Images: \(images.count) slot(s)"
                                 + (toGenerate > 0 ? ", \(toGenerate) to generate" : "")
                                 + (undeclared > 0 ? ", \(undeclared) not in report" : "")
                                 + (missingFiles > 0 ? ", \(missingFiles) missing file(s)" : ""))
                                .font(.callout.weight(.semibold))
                                .padding(.top, 4)
                            ForEach(images, id: \.self) { image in
                                ReportImageRow(image: image)
                            }
                        }
                        if let full = report.report, !full.isEmpty {
                            DisclosureGroup("Full handoff report") {
                                MarkdownView(full, fontSize: 12.5)
                                    .padding(.top, 6)
                            }
                            expandButton("Read report") { openReader(.report) }
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
                        expandButton("Expand") { openReader(.plan) }
                    }) {
                        DisclosureGroup("Implementation plan") {
                            MarkdownView(plan, fontSize: 12.5).padding(.top, 6)
                        }
                    }
                }
                section("Description", accessory: {
                    if let description = task.description, !description.isEmpty {
                        expandButton("Expand") { openReader(.description) }
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
    }

    private func openReader(_ section: ReaderRequest.Section) {
        openWindow(id: "reader", value: ReaderRequest(taskID: task.id, section: section))
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
    @State private var isDeciding = false

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
                if !questions.isEmpty {
                    Button(isDeciding ? "Agent deciding…" : "Let agent decide") {
                        Task {
                            isDeciding = true
                            if !(await store.decideTask(task)) { isDeciding = false }
                        }
                    }
                    .help("Claude answers these questions as product owner and resumes the task; you review its decisions at the end")
                    .disabled(isDeciding || isSending)
                }
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

/// An image slot from the report; prompts and target paths copy with one click for generating during review.
private struct ReportImageRow: View {
    let image: ReportImage
    @State private var copied: String?

    private var symbol: String {
        switch image.source {
        case "generate": return "wand.and.stars"
        case "web": return "globe"
        case "undeclared": return "questionmark.square.dashed"
        default: return "photo"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Label(image.placement, systemImage: symbol).font(.callout.weight(.medium))
                if image.undeclared == true {
                    Text("Not in report")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.orange)
                        .help("The change uses this image but the agent did not list it. Check it belongs, then supply the file.")
                }
                if let ratio = image.aspectRatio, !ratio.isEmpty {
                    Text(ratio).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                if let exists = image.fileExists {
                    Label(exists ? "File present" : "Missing file",
                          systemImage: exists ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(exists ? .green : .orange)
                        .help(exists ? "The image file exists in the worktree." :
                              "No file at this path yet. Generate it (or download the source) and save it here.")
                }
                Spacer(minLength: 8)
                if let path = image.resolvedPath.flatMap({ $0.isEmpty ? nil : $0 }) ?? image.path, !path.isEmpty {
                    copyButton("Copy path", path)
                }
                if let prompt = image.prompt, !prompt.isEmpty { copyButton("Copy prompt", prompt) }
            }
            if let purpose = image.purpose, !purpose.isEmpty {
                Text(purpose).font(.caption).foregroundStyle(.secondary)
            }
            if let path = image.resolvedPath.flatMap({ $0.isEmpty ? nil : $0 }) ?? image.path, !path.isEmpty {
                Text(path).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            if let prompt = image.prompt, !prompt.isEmpty {
                Text(prompt).font(.caption).padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
            }
            if let url = image.url, !url.isEmpty, let link = URL(string: url) {
                Link(destination: link) {
                    Text(url + ((image.license ?? "").isEmpty ? "" : " — \(image.license!)")).font(.caption).lineLimit(1)
                }
            }
        }
        .textSelection(.enabled)
        .padding(.vertical, 4)
    }

    private func copyButton(_ title: String, _ value: String) -> some View {
        Button(copied == title ? "Copied" : title) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
            copied = title
        }
        .buttonStyle(.borderless)
        .font(.caption)
    }
}
