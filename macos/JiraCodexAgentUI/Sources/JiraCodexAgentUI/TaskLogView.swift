import SwiftUI

struct TaskLogView: View {
    @EnvironmentObject private var store: AgentStore
    @Binding var isShowingLogs: Bool
    @Binding var isExpanded: Bool
    @State private var follow = true

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Codex logs", systemImage: "terminal").font(.headline)
                if let task = store.selectedTaskID ?? store.currentTask?.issueKey {
                    Text(task).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("Auto-scroll", isOn: $follow).toggleStyle(.checkbox)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(store.logEvents.map { "[\($0.timestamp)] \($0.kind)\n\($0.message)" }.joined(separator: "\n\n"), forType: .string)
                }
                Button { isExpanded.toggle() } label: {
                    Image(systemName: isExpanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                }
                .help(isExpanded ? "Collapse Codex logs" : "Expand Codex logs")
                .accessibilityLabel(isExpanded ? "Collapse Codex logs" : "Expand Codex logs")
                Button { isShowingLogs = false; isExpanded = false } label: {
                    Image(systemName: "xmark")
                }.help("Hide Codex logs")
            }.padding(12)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if let error = store.logError {
                            Text("Logs unavailable: \(error)").foregroundStyle(.red)
                        } else if store.logEvents.isEmpty {
                            Text("Select a task to view its recorded Codex activity. Logs start after the daemon is updated.")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(store.logEvents) { event in
                            VStack(alignment: .leading, spacing: 4) {
                                Text("\(event.timestamp) · \(event.kind)").font(.caption).foregroundStyle(.secondary)
                                Text(event.message).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                            }.id(event.id)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                }
                .onChange(of: store.logEvents.last?.id) { id in
                    if follow, let id { proxy.scrollTo(id, anchor: .bottom) }
                }
            }
        }
        .frame(minHeight: 250, maxHeight: isExpanded ? .infinity : 250)
        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.separator.opacity(0.4)))
        .task {
            while !Task.isCancelled {
                await store.refreshLogs()
                do { try await Task.sleep(for: .seconds(2)) } catch { break }
            }
        }
        .onChange(of: store.selectedTaskID) { _ in Task { await store.refreshLogs() } }
    }
}
