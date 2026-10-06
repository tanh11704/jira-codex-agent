import SwiftUI

struct QuotaView: View {
    @EnvironmentObject private var store: AgentStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Codex quota", systemImage: "gauge").font(.headline)
                if let date = store.quotaUpdatedAt {
                    Text("Checked \(date, style: .relative) ago").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if store.isLoadingQuota { ProgressView().controlSize(.small) }
                Button("Check now") { Task { await store.refreshQuota() } }
                    .disabled(store.isLoadingQuota)
            }
            if let error = store.quotaError {
                Text("Quota unavailable: \(error)").font(.caption).foregroundStyle(.orange)
            } else {
                HStack(spacing: 20) {
                    window("5 hours", info: store.quota?.snapshot.fiveHour, threshold: store.quota?.fiveHourThreshold)
                    window("7 days", info: store.quota?.snapshot.weekly, threshold: store.quota?.weeklyThreshold)
                }
                if store.quota?.snapshot.ordinary_usage_allowed == false {
                    Text("Codex reports usage unavailable. New tasks will wait.").font(.caption).foregroundStyle(.orange)
                }
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .task {
            while !Task.isCancelled {
                await store.refreshQuota()
                do { try await Task.sleep(for: .seconds(60)) } catch { break }
            }
        }
    }

    private func window(_ title: String, info: QuotaWindowInfo?, threshold: Int?) -> some View {
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
                    Text("Below stop threshold — new tasks will wait").font(.caption).foregroundStyle(.orange)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
