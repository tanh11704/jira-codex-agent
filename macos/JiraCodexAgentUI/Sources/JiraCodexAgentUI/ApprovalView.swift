import SwiftUI

struct ApprovalView: View {
    @EnvironmentObject private var store: AgentStore

    var body: some View {
        if !store.approvals.isEmpty {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(store.approvals) { approval in
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Waiting for approval", systemImage: "hand.raised.fill")
                                .font(.headline).foregroundStyle(.orange)
                            Text(store.tasks.first(where: { $0.sessionId == approval.threadId })?.issueKey ?? "Codex task")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(approval.command ?? (approval.kind.contains("fileChange") ? "Approve file changes" : "Approve requested permissions"))
                                .font(.system(.body, design: .monospaced)).textSelection(.enabled)
                            Text("Directory: \(approval.cwd)").textSelection(.enabled)
                            Text(approval.reason ?? "Codex requests additional access. Review the details before approving.")
                            DisclosureGroup("Request details") {
                                Text(approval.details + "\nChanges: " + approval.changes)
                                    .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            }
                            HStack {
                                Text(approval.scope == "turn" ? "Requested permissions apply to this turn only." : "Applies only to this request; no session-wide grant.")
                                    .font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button("Decline") { Task { await store.answerApproval(approval, accept: false) } }
                                Button("Approve") { Task { await store.answerApproval(approval, accept: true) } }
                                    .buttonStyle(.borderedProminent)
                            }.disabled(store.answeringApprovals.contains(approval.id))
                        }.padding(12).background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                    }
                }
            }.frame(maxHeight: 240)
        }
    }
}
