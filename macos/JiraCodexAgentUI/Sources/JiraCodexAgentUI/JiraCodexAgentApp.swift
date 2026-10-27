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
        .defaultSize(width: 1280, height: 760)

        WindowGroup("Task", id: "task", for: String.self) { $taskID in
            TaskWindow(taskID: taskID).environmentObject(store)
        }
        .defaultSize(width: 860, height: 820)

        WindowGroup("Reader", id: "reader", for: ReaderRequest.self) { $request in
            ReaderWindow(request: request).environmentObject(store)
        }
        .defaultSize(width: 860, height: 760)

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
