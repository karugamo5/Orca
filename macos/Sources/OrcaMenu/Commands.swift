import Foundation
import Darwin

struct CommandEnvelope<Value: Decodable>: Decodable {
    let v: Int
    let ok: Bool
    let result: Value?
    let error: CommandFailure?
}
struct CommandFailure: Decodable { let code: String }
struct AcceptedOperation: Decodable, Sendable { let accepted: Bool }
struct RefreshOperation: Decodable, Sendable { let generation: Int; let status: String }
struct DefinitionSettings: Decodable, Sendable { let revision: String; let models: [ToolModelChoice] }

struct OrcaCommandClient: Sendable {
    let home: String
    var runtime: String? = nil

    func call<Value: Decodable & Sendable>(_ arguments: [String], input: Data? = nil,
                                          as type: Value.Type) async throws -> Value {
        let root = home
        let runtimeDirectory = runtime
        return try await Task.detached(priority: .utility) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: root).appendingPathComponent("current/venv/bin/python")
            var args = ["-I", "-m", "orca", "--home", root]
            if let runtimeDirectory { args += ["--runtime-dir", runtimeDirectory] }
            process.arguments = args + arguments + ["--json"] + (input == nil ? [] : ["--input_json"])
            let output = Pipe()
            let source = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            process.standardInput = input == nil ? FileHandle.nullDevice : source.fileHandleForReading
            try process.run()
            let reading = Task.detached { () throws -> Data in
                var data = Data()
                while let chunk = try output.fileHandleForReading.read(upToCount: 8192), !chunk.isEmpty {
                    data.append(chunk)
                    guard data.count <= 1024 * 1024 else { throw ConnectionError.invalid }
                }
                return data
            }
            let writing = Task.detached {
                defer { try? source.fileHandleForWriting.close() }
                if let input { try source.fileHandleForWriting.write(contentsOf: input) }
            }
            defer {
                if process.isRunning { process.terminate() }
                try? output.fileHandleForReading.close()
                try? source.fileHandleForReading.close()
            }
            let deadline = ContinuousClock.now.advanced(by: .seconds(20))
            while process.isRunning && ContinuousClock.now < deadline {
                try await Task.sleep(nanoseconds: 20_000_000)
            }
            if process.isRunning {
                process.terminate()
                Darwin.kill(process.processIdentifier, SIGKILL)
                throw ConnectionError.unavailable
            }
            try await writing.value
            let raw = try await reading.value
            let response = try JSONDecoder().decode(CommandEnvelope<Value>.self, from: raw)
            guard response.v == 1 else { throw ConnectionError.invalid }
            if !response.ok {
                if response.error?.code == "stopped" { throw ConnectionError.stopped }
                throw ConnectionError.invalid
            }
            guard process.terminationStatus == 0, let result = response.result else { throw ConnectionError.invalid }
            return result
        }.value
    }
}

struct CommandMenuService: MenuService {
    let reader: SocketClient
    let command: OrcaCommandClient
    func snapshot() async throws -> Snapshot { try await reader.snapshot() }
    func setEnabled(modelID: String, enabled: Bool) async throws -> Bool {
        try await command.call(["model", enabled ? "--enable" : "--disable", modelID], as: EnableResult.self).enabled
    }
}

struct CommandToolService: ToolSettingsService {
    let reader: SocketClient
    let command: OrcaCommandClient
    func toolSettings() async throws -> ToolSettings { try await reader.toolSettings() }
    func setToolModels(toolID: String, models: [String], revision: String) async throws -> ToolSettings {
        let data = try JSONEncoder().encode(["models": SendableValue.strings(models), "revision": .string(revision)])
        return try await command.call(["tool", "--assign", toolID], input: data, as: ToolSettings.self)
    }
}

struct CommandLocalService: LocalModelService {
    let reader: LocalModelClient
    let command: OrcaCommandClient
    func snapshot() async throws -> LocalModelSnapshot { try await reader.snapshot() }
    func start(modelID: String) async throws -> LocalModelSnapshot {
        let _: AcceptedOperation = try await command.call(["model", "--start", modelID, "--wait", "false"], as: AcceptedOperation.self)
        return try await snapshot()
    }
    func stop(modelID: String) async throws -> LocalModelSnapshot {
        let _: AcceptedOperation = try await command.call(["model", "--stop", modelID, "--wait", "false"], as: AcceptedOperation.self)
        return try await snapshot()
    }
}
