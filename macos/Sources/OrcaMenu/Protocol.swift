import Foundation
import CryptoKit
import Darwin

struct ModelRow: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let enabled: Bool
    let health: String
}

struct LocalModelRow: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let health: String
    let processState: String
    let error: String?
    let canStart: Bool
    let canStop: Bool
    let configured: Bool
    enum CodingKeys: String, CodingKey {
        case id, name, health, error, configured
        case processState = "process_state", canStart = "can_start", canStop = "can_stop"
    }
    var statusLabel: String? {
        switch processState {
        case "starting": return "起動中"
        case "stopping": return "終了中"
        case "failed": return "操作失敗"
        default: return nil
        }
    }
    var errorLabel: String {
        switch error {
        case "server_exists": return "サーバーは既に起動しています"
        case "connection_failed": return "接続設定や既存サーバーを確認してください"
        case "configuration_changed": return "起動中に設定が変更されました"
        case "process_exited": return "モデルのプロセスが終了しました"
        case "startup_timeout": return "起動確認が期限を超えました"
        case "start_failed": return "起動コマンドを実行できませんでした"
        case "stop_failed": return "プロセスの終了を確認できませんでした"
        default: return ""
        }
    }
}

struct LocalModelSnapshot: Decodable, Sendable {
    let models: [LocalModelRow]
    let configError: Bool
    enum CodingKeys: String, CodingKey { case models, configError = "config_error" }
}

protocol LocalModelService: Sendable {
    func snapshot() async throws -> LocalModelSnapshot
    func start(modelID: String) async throws -> LocalModelSnapshot
    func stop(modelID: String) async throws -> LocalModelSnapshot
}

struct LocalModelClient: LocalModelService {
    let home: String
    var runtime: String? = nil
    var socket: SocketClient {
        let daemon = URL(fileURLWithPath: SocketClient.socketPath(home: home, runtime: runtime))
        return SocketClient(path: daemon.deletingLastPathComponent().appendingPathComponent("models.sock").path)
    }

    func snapshot() async throws -> LocalModelSnapshot {
        do {
            return try await socket.call("snapshot", as: LocalModelSnapshot.self)
        } catch ConnectionError.stopped {
            // Explicit menu use can start the manager, never a model or the MCP daemon.
            try await ensureManager()
            return try await socket.call("snapshot", as: LocalModelSnapshot.self)
        }
    }

    func start(modelID: String) async throws -> LocalModelSnapshot {
        _ = try await snapshot()
        return try await socket.call("start", data: ["model_id": .string(modelID)], as: LocalModelSnapshot.self)
    }

    func stop(modelID: String) async throws -> LocalModelSnapshot {
        _ = try await snapshot()
        return try await socket.call("stop", data: ["model_id": .string(modelID)], as: LocalModelSnapshot.self)
    }

    func ensureManager() async throws {
        let root = home
        let runtimeDirectory = runtime
        try await Task.detached(priority: .utility) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: root).appendingPathComponent("current/venv/bin/python")
            var args = ["-m", "orca", "--home", root]
            if let runtimeDirectory { args += ["--runtime-dir", runtimeDirectory] }
            process.arguments = args + ["model", "--status"]
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            try process.run()
            let deadline = Date().addingTimeInterval(15)
            while process.isRunning && Date() < deadline { try await Task.sleep(nanoseconds: 50_000_000) }
            if process.isRunning {
                process.terminate()
                throw ConnectionError.unavailable
            }
            guard process.terminationStatus == 0 else { throw ConnectionError.unavailable }
        }.value
    }
}

struct RequestRow: Decodable, Identifiable, Sendable {
    let requestID: String
    let source: String
    let status: String
    let receivedAt: String
    let startedAt: String?
    var id: String { requestID }
    enum CodingKeys: String, CodingKey {
        case requestID = "request_id", source, status, receivedAt = "received_at", startedAt = "started_at"
    }

    func elapsed(at date: Date) -> Int {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let raw = status == "running" ? (startedAt ?? receivedAt) : receivedAt
        guard let start = formatter.date(from: raw) else { return 0 }
        return max(0, Int(date.timeIntervalSince(start)))
    }

    var stateLabel: String {
        switch status {
        case "running": return "実行中"
        case "retry_wait": return "待機中"
        default: return "待機中"
        }
    }
}

struct Snapshot: Decodable, Sendable {
    let state: String
    let ready: Bool
    let maxPending: Int
    let pendingCount: Int
    let models: [ModelRow]
    let requests: [RequestRow]
    var requestCount: Int? = nil
    var modelRefresh: RefreshOperation? = nil
    var totalRequestCount: Int { requestCount ?? requests.count }
    enum CodingKeys: String, CodingKey {
        case state, ready, models, requests, maxPending = "max_pending", pendingCount = "pending_count"
        case requestCount = "request_count"
        case modelRefresh = "model_refresh"
    }
}

enum ServiceState: String {
    case running = "稼働中"
    case stopped = "停止中"
    case error = "エラー"
}

enum ConnectionError: Error, Sendable { case stopped, invalid, unavailable }

struct Envelope<Value: Decodable>: Decodable {
    let v: Int
    let id: String
    let ok: Bool
    let result: Value?
}

struct EnableResult: Decodable, Sendable { let enabled: Bool }

struct ToolRow: Decodable, Identifiable, Sendable {
    let id: String
    let models: [String]
    var includesAll: Bool { models == ["all"] }
}

struct ToolModelChoice: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
}

struct ToolSettings: Decodable, Sendable {
    let revision: String
    let tools: [ToolRow]
    let models: [ToolModelChoice]

    func selectedModels(for tool: ToolRow) -> [String] {
        tool.includesAll ? models.map(\.id) : tool.models
    }

    func choices(for tool: ToolRow) -> [ToolModelChoice] {
        models + tool.models.filter { $0 != "all" && !models.map(\.id).contains($0) }
            .map { ToolModelChoice(id: $0, name: "未登録") }
    }

    func summary(for tool: ToolRow) -> String { "\(selectedModels(for: tool).count)モデル" }
}

protocol ToolSettingsService: Sendable {
    func toolSettings() async throws -> ToolSettings
    func setToolModels(toolID: String, models: [String], revision: String) async throws -> ToolSettings
}

protocol MenuService: Sendable {
    func snapshot() async throws -> Snapshot
    func setEnabled(modelID: String, enabled: Bool) async throws -> Bool
}

struct SocketClient: MenuService, ToolSettingsService {
    let path: String

    func snapshot() async throws -> Snapshot {
        try await call("snapshot", as: Snapshot.self)
    }

    func setEnabled(modelID: String, enabled: Bool) async throws -> Bool {
        try await call("enable", data: ["model_id": .string(modelID), "enabled": .bool(enabled)], as: EnableResult.self).enabled
    }

    func toolSettings() async throws -> ToolSettings {
        try await call("tool_settings", as: ToolSettings.self)
    }

    func setToolModels(toolID: String, models: [String], revision: String) async throws -> ToolSettings {
        try await call("set_tool_models", data: ["tool_id": .string(toolID), "models": .strings(models),
                                               "revision": .string(revision)], as: ToolSettings.self)
    }

    static func socketPath(home: String, runtime: String? = nil) -> String {
        if let runtime { return URL(fileURLWithPath: runtime).appendingPathComponent("daemon.sock").path }
        let resolved = URL(fileURLWithPath: home).resolvingSymlinksInPath().path
        let digest = SHA256.hash(data: Data(resolved.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16)
        return "/tmp/orca-\(getuid())-\(digest)/daemon.sock"
    }

    // Blocking POSIX IO is isolated on a detached task; never blocks the UI actor.
    func call<Value: Decodable & Sendable>(_ op: String, data: [String: SendableValue] = [:], as type: Value.Type) async throws -> Value {
        let socketPath = path
        return try await Task.detached(priority: .utility) {
            let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
            guard fd >= 0 else { throw ConnectionError.unavailable }
            defer { Darwin.close(fd) }
            var noSignal: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            var timeout = timeval(tv_sec: 5, tv_usec: 0)
            setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
            let bytes = Array(socketPath.utf8) + [0]
            guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw ConnectionError.invalid }
            withUnsafeMutableBytes(of: &address.sun_path) { buffer in
                buffer.copyBytes(from: bytes)
            }
            let flags = fcntl(fd, F_GETFL)
            guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else { throw ConnectionError.unavailable }
            let result = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            if result != 0 {
                let code = errno
                if code == ENOENT || code == ECONNREFUSED { throw ConnectionError.stopped }
                guard code == EINPROGRESS else { throw ConnectionError.unavailable }
                var pollFD = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                guard poll(&pollFD, 1, 5000) > 0 else { throw ConnectionError.unavailable }
                var error: Int32 = 0
                var length = socklen_t(MemoryLayout<Int32>.size)
                guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) == 0, error == 0 else { throw ConnectionError.unavailable }
            }
            guard fcntl(fd, F_SETFL, flags) == 0 else { throw ConnectionError.unavailable }
            let id = UUID().uuidString
            let message = IPCRequest(v: 1, id: id, op: op, data: data)
            var payload = try JSONEncoder().encode(message)
            payload.append(10)
            try payload.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let count = Darwin.send(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset, 0)
                    if count < 0 && errno == EINTR { continue }
                    guard count > 0 else { throw ConnectionError.unavailable }
                    offset += count
                }
            }
            var response = Data()
            var buffer = [UInt8](repeating: 0, count: 8192)
            while true {
                let count = Darwin.recv(fd, &buffer, buffer.count, 0)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw ConnectionError.unavailable }
                response.append(contentsOf: buffer.prefix(count))
                guard response.count <= 1024 * 1024 else { throw ConnectionError.invalid }
                if response.last == 10 { break }
            }
            let envelope = try JSONDecoder().decode(Envelope<Value>.self, from: response)
            guard envelope.v == 1, envelope.id == id, envelope.ok, let value = envelope.result else { throw ConnectionError.invalid }
            return value
        }.value
    }
}

struct IPCRequest: Encodable {
    let v: Int
    let id: String
    let op: String
    let data: [String: SendableValue]
}

enum SendableValue: Encodable, Sendable {
    case string(String), bool(Bool), strings([String])
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .strings(let value): try container.encode(value)
        }
    }
}
