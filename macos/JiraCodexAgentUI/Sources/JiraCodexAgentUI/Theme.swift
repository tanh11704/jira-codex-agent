import SwiftUI

enum AppTheme {
    static let accent = Color(red: 0.25, green: 0.48, blue: 0.98)
    static let panel = Color(nsColor: .controlBackgroundColor)
}

extension Color {
    static func taskTint(_ name: String) -> Color {
        switch name {
        case "blue": return .blue
        case "green": return .green
        case "red": return .red
        default: return .secondary
        }
    }
}
