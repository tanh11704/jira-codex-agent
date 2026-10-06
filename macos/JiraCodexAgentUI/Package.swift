// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JiraCodexAgentUI",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "JiraCodexAgentUI", targets: ["JiraCodexAgentUI"])],
    targets: [
        .executableTarget(
            name: "JiraCodexAgentUI",
            path: "Sources/JiraCodexAgentUI"
        )
    ]
)
