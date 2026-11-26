import SwiftUI

private enum SettingsTab: String, CaseIterable, Identifiable {
    case jira = "Jira"
    case agents = "Agents"
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
            case .agents: agentsTab
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
        let enabled = AgentRole.allCases.contains { store.isEnabled($0) && store.agent(for: $0) == .claude }
        return Form {
            Section {
                Stepper("5 hours: \(store.claudeQuotaFiveHourThreshold)%", value: $store.claudeQuotaFiveHourThreshold, in: 0...100)
                Stepper("7 days: \(store.claudeQuotaWeeklyThreshold)%", value: $store.claudeQuotaWeeklyThreshold, in: 0...100)
                Stepper(store.claudeMaxTurns == 0 ? "Turns per pass: no limit" : "Turns per pass: \(store.claudeMaxTurns)",
                        value: $store.claudeMaxTurns, in: 0...200, step: 10)
            } header: {
                Text("Skip Claude when remaining quota reaches")
            } footer: {
                Text("Read from your plan's /usage before every Claude pass, so the rest stays for your own Claude work. A skipped pass never stops the queue: the coder works without the plan, the review is skipped and the task waits for you, or Codex codes instead of Claude Code.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .disabled(!enabled)
            Section {
                Picker("Model", selection: $store.claudeModel) {
                    Text("CLI default").tag("")
                    ForEach(ModelNames.claudeAliases, id: \.0) { Text($0.1).tag($0.0) }
                    if !([""] + ModelNames.claudeAliases.map(\.0)).contains(store.claudeModel) {
                        Text(store.claudeModel).tag(store.claudeModel)
                    }
                }
                Picker("Effort", selection: $store.claudeEffort) {
                    Text("Default").tag("")
                    ForEach(["low", "medium", "high", "xhigh", "max"], id: \.self) { Text(ReasoningLevel.title($0)).tag($0) }
                }
            } header: {
                Text("Default model")
            } footer: {
                Text("Used by Claude jobs that keep \"Default\" as their model in the Agents tab.")
                    .font(.caption).foregroundStyle(.secondary)
            }
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

    // MARK: Agents

    private var agentsTab: some View {
        Form {
            Section {
                ForEach(AgentRole.allCases) { role in
                    RoleSummaryRow(role: role, agent: store.agent(for: role), summary: store.runSummary(for: role),
                                   isEnabled: store.isEnabled(role))
                }
            } header: {
                Text("Who does what")
            } footer: {
                Text("New tasks use these agents after the daemon restarts; a task that is interrupted continues with the agent that started each job. Every task records who did each job, shown under Who did what in its details.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            roleSection(.plan, footer: "Read-only. Explores the repository and writes the implementation plan, the issue's area (frontend, backend or full-stack) and open questions; the coder follows the plan. If the planner is unavailable, the coder plans alone.") {
                Toggle("Plan before coding", isOn: $store.claudePlan)
            }
            roleSection(.backend, footer: "Implements the issue, or only its server side when frontend work goes to its own agent. Claude Code and Antigravity edit the worktree directly without Codex's sandbox and never commit; if they cannot run, Codex takes over.") {
                EmptyView()
            }
            roleSection(.frontend, footer: "When on, each issue is classified: frontend issues go to this agent, full-stack issues are split (backend agent first, then this agent builds the UI on top), backend issues stay with the backend agent. It follows the same task prompt, including the UI quality and imagery rules, and reports every image slot. If it cannot run (quota, sign-in), the backend agent codes the UI instead.") {
                Toggle("Send frontend work to its own agent", isOn: $store.claudeFrontend)
                if store.claudeFrontend && store.agent(for: .frontend) == store.agent(for: .backend) {
                    Label("Same agent as Backend: it codes whole issues and nothing is split.", systemImage: "info.circle")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            roleSection(.review, footer: "Read-only. Reviews the coder's diff and screenshots after it reports COMPLETED; blocker and major findings go back to the same coder session. If they remain after the allowed rounds, the task moves to Needs you. Frontend-only work by its own agent is left to your review.") {
                Toggle("Review the coder's changes", isOn: $store.claudeReview)
                Stepper(store.claudeReviewRounds == 0 ? "Fix rounds: none (report only)" : "Fix rounds: \(store.claudeReviewRounds)",
                        value: $store.claudeReviewRounds, in: 0...5)
                    .disabled(!store.claudeReview)
            }
            roleSection(.decide, footer: "Instead of waiting for you, answers the plan's open questions before coding and the questions the coder asks, using the specs and the existing code; the coder then continues in the same session. Every decision, with its reason, confidence, risk and sources, is listed at the end of the report for you to check. Its instructions are under Prompt → Product owner prompt.") {
                Toggle("Decide open questions as product owner", isOn: $store.claudeDecide)
                Stepper(store.claudeDecideRounds == 0 ? "Rounds after the coder asks: none (plan only)"
                        : "Rounds after the coder asks: \(store.claudeDecideRounds)",
                        value: $store.claudeDecideRounds, in: 0...5)
                    .disabled(!store.claudeDecide)
            }

            Section {
                HStack {
                    Image(systemName: isAntigravityConnected ? "checkmark.circle.fill" : "person.crop.circle.badge.exclamationmark")
                        .foregroundStyle(isAntigravityConnected ? .green : .orange)
                    Text(store.antigravityStatus).lineLimit(2)
                    Spacer()
                    if store.isLoadingAntigravityModels {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Check") { Task { await store.checkAntigravity() } }
                            .help("Run agy models to check the sign-in and load the models on your plan")
                    }
                }
                TextField("Antigravity executable", text: $store.antigravityPath)
                Picker("Default model", selection: $store.antigravityModel) {
                    antigravityModelOptions(selected: store.antigravityModel)
                }
            } header: {
                Text("Antigravity account")
            } footer: {
                Text("Runs the Antigravity CLI headless (agy --print) in the task worktree, signed in with your Antigravity plan; run agy once in Terminal to sign in. Read-only jobs may only read the worktree and run read-only git commands. Coding may edit the worktree and run any command except git commit, push, reset, rebase, merge, checkout, switch, stash and clean.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .task { if store.antigravityModels.isEmpty { await store.checkAntigravity() } }
    }

    private func roleSection<Switches: View>(_ role: AgentRole, footer: String,
                                             @ViewBuilder switches: () -> Switches) -> some View {
        let enabled = store.isEnabled(role)
        let agent = store.agent(for: role)
        return Section {
            switches()
            Picker("Agent", selection: agentBinding(role)) {
                ForEach(role.agents) { kind in
                    Label(kind.coderName, systemImage: kind.symbol).tag(kind)
                }
            }
            .disabled(!enabled)
            Group {
                switch agent {
                case .codex:
                    LabeledContent("Model", value: store.codexRunSummary)
                    Text("Codex uses the model, reasoning and speed from the Codex tab.")
                        .font(.caption).foregroundStyle(.secondary)
                case .claude:
                    Picker("Model", selection: modelBinding(role)) {
                        Text("Default (\(store.claudeModel.isEmpty ? "CLI default" : ModelNames.pretty(store.claudeModel, agent: .claude)))").tag("")
                        ForEach(ModelNames.claudeAliases, id: \.0) { Text($0.1).tag($0.0) }
                        let model = store.roleModels[role.rawValue] ?? ""
                        if !([""] + ModelNames.claudeAliases.map(\.0)).contains(model) {
                            Text(ModelNames.pretty(model, agent: .claude)).tag(model)
                        }
                    }
                    Picker("Effort", selection: effortBinding(role)) {
                        Text("Default").tag("")
                        ForEach(["low", "medium", "high", "xhigh", "max"], id: \.self) { Text(ReasoningLevel.title($0)).tag($0) }
                    }
                case .antigravity:
                    Picker("Model", selection: modelBinding(role)) {
                        Text("Default (\(ModelNames.pretty(store.antigravityModel, agent: .antigravity, catalog: store.antigravityModels)))").tag("")
                        antigravityModelOptions(selected: store.roleModels[role.rawValue] ?? "")
                    }
                    if role == .frontend {
                        Label("Gemini 4 Hydrogen (High) ranks highest for web UI among the models on your plan (WebDev Arena) and uses less quota than XHigh.",
                              systemImage: "star.fill")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if enabled && !isAntigravityConnected && !store.isLoadingAntigravityModels {
                        Label("Antigravity is not ready: \(store.antigravityStatus)", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            }
            .disabled(!enabled)
        } header: {
            Label(role.title, systemImage: role.symbol)
        } footer: {
            Text(footer).font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func antigravityModelOptions(selected: String) -> some View {
        ForEach(store.antigravityModels) { option in
            Text(option.displayName + (option.id == AgentStore.defaultAntigravityModel ? " — recommended for UI" : ""))
                .tag(option.id)
        }
        if !selected.isEmpty && !store.antigravityModels.contains(where: { $0.id == selected }) {
            Text(ModelNames.pretty(selected, agent: .antigravity)).tag(selected)
        }
    }

    private func agentBinding(_ role: AgentRole) -> Binding<AgentKind> {
        Binding(get: { store.agent(for: role) }, set: { store.setAgent($0, for: role) })
    }

    private func modelBinding(_ role: AgentRole) -> Binding<String> {
        Binding(get: { store.roleModels[role.rawValue] ?? "" }, set: { store.roleModels[role.rawValue] = $0 })
    }

    private func effortBinding(_ role: AgentRole) -> Binding<String> {
        Binding(get: { store.roleEfforts[role.rawValue] ?? "" }, set: { store.roleEfforts[role.rawValue] = $0 })
    }

    private var isAntigravityConnected: Bool { store.antigravityStatus.hasPrefix("Connected") }

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
            Section {
                TextEditor(text: $store.decidePrompt)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 180)
                    .accessibilityLabel("Product owner prompt")
            } header: {
                HStack {
                    Text("Product owner prompt")
                    Spacer()
                    Button("Restore default") { store.decidePrompt = AgentStore.defaultDecidePrompt }
                        .buttonStyle(.borderless)
                        .disabled(store.decidePrompt == AgentStore.defaultDecidePrompt)
                }
            } footer: {
                Text("How the decision agent (Agents → Decisions) decides: its role, which evidence it trusts and what it prefers when judging. The issue, plan and questions are added automatically, as is the rule to answer every question by id. Applies after the daemon restarts. An empty prompt uses the default.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .disabled(!store.claudeDecide)
            Section {
                TextEditor(text: $store.verifyCommands)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 90)
                    .accessibilityLabel("Local check commands, one per line")
                Stepper(store.verifyFixRounds == 0 ? "Fix rounds: none (report only)" : "Fix rounds: \(store.verifyFixRounds)",
                        value: $store.verifyFixRounds, in: 0...5)
            } header: {
                Text("Local checks")
            } footer: {
                Text("One shell command per line (e.g. pnpm install --frozen-lockfile, pnpm build, pnpm test), run in order in the task worktree after the coding agent reports COMPLETED. They run outside the agent sandbox, so Docker and Testcontainers work; a report that is incomplete only because the sandbox blocked checks is verified here and upgraded to COMPLETED when they pass. Agents are told not to run full builds themselves; on failure only the trimmed error output goes back to them. Lines starting with # are ignored. Leave empty to let agents run checks themselves.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                Toggle("Compare failing checks with the base commit", isOn: $store.baselineChecks)
                TextField("Base worktree setup (optional), e.g. pnpm install --frozen-lockfile", text: $store.baselineSetupCommand)
                    .font(.system(.body, design: .monospaced))
                    .disabled(!store.baselineChecks)
            } header: {
                Text("Pre-existing failures")
            } footer: {
                Text("When a check fails, it is rerun once on the base commit in a shared worktree (cached per commit). If the base fails with the same errors, the check is marked PRE-EXISTING and does not block the task; any new error still goes back to the agent.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                TextEditor(text: $store.autoApproveCommands)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 70)
                    .accessibilityLabel("Commands approved automatically, one pattern per line")
            } header: {
                Text("Approve commands automatically")
            } footer: {
                Text("One pattern per line, * matches anything (e.g. ./mvnw *, make check, pnpm test*). When Codex asks to run a matching command outside its sandbox, it is approved without waiting for you. A leading cd <dir> && is allowed; commands with ;, |, redirects, & or $( ) never match. Lines starting with # are ignored.")
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

private struct RoleSummaryRow: View {
    let role: AgentRole
    let agent: AgentKind
    let summary: String
    let isEnabled: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: agent.symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(isEnabled ? agent.tint : Color.secondary)
                .frame(width: 30, height: 30)
                .background((isEnabled ? agent.tint : Color.secondary).opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 1) {
                Text(role.title).font(.caption).foregroundStyle(.secondary)
                Text(isEnabled ? summary : "Off").font(.body.weight(isEnabled ? .semibold : .regular))
                    .foregroundStyle(isEnabled ? Color.primary : Color.secondary)
            }
            Spacer()
            Text(role.tag)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(.quaternary.opacity(0.6), in: Capsule())
        }
        .padding(.vertical, 1)
        .accessibilityElement(children: .combine)
    }
}
