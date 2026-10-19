import SwiftUI

struct QuotaView: View {
    @EnvironmentObject private var store: AgentStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Quota", systemImage: "gauge").font(.headline)
                if let date = store.quotaUpdatedAt {
                    Text("Checked \(date, style: .relative) ago").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if store.isLoadingQuota { ProgressView().controlSize(.small) }
                Button("Check now") { Task { await store.refreshQuota() } }
                    .disabled(store.isLoadingQuota)
            }
            provider("Codex", error: store.quotaError, info: store.quota?.snapshot,
                     fiveHourThreshold: store.quota?.fiveHourThreshold, weeklyThreshold: store.quota?.weeklyThreshold,
                     blockedNote: "new tasks will wait")
            if store.quota?.snapshot?.ordinary_usage_allowed == false {
                Text("Codex reports usage unavailable. New tasks will wait.").font(.caption).foregroundStyle(.orange)
            }
            if let claude = store.quota?.claude {
                Divider()
                provider("Claude", error: claude.snapshot?.error, info: claude.snapshot,
                         fiveHourThreshold: claude.fiveHourThreshold, weeklyThreshold: claude.weeklyThreshold,
                         blockedNote: "plan and review are skipped")
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
    }

    @ViewBuilder
    private func provider(_ name: String, error: String?, info: QuotaInfo?, fiveHourThreshold: Int?,
                          weeklyThreshold: Int?, blockedNote: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(name).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            if let error {
                Text("Quota unavailable: \(error)").font(.caption).foregroundStyle(.orange)
            } else {
                HStack(spacing: 20) {
                    window("5 hours", info: info?.fiveHour, threshold: fiveHourThreshold, blockedNote: blockedNote)
                    window("7 days", info: info?.weekly, threshold: weeklyThreshold, blockedNote: blockedNote)
                }
            }
        }
    }

    private func window(_ title: String, info: QuotaWindowInfo?, threshold: Int?, blockedNote: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(title).fontWeight(.semibold)
                Spacer()
                Text(info.map { "\($0.remaining)% remaining" } ?? "Unavailable")
            }
            if let info {
                ProgressView(value: Double(info.remaining), total: 100)
                    .tint(info.remaining <= (threshold ?? 30) ? .orange : .green)
                HStack {
                    Text("Stop at ≤ \(threshold ?? 30)%")
                    Spacer()
                    if let date = info.resetDate {
                        Text("Reset: \(date.formatted(date: .abbreviated, time: .shortened))")
                    } else { Text("Reset unavailable") }
                }.font(.caption).foregroundStyle(.secondary)
                if info.remaining <= (threshold ?? 30) {
                    Text("Below stop threshold — \(blockedNote)").font(.caption).foregroundStyle(.orange)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
