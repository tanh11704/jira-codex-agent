import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: AgentStore

    var body: some View {
        TabView {
            Form {
                Section("Jira Cloud") {
                    TextField("https://company.atlassian.net", text: $store.jiraURL)
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
            .padding(20)
            .tabItem { Label("Jira", systemImage: "shippingbox") }

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
            }
            .padding(20)
            .tabItem { Label("Codex", systemImage: "sparkles") }

            Form {
                Section("Daemon connection") {
                    TextField("Unix socket", text: $store.socketPath)
                    Text("The UI and daemon must use the same socket path.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button("Reconnect") { Task { await store.refresh() } }
            }
            .padding(20)
            .tabItem { Label("Advanced", systemImage: "gearshape") }
        }
        .safeAreaInset(edge: .bottom) {
            HStack {
                if let message = store.configurationMessage {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Save configuration") { store.saveConfiguration() }
                    .buttonStyle(.borderedProminent)
            }
            .padding(14)
            .background(.bar)
        }
        .frame(width: 640, height: 470)
    }
}
