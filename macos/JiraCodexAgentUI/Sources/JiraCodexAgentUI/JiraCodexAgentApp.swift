import SwiftUI

final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }
}

@main
struct JiraCodexAgentApp: App {
    @NSApplicationDelegateAdaptor(ApplicationDelegate.self) private var applicationDelegate
    @StateObject private var store = AgentStore()

    var body: some Scene {
        Window("Jira Codex Agent", id: "dashboard") {
            DashboardView().environmentObject(store)
        }
        .defaultSize(width: 1080, height: 700)

        MenuBarExtra {
            MenuBarView().environmentObject(store)
        } label: {
            Image(systemName: menuBarSymbol)
        }
        .menuBarExtraStyle(.window)
    }

    private var menuBarSymbol: String {
        switch store.connection {
        case .online: return store.isPaused ? "pause.circle.fill" : "cpu.fill"
        case .connecting: return "ellipsis.circle"
        case .offline: return "exclamationmark.circle.fill"
        }
    }
}
