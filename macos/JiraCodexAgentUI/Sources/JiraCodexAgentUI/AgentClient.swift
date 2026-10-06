import Darwin
import Foundation

enum AgentClientError: LocalizedError {
    case socketPathTooLong
    case connectionFailed(String)
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .socketPathTooLong: return "The Unix socket path is too long."
        case .connectionFailed(let message): return message
        case .invalidResponse: return "The daemon returned an invalid response."
        case .server(let message): return message
        }
    }
}

final class AgentClient: @unchecked Sendable {
    func request<Response: Decodable & Sendable>(
        _ command: String,
        socketPath: String,
        as type: Response.Type
    ) async throws -> Response {
        try await Task.detached(priority: .userInitiated) {
            let data = try self.performRequest(command, socketPath: socketPath)
            if let serverError = try? JSONDecoder().decode(ErrorResponse.self, from: data) {
                throw AgentClientError.server(serverError.error)
            }
            do {
                return try JSONDecoder().decode(type, from: data)
            } catch {
                throw AgentClientError.invalidResponse
            }
        }.value
    }

    private func performRequest(_ command: String, socketPath: String) throws -> Data {
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw AgentClientError.connectionFailed(String(cString: strerror(errno)))
        }
        defer { close(descriptor) }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8CString)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard pathBytes.count <= capacity else { throw AgentClientError.socketPathTooLong }

        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: capacity) { destination in
                pathBytes.withUnsafeBufferPointer { source in
                    destination.initialize(from: source.baseAddress!, count: pathBytes.count)
                }
            }
        }

        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            throw AgentClientError.connectionFailed("Daemon is not running or the socket cannot be reached.")
        }

        let request = try JSONSerialization.data(withJSONObject: ["command": command]) + Data([0x0A])
        let sent = request.withUnsafeBytes { bytes in
            Darwin.write(descriptor, bytes.baseAddress, bytes.count)
        }
        guard sent == request.count else {
            throw AgentClientError.connectionFailed("Could not send the command to the daemon.")
        }

        var response = Data()
        var byte: UInt8 = 0
        while true {
            let count = Darwin.read(descriptor, &byte, 1)
            if count <= 0 { break }
            if byte == 0x0A { break }
            response.append(byte)
        }
        guard !response.isEmpty else { throw AgentClientError.invalidResponse }
        return response
    }
}
