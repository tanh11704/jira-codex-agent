import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: AgentStore
    @Environment(\.dismiss) private var dismiss
    @State private var selectedTab = "Jira"

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
                    Text("Jira").tag("Jira")
                    Text("Codex").tag("Codex")
                    Text("Advanced").tag("Advanced")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }
            .padding(24)
            Divider()
            if selectedTab == "Jira" {
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
            } else if selectedTab == "Codex" {
            Form {
                Section("Codex account") {
                    HStack {
                        Image(systemName: store.codexStatus.contains("Connected") ? "checkmark.circle.fill" : "person.crop.circle.badge.exclamationmark")
                            .foregroundStyle(store.codexStatus.contains("Connected") ? .green : .orange)
                        Text(store.codexStatus)
                        Spacer()
                        Button("Check") { Task { await store.checkCodexLogin() } }
                    }
                    TextField("Codex executable", text: $store.codexPath)
                    Button("Sign in with ChatGPT") { store.loginCodex() }
                        .buttonStyle(.borderedProminent)
                }
                Section("Approvals") {
                    Picker("Approval mode", selection: $store.codexApprovalsReviewer) {
                        Text("Ask for approval — you review").tag("user")
                        Text("Approve for me — Codex reviews").tag("auto_review")
                    }
                    Text(store.codexApprovalsReviewer == "auto_review"
                         ? "Codex evaluates eligible approval requests automatically. The workspace sandbox stays enabled; some requests may still need you. Auto-review can make mistakes and adds processing."
                         : "Approval requests appear in the dashboard for you to approve or decline.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Save and restart the daemon to apply to new or resumed tasks.")
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
                    Text("Either limit pauses new tasks. Save and restart the daemon to apply.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Task prompt") {
                    Text("Instructions applied to every task. Jira key, title and description are added automatically.")
                        .font(.caption).foregroundStyle(.secondary)
                    TextEditor(text: $store.taskPrompt)
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 160)
                        .accessibilityLabel("Task prompt instructions")
                    Button("Restore default prompt") { store.taskPrompt = AgentStore.defaultTaskPrompt }
                    Text("Save and restart the daemon. Applies to new and resumed tasks, not a task already running. An empty prompt uses the default.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Default model for new tasks") {
                    Picker("Model", selection: $store.codexModel) {
                        Text("Use Codex CLI configuration").tag("")
                        ForEach(store.availableModels) { option in
                            Text(option.displayName + " (" + option.model + ")").tag(option.model)
                        }
                        if !store.codexModel.isEmpty && !store.availableModels.contains(where: { $0.model == store.codexModel }) {
                            Text(store.codexModel + " — not in current catalog").tag(store.codexModel)
                        }
                    }
                    Button(store.isLoadingModels ? "Loading models…" : "Refresh models from Codex") {
                        Task { await store.reloadModels() }
                    }.disabled(store.isLoadingModels)
                    if let error = store.modelCatalogError {
                        Text(error).font(.caption).foregroundStyle(.red)
                    }
                }
            }
            .formStyle(.grouped)
            .task { await store.reloadModels() }
            } else {
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
            Divider()
            HStack {
                if let message = store.configurationMessage {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Save configuration") { store.saveConfiguration() }
                    .buttonStyle(.borderedProminent)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
            .background(.bar)
        }
        .frame(width: 700, height: 620)
    }
}
