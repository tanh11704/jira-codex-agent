import SwiftUI

struct StatusBadge: View {
    let connection: AgentConnection
    let isPaused: Bool

    private var label: String {
        switch connection {
        case .connecting: return "Connecting"
        case .offline: return "Offline"
        case .online: return isPaused ? "Paused" : "Running"
        }
    }

    private var color: Color {
        switch connection {
        case .connecting: return .orange
        case .offline: return .red
        case .online: return isPaused ? .orange : .green
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(color.opacity(0.12), in: Capsule())
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let symbol: String
    let color: Color

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.title2)
                .foregroundStyle(color)
                .frame(width: 40, height: 40)
                .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 2) {
                Text(value).font(.title2.bold()).monospacedDigit()
                Text(title).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(16)
        .background(AppTheme.panel, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.separator.opacity(0.4)))
    }
}

struct TaskStateBadge: View {
    let task: AgentTask

    var body: some View {
        Label(task.displayState, systemImage: task.symbol)
            .font(.caption.weight(.medium))
            .foregroundStyle(Color.taskTint(task.tintName))
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color.taskTint(task.tintName).opacity(0.1), in: Capsule())
    }
}

struct EmptyState: View {
    let filter: TaskFilter

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: filter.symbol)
                .font(.system(size: 38))
                .foregroundStyle(.tertiary)
            Text(filter == .all ? "No tasks yet" : "Nothing in \(filter.rawValue.lowercased())")
                .font(.title3.weight(.semibold))
            Text("The dashboard updates automatically when the daemon discovers Jira issues.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .multilineTextAlignment(.center)
    }
}
