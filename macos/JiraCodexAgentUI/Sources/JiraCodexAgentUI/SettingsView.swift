import SwiftUI

private enum SettingsTab: String, CaseIterable, Identifiable {
    case jira = "Jira"
    case codex = "Codex"
    case claude = "Claude"
    case automation = "Automation"
    case prompt = "Prompt"
    case advanced = "Advanced"

    var id: String { rawValue }
}

struct SettingsView: View {
    @EnvironmentObject private var store: AgentStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedTab = SettingsTab.jira

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Accounts & Settings").font(.title2.bold())
                        Text("Manage connections and automation preferences")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 12, weight: .semibold))
                            .frame(width: 28, height: 28)
                    }
                    .buttonStyle(.plain)
                    .background(.primary.opacity(0.08), in: Circle())
                    .help("Close Settings")
                    .keyboardShortcut(.cancelAction)
                }
                Picker("Settings section", selection: $selectedTab) {
                    ForEach(SettingsTab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }
            .padding(24)
            Divider()
            switch selectedTab {
            case .jira: jiraTab
            case .codex: codexTab
            case .claude: claudeTab
            case .automation: automationTab
            case .prompt: promptTab
            case .advanced: advancedTab
            }
            Divider()
            footer
        }
        .frame(width: 720, height: 680)
    }

    // MARK: Tabs

    private var jiraTab: some View {
        Form {
            Section("Jira Cloud") {
                TextField("Jira URL", text: $store.jiraURL, prompt: Text("https://company.atlassian.net"))
                TextField("Email", text: $store.jiraEmail)
                SecureField("API token", text: $store.jiraToken)
                TextField("JQL", text: $store.jiraJQL, axis: .vertical)
                    .lineLimit(2...4)
            }
            Section("Repository") {
                HStack {
                    TextField("Repository path", text: $store.repositoryPath)
                    Button("Choose…") { store.chooseRepository() }
                }
                TextField("Base branch", text: $store.baseBranch)
            }
        }
        .formStyle(.grouped)
    }

    private var codexTab: some View {
        Form {
            Section {
                HStack(spacing: 12) {
                    Image(systemName: "sparkles")
                        .font(.title3)
                        .foregroundStyle(AppTheme.accent)
                        .frame(width: 36, height: 36)
                        .background(AppTheme.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: 9))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("New tasks run with").font(.caption).foregroundStyle(.secondary)
                        Text(store.codexRunSummary).font(.headline)
                    }
                    Spacer()
                }
                .padding(.vertical, 2)
            }

            Section {
                ModelRow(title: "Codex default",
                         subtitle: "Use the model from your Codex CLI configuration.",
                         identifier: nil, isDefault: false,
                         isSelected: store.codexModel.isEmpty) { store.selectModel("") }
                ForEach(store.availableModels) { option in
                    ModelRow(title: option.displayName, subtitle: option.description,
                             identifier: option.model, isDefault: option.isDefault,
                             isSelected: store.codexModel == option.model) { store.selectModel(option.model) }
                }
                if !store.codexModel.isEmpty && !store.availableModels.contains(where: { $0.model == store.codexModel }) {
                    ModelRow(title: store.codexModel,
                             subtitle: store.availableModels.isEmpty ? "Saved model. Refresh to load the catalog." : "Not in the current Codex catalog.",
                             identifier: nil, isDefault: false, isSelected: true) {}
                }
            } header: {
                HStack {
                    Text("Model")
                    Spacer()
                    if store.isLoadingModels {
                        ProgressView().controlSize(.small)
                    } else {
                        Button { Task { await store.reloadModels() } } label: {
                            Label("Refresh", systemImage: "arrow.clockwise")
                        }
                        .buttonStyle(.borderless)
                        .help("Reload the model catalog from Codex")
                    }
                }
            } footer: {
                if let error = store.modelCatalogError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(.red)
                }
            }

            reasoningSection
            speedSection

            Section("Codex account") {
                HStack {
                    Image(systemName: isCodexConnected ? "checkmark.circle.fill" : "person.crop.circle.badge.exclamationmark")
                        .foregroundStyle(isCodexConnected ? .green : .orange)
                    Text(store.codexStatus)
                    Spacer()
                    Button("Check") { Task { await store.checkCodexLogin() } }
                    if !isCodexConnected {
                        Button("Sign in with ChatGPT") { store.loginCodex() }
                            .buttonStyle(.borderedProminent)
                    }
                }
                TextField("Codex executable", text: $store.codexPath)
            }
        }
        .formStyle(.grouped)
        .task { if store.availableModels.isEmpty { await store.reloadModels() } }
    }

    @ViewBuilder
    private var reasoningSection: some View {
        let option = store.selectedModelOption
        let efforts = option?.efforts ?? []
        Section {
            if efforts.isEmpty {
                Text(store.codexReasoningEffort.isEmpty
                     ? "Uses the model's default reasoning. Refresh the catalog to choose a level."
                     : "Saved level: \(ReasoningLevel.title(store.codexReasoningEffort)). Refresh the catalog to change it.")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 6) {
                    LevelChip(title: "Auto", symbol: "wand.and.stars",
                              isSelected: store.codexReasoningEffort.isEmpty) { store.codexReasoningEffort = "" }
                    ForEach(efforts, id: \.reasoningEffort) { effort in
                        LevelChip(title: ReasoningLevel.title(effort.reasoningEffort),
                                  symbol: ReasoningLevel.symbol(effort.reasoningEffort),
                                  isSelected: store.codexReasoningEffort == effort.reasoningEffort) {
                            store.codexReasoningEffort = effort.reasoningEffort
                        }
                        .help(effort.description ?? "")
                    }
                }
                Text(effortDescription(option: option, efforts: efforts))
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if ReasoningLevel.isExpensive(store.codexReasoningEffort) {
                    Label("Uses noticeably more quota and time. Best for the hardest issues; quota limits still pause the queue.",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
        } header: {
            Text("Reasoning effort")
        } footer: {
            Text("Higher levels think longer before acting. Applies to new and resumed tasks after the daemon restarts.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var speedSection: some View {
        if let tiers = store.selectedModelOption?.tiers, !tiers.isEmpty {
            Section {
                Picker("Speed", selection: $store.codexServiceTier) {
                    Text("Standard").tag("")
                    ForEach(tiers, id: \.id) { Text($0.name).tag($0.id) }
                }
                .pickerStyle(.segmented)
                Text(tiers.first(where: { $0.id == store.codexServiceTier })?.description ?? "Normal speed and usage.")
                    .font(.callout).foregroundStyle(.secondary)
            } header: {
                Text("Speed")
            }
        }
    }

    private var claudeTab: some View {
        let enabled = store.claudePlan || store.claudeReview
        return Form {
            Section {
                Toggle("Plan with Claude before Codex codes", isOn: $store.claudePlan)
                Toggle("Review Codex's changes with Claude", isOn: $store.claudeReview)
                Text("Claude Code runs read-only in the task worktree. The plan is added to the Codex prompt. When Codex reports COMPLETED, Claude reviews the diff; blocker or major findings go back to Codex, and if they remain the task moves to Needs you. If Claude is unavailable, Codex continues alone.")
                    .font(.caption).foregroundStyle(.secondary)
            } header: {
                Text("Claude passes")
            }
            Section("Review") {
                Stepper(store.claudeReviewRounds == 0 ? "Codex fix rounds: none (report only)" : "Codex fix rounds: \(store.claudeReviewRounds)",
                        value: $store.claudeReviewRounds, in: 0...5)
            }
            .disabled(!store.claudeReview)
            Section {
                Stepper("5 hours: \(store.claudeQuotaFiveHourThreshold)%", value: $store.claudeQuotaFiveHourThreshold, in: 0...100)
                Stepper("7 days: \(store.claudeQuotaWeeklyThreshold)%", value: $store.claudeQuotaWeeklyThreshold, in: 0...100)
                Stepper(store.claudeMaxTurns == 0 ? "Turns per pass: no limit" : "Turns per pass: \(store.claudeMaxTurns)",
                        value: $store.claudeMaxTurns, in: 0...200, step: 10)
            } header: {
                Text("Skip Claude when remaining quota reaches")
            } footer: {
                Text("Read from your plan's /usage before every pass, so the rest stays for your own Claude work. A skipped pass never stops the queue: Codex codes without the plan, or the task goes to review without Claude's review.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .disabled(!enabled)
            Section("Model") {
                Picker("Model", selection: $store.claudeModel) {
                    Text("CLI default").tag("")
                    Text("Opus").tag("opus")
                    Text("Sonnet").tag("sonnet")
                    Text("Fable").tag("fable")
                    if !["", "opus", "sonnet", "fable"].contains(store.claudeModel) {
                        Text(store.claudeModel).tag(store.claudeModel)
                    }
                }
                Picker("Effort", selection: $store.claudeEffort) {
                    Text("Default").tag("")
                    ForEach(["low", "medium", "high", "xhigh", "max"], id: \.self) { Text($0.capitalized).tag($0) }
                }
            }
            .disabled(!enabled)
            Section("Claude account") {
                HStack {
                    Image(systemName: store.claudeStatus.hasPrefix("Connected") ? "checkmark.circle.fill" : "person.crop.circle.badge.exclamationmark")
                        .foregroundStyle(store.claudeStatus.hasPrefix("Connected") ? .green : .orange)
                    Text(store.claudeStatus)
                    Spacer()
                    Button("Check") { Task { await store.checkClaudeLogin() } }
                }
                TextField("Claude executable", text: $store.claudePath)
            }
        }
        .formStyle(.grouped)
        .task { await store.checkClaudeLogin() }
    }

    private var automationTab: some View {
        Form {
            Section("Approvals") {
                Picker("Approval mode", selection: $store.codexApprovalsReviewer) {
                    Text("Ask for approval — you review").tag("user")
                    Text("Approve for me — Codex reviews").tag("auto_review")
                }
                Text(store.codexApprovalsReviewer == "auto_review"
                     ? "Codex evaluates eligible approval requests automatically. The workspace sandbox stays enabled; some requests may still need you. Auto-review can make mistakes and adds processing."
                     : "Approval requests appear in the dashboard for you to approve or decline.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Unattended operation") {
                Stepper(store.approvalTimeoutMinutes == 0 ? "Approval timeout: wait forever" : "Decline unanswered approvals after \(store.approvalTimeoutMinutes) min",
                        value: $store.approvalTimeoutMinutes, in: 0...240, step: 5)
                Stepper("Attempts per task: \(store.maxTaskAttempts)", value: $store.maxTaskAttempts, in: 1...10)
                Stepper("Keep finished-task history: \(store.historyRetentionDays) days", value: $store.historyRetentionDays, in: 1...365)
                Toggle("Keep the Mac awake while working", isOn: $store.preventSleep)
                Toggle("Notify when a task finishes, fails or needs you", isOn: $store.notificationsEnabled)
                Text("Failed or timed-out runs retry with back-off and the queue moves on. Issues blocked in Jira wait until their blocker is done. Codex reports that are not COMPLETED go to Needs you. Closing the lid on battery still sleeps the Mac.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Pause when remaining quota reaches") {
                Stepper("5 hours: \(store.quotaFiveHourThreshold)%", value: $store.quotaFiveHourThreshold, in: 0...100)
                Stepper("7 days: \(store.quotaWeeklyThreshold)%", value: $store.quotaWeeklyThreshold, in: 0...100)
                Text("Either limit pauses new tasks.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private var promptTab: some View {
        Form {
            Section {
                TextEditor(text: $store.taskPrompt)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 320)
                    .accessibilityLabel("Task prompt instructions")
            } header: {
                HStack {
                    Text("Task prompt")
                    Spacer()
                    Button("Restore default") { store.taskPrompt = AgentStore.defaultTaskPrompt }
                        .buttonStyle(.borderless)
                        .disabled(store.taskPrompt == AgentStore.defaultTaskPrompt)
                }
            } footer: {
                Text("Applied to every task; the Jira key, title and description are added automatically. Applies to new and resumed tasks, not a task already running. An empty prompt uses the default.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private var advancedTab: some View {
        Form {
            Section("Daemon connection") {
                TextField("Agent project directory", text: $store.daemonProjectDirectory)
                TextField("Python executable", text: $store.daemonPythonPath)
                TextField("Unix socket", text: $store.socketPath)
                Text("The UI and daemon must use the same socket path.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Reconnect") { Task { await store.refresh() } }
        }
        .formStyle(.grouped)
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 10) {
            if store.hasUnsavedChanges {
                Label("Unsaved changes", systemImage: "circle.fill")
                    .font(.caption).foregroundStyle(.orange)
                    .labelStyle(DotLabelStyle())
            } else if let message = store.configurationMessage {
                Label(message, systemImage: "checkmark.circle.fill")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if store.connection == .online {
                Button("Save") { store.saveConfiguration() }
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(!store.hasUnsavedChanges)
                Button(store.isManagingDaemon ? "Restarting…" : "Save & Restart Daemon") {
                    Task { await store.saveAndRestartDaemon() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!store.hasUnsavedChanges || store.isManagingDaemon)
                .help("Save, then restart the daemon so new settings apply to the next task")
            } else {
                Button("Save configuration") { store.saveConfiguration() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(!store.hasUnsavedChanges)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(.bar)
    }

    // MARK: Helpers

    private var isCodexConnected: Bool { store.codexStatus.contains("Connected") }

    private func effortDescription(option: CodexModelOption?, efforts: [CodexReasoningEffort]) -> String {
        if store.codexReasoningEffort.isEmpty {
            guard let fallback = option?.defaultReasoningEffort else { return "Uses the model's default reasoning level." }
            return "Uses the model's default level: \(ReasoningLevel.title(fallback))."
        }
        return efforts.first(where: { $0.reasoningEffort == store.codexReasoningEffort })?.description
            ?? ReasoningLevel.title(store.codexReasoningEffort)
    }
}

private struct ModelRow: View {
    let title: String
    let subtitle: String?
    let identifier: String?
    let isDefault: Bool
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 15))
                    .foregroundStyle(isSelected ? AppTheme.accent : Color.secondary)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(title).font(.body.weight(isSelected ? .semibold : .regular))
                        if isDefault {
                            Text("Recommended")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(AppTheme.accent)
                                .padding(.horizontal, 6).padding(.vertical, 1)
                                .background(AppTheme.accent.opacity(0.12), in: Capsule())
                        }
                    }
                    if let subtitle, !subtitle.isEmpty {
                        Text(subtitle).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if let identifier {
                    Text(identifier)
                        .font(.caption.monospaced())
                        .foregroundStyle(.tertiary)
                        .textSelection(.enabled)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

private struct LevelChip: View {
    let title: String
    let symbol: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 4) {
                Image(systemName: symbol).font(.system(size: 13))
                Text(title).font(.caption.weight(.medium)).lineLimit(1)
            }
            .frame(maxWidth: .infinity, minHeight: 44)
            .foregroundStyle(isSelected ? Color.white : Color.primary)
            .background(isSelected ? AppTheme.accent : Color.primary.opacity(0.06),
                        in: RoundedRectangle(cornerRadius: 8))
            .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

private struct DotLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.font(.system(size: 6))
            configuration.title
        }
    }
}
