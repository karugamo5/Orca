import AppKit
import SwiftUI

@main
struct OrcaMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuApplicationDelegate.self) private var delegate
    @StateObject private var model: MenuModel
    @StateObject private var additions = AdditionWindows()
    private let instance: MenuInstance

    init() {
        let args = CommandLine.arguments
        func option(_ key: String) -> String? {
            guard let index = args.firstIndex(of: key), index + 1 < args.count else { return nil }
            return args[index + 1]
        }
        let home = option("--home") ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Orca").path
        guard let ownership = MenuInstance.claim(home: home) else { exit(0) }
        instance = ownership
        let client = SocketClient(path: SocketClient.socketPath(home: home, runtime: option("--runtime-dir")))
        let command = OrcaCommandClient(home: home, runtime: option("--runtime-dir"))
        _model = StateObject(wrappedValue: MenuModel(client: CommandMenuService(reader: client, command: command),
            localClient: CommandLocalService(reader: LocalModelClient(home: home, runtime: option("--runtime-dir")), command: command),
            toolClient: CommandToolService(reader: client, command: command), command: command))
        delegate.instance = ownership
    }

    var body: some Scene {
        MenuBarExtra("Orca", systemImage: "waveform.path") {
            MenuContent(model: model, openAddition: { tool in additions.show(model: model, tool: tool) })
        }
        .menuBarExtraStyle(.window)
    }
}

final class MenuApplicationDelegate: NSObject, NSApplicationDelegate {
    var instance: MenuInstance?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        guard instance?.publishPID() == true else { exit(1) }
    }
}

struct MenuContent: View {
    @ObservedObject var model: MenuModel
    var openAddition: ((Bool) -> Void)? = nil

    func selectionBinding(for row: ModelRow) -> Binding<Bool> {
        Binding(get: { row.enabled }, set: { model.setEnabled(row, to: $0) })
    }

    var modelSelection: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                if !model.displayModels.isEmpty {
                    ForEach(model.displayModels) { row in
                        let local = model.localSnapshot?.models.first(where: { $0.id == row.id })
                        HStack(spacing: 6) {
                            Toggle(isOn: selectionBinding(for: row)) {
                                HStack(spacing: 8) {
                                    Text("\(row.id) (\(row.name))")
                                        .lineLimit(1).truncationMode(.middle)
                                    Spacer()
                                    HStack(spacing: 5) {
                                        Circle()
                                            .fill(row.health == "online" ? Color.green : row.health == "offline" ? .red : .gray)
                                            .frame(width: 7, height: 7)
                                            .accessibilityHidden(true)
                                        Text(local?.statusLabel ?? (row.health == "online" ? "オンライン" : row.health == "offline" ? "オフライン" : "未確認"))
                                            .foregroundStyle(.secondary)
                                    }
                                    .font(.caption)
                                    .fixedSize()
                                }
                            }
                            .toggleStyle(.checkbox)
                            .controlSize(.small)
                            .frame(minHeight: 24)
                            .disabled(!model.canSelect(row))
                            if let local {
                                if local.canStop || local.processState == "stopping" {
                                    Button("終了") { model.controlModel(local, start: false) }
                                        .disabled(!local.canStop || !model.modelChanging.isEmpty)
                                } else if local.canStart {
                                    Button("開始") { model.controlModel(local, start: true) }
                                        .disabled(!model.modelChanging.isEmpty)
                                }
                            }
                        }
                        .controlSize(.small)
                        .help(local?.errorLabel ?? "")
                    }
                } else {
                    Text("モデルがありません").foregroundStyle(.secondary)
                        .frame(minHeight: 24)
                }
            }
        }
        // MenuBarExtra can propose zero height to a flexible ScrollView.
        // Reserve one row for the empty state and cap longer lists for scrolling.
        .frame(height: min(180, CGFloat(max(1, model.displayModels.count)) * 28 - 4))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Orca").fontWeight(.semibold)
                Spacer()
                HStack(spacing: 5) {
                    Image(systemName: model.state == .stopped ? "circle" : "circle.fill")
                        .font(.system(size: 7))
                        .foregroundStyle(model.state == .running ? Color.green : model.state == .error ? .orange : .secondary)
                        .accessibilityHidden(true)
                    Text(model.state.rawValue).foregroundStyle(.secondary)
                }
                .font(.callout)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("service-state")
            Divider()
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("モデル").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button { model.refreshHealth() } label: {
                        if model.healthRefreshing { ProgressView().controlSize(.mini) }
                        else { Image(systemName: "arrow.clockwise") }
                    }.disabled(model.command == nil || model.state != .running || model.healthRefreshing)
                        .help(model.healthRefreshing ? "更新中" : "モデル状態を更新")
                        .accessibilityLabel("モデル状態を更新")
                    Button { openAddition?(false) } label: { Image(systemName: "plus") }
                        .disabled(model.command == nil).help("モデルを追加").accessibilityLabel("モデルを追加")
                }.controlSize(.small)
                modelSelection
                if let error = model.localError {
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("ツール").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button { openAddition?(true) } label: { Image(systemName: "plus") }
                        .disabled(model.command == nil).help("ツールを追加").accessibilityLabel("ツールを追加")
                }.controlSize(.small)
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        if let settings = model.toolSettings {
                            if settings.tools.isEmpty {
                                Text("ツールがありません").foregroundStyle(.secondary).frame(minHeight: 24)
                            } else {
                                ForEach(settings.tools) { tool in
                                    ToolModelPicker(model: model, toolID: tool.id)
                                }
                            }
                        } else {
                            Text("—").foregroundStyle(.secondary).frame(minHeight: 24)
                        }
                    }
                }
                .frame(height: min(160, CGFloat(max(1, model.toolSettings?.tools.count ?? 0)) * 28 - 4))
                if let error = model.toolError {
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("キュー")
                    Spacer()
                    if let snapshot = model.snapshot {
                        Text("待機 \(snapshot.pendingCount) / \(snapshot.maxPending == 0 ? "無制限" : String(snapshot.maxPending))")
                    } else {
                        Text("—")
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
                if let snapshot = model.snapshot {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        VStack(alignment: .leading, spacing: 4) {
                            if snapshot.requests.isEmpty {
                                Text("依頼はありません").foregroundStyle(.secondary)
                            }
                            ForEach(Array(snapshot.requests.prefix(10))) { row in
                                HStack(spacing: 8) {
                                    Text(row.source).lineLimit(1)
                                    Spacer()
                                    Text(row.stateLabel).foregroundStyle(.secondary)
                                    Text("\(row.elapsed(at: context.date))秒").monospacedDigit()
                                        .frame(minWidth: 48, alignment: .trailing)
                                }
                            }
                            if snapshot.totalRequestCount > 10 {
                                Text("ほか\(snapshot.totalRequestCount - 10)件").foregroundStyle(.secondary)
                            }
                        }
                        .font(.callout)
                    }
                }
            }
        }
        .font(.body).fontWeight(.regular)
        .buttonStyle(.plain)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(width: 360)
        // MenuBarExtra's window supplies the native material, corners, and shadow.
        // Leave the content background clear so it follows the system appearance.
    }
}
