import Foundation
import SwiftUI

@MainActor
final class MenuModel: ObservableObject {
    @Published private(set) var state: ServiceState = .stopped
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var changing = false
    @Published private(set) var localSnapshot: LocalModelSnapshot?
    @Published private(set) var localError: String?
    @Published private(set) var modelChanging = Set<String>()
    @Published private(set) var toolSettings: ToolSettings?
    @Published private(set) var toolError: String?
    @Published private(set) var toolChanging = false
    @Published private(set) var healthRefreshing = false
    let command: OrcaCommandClient?
    private let client: any MenuService
    private let localClient: (any LocalModelService)?
    private let toolClient: (any ToolSettingsService)?
    private var polling: Task<Void, Never>?
    private var localPolling: Task<Void, Never>?
    private var toolPolling: Task<Void, Never>?
    private var toolRefreshing = false
    private var toolRevision = 0
    private var localRefreshing = false
    private var localRevision = 0
    private var refreshing = false
    private var revision = 0
    private var healthRefreshPending = false

    init(client: any MenuService, localClient: (any LocalModelService)? = nil,
         toolClient: (any ToolSettingsService)? = nil, command: OrcaCommandClient? = nil, poll: Bool = true) {
        self.command = command
        self.client = client
        self.localClient = localClient
        self.toolClient = toolClient
        if poll {
            polling = Task { [weak self] in
                while !Task.isCancelled {
                    await self?.refresh()
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                }
            }
            if localClient != nil {
                localPolling = Task { [weak self] in
                    while !Task.isCancelled {
                        await self?.refreshModels()
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                }
            }
            if toolClient != nil {
                toolPolling = Task { [weak self] in
                    while !Task.isCancelled {
                        await self?.refreshTools()
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                }
            }
        }
    }

    deinit { polling?.cancel(); localPolling?.cancel(); toolPolling?.cancel() }

    var canEditTools: Bool { state == .running && !toolChanging && toolSettings != nil && toolError == nil }

    func refreshTools() async {
        guard let toolClient, !toolRefreshing, !toolChanging else { return }
        toolRefreshing = true
        let revision = toolRevision
        defer { toolRefreshing = false }
        do {
            let value = try await toolClient.toolSettings()
            guard revision == toolRevision, !toolChanging else { return }
            toolSettings = value
            toolError = nil
        } catch {
            guard revision == toolRevision, !toolChanging else { return }
            if let error = error as? ConnectionError, case .stopped = error {
                toolSettings = nil
                toolError = "Orcaは停止中です"
            } else {
                toolError = "ツール設定を読み込めません"
            }
        }
    }

    @discardableResult
    func setToolModels(toolID: String, models: [String]) -> Task<Void, Never>? {
        guard let toolClient, let settings = toolSettings, canEditTools,
              settings.tools.contains(where: { $0.id == toolID }) else { return nil }
        toolChanging = true
        toolRevision += 1
        return Task {
            defer { toolChanging = false }
            do {
                toolSettings = try await toolClient.setToolModels(toolID: toolID, models: models, revision: settings.revision)
                toolError = nil
            } catch {
                toolError = "ツール設定を保存できません。再読み込み後に操作してください"
            }
        }
    }

    @discardableResult
    func selectToolModel(toolID: String, modelID: String, selected: Bool) -> Task<Void, Never>? {
        guard let settings = toolSettings, let tool = settings.tools.first(where: { $0.id == toolID }) else { return nil }
        var models = settings.selectedModels(for: tool)
        if selected && !models.contains(modelID) { models.append(modelID) }
        if !selected { models.removeAll { $0 == modelID } }
        return setToolModels(toolID: toolID, models: models)
    }

    var displayModels: [ModelRow] {
        guard let localSnapshot else { return snapshot?.models ?? [] }
        let routing = Dictionary(uniqueKeysWithValues: (snapshot?.models ?? []).map { ($0.id, $0) })
        return localSnapshot.models.map { row in
            ModelRow(id: row.id, name: row.name, enabled: routing[row.id]?.enabled ?? false,
                     health: routing[row.id]?.health ?? row.health)
        }
    }

    func canSelect(_ row: ModelRow) -> Bool {
        state == .running && !changing && snapshot?.models.contains(where: { $0.id == row.id }) == true
    }

    func refreshModels() async {
        guard let localClient, !localRefreshing, modelChanging.isEmpty else { return }
        localRefreshing = true
        let revision = localRevision
        defer { localRefreshing = false }
        do {
            let value = try await localClient.snapshot()
            guard revision == localRevision else { return }
            localSnapshot = value
            localError = value.configError ? "モデル設定を確認してください" : nil
        } catch {
            guard revision == localRevision else { return }
            localError = "モデル管理に接続できません"
        }
    }

    @discardableResult
    func controlModel(_ row: LocalModelRow, start: Bool) -> Task<Void, Never>? {
        guard let localClient, modelChanging.isEmpty, start ? row.canStart : row.canStop else { return nil }
        modelChanging.insert(row.id)
        localRevision += 1
        return Task {
            defer { modelChanging.remove(row.id) }
            do {
                let value = try await (start ? localClient.start(modelID: row.id) : localClient.stop(modelID: row.id))
                localSnapshot = value
                localError = value.configError ? "モデル設定を確認してください" : nil
            } catch {
                localError = "モデルの操作に失敗しました"
            }
        }
    }

    func apply(_ value: Snapshot) {
        snapshot = value
        healthRefreshing = healthRefreshPending || value.modelRefresh?.status == "running"
        state = value.state == "running" && value.ready ? .running : .error
    }

    func refreshHealth() {
        guard let command, !healthRefreshing, state == .running else { return }
        revision += 1
        healthRefreshPending = true
        healthRefreshing = true
        Task {
            defer { healthRefreshPending = false }
            do {
                let _: RefreshOperation = try await command.call(["model", "--refresh", "--wait", "false"], as: RefreshOperation.self)
                healthRefreshPending = false
                await refresh()
            } catch {
                healthRefreshing = false
                localError = "モデル状態を更新できません"
            }
        }
    }

    func refresh() async {
        guard !refreshing, !changing else { return }
        refreshing = true
        let requestedRevision = revision
        defer { refreshing = false }
        do {
            let value = try await client.snapshot()
            guard requestedRevision == revision, !changing else { return }
            apply(value)
        } catch {
            guard requestedRevision == revision, !changing else { return }
            present(error)
        }
    }

    @discardableResult
    func setEnabled(_ model: ModelRow, to enabled: Bool) -> Task<Void, Never>? {
        guard state == .running, !changing else { return nil }
        changing = true
        revision += 1
        return Task {
            defer { changing = false }
            do {
                _ = try await client.setEnabled(modelID: model.id, enabled: enabled)
                apply(try await client.snapshot())
            } catch {
                present(error)
            }
        }
    }

    private func present(_ error: Error) {
        if let connection = error as? ConnectionError, case .stopped = connection {
            healthRefreshing = false
            snapshot = nil
            state = .stopped
        } else {
            state = .error
        }
    }
}
