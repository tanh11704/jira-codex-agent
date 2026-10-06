import SwiftUI

struct MenuBarView: View {
    @EnvironmentObject private var store: AgentStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Jira Codex Agent").font(.headline)
                    if let current = store.currentTask {
                        Text("\(current.issueKey) · \(current.summary)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    } else {
                        Text(store.isPaused ? "Paused" : "Idle")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                StatusBadge(connection: store.connection, isPaused: store.isPaused)
            }

            HStack {
                Label("\(store.queuedCount) queued", systemImage: "clock")
                Spacer()
                Label("\(store.reviewCount) review", systemImage: "checkmark.circle")
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            Divider()
            Button("Open Dashboard") {
                openWindow(id: "dashboard")
                NSApp.activate(ignoringOtherApps: true)
            }
            Button(store.isPaused ? "Resume Agent" : "Pause Agent") {
                Task { await store.togglePaused() }
            }
            .disabled(store.connection != .online || store.isWorking)
            Button("Refresh") { Task { await store.refresh() } }
            Button("Accounts & Settings…") {
                store.isShowingSettings = true
                openWindow(id: "dashboard")
                NSApp.activate(ignoringOtherApps: true)
            }
            Divider()
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .padding(12)
        .frame(width: 310)
        .task { store.startRefreshing() }
    }
}
