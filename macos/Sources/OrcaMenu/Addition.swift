import SwiftUI

@MainActor
final class AdditionWindows: ObservableObject {
    private var windows: [Bool: NSWindow] = [:]

    func show(model: MenuModel, tool: Bool) {
        let window: NSWindow
        if let existing = windows[tool] { window = existing }
        else {
            window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 320),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = tool ? "ツールを追加" : "モデルを追加"
            window.identifier = NSUserInterfaceItemIdentifier(tool ? "add-tool" : "add-model")
            window.isReleasedWhenClosed = false
            let controller = NSHostingController(rootView: AdditionContent(model: model, tool: tool,
                onClose: { [weak window] in window?.close() }))
            window.contentViewController = controller
            window.setContentSize(controller.view.fittingSize)
            window.center()
            windows[tool] = window
        }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}

struct AdditionContent: View {
    @ObservedObject var model: MenuModel
    let tool: Bool
    var onClose: () -> Void = {}
    @State private var identifier = ""
    @State private var apiModel = ""
    @State private var baseURL = "http://127.0.0.1:8000/v1"
    @State private var keychain = false
    @State private var service = ""
    @State private var account = ""
    @State private var concurrent = "1"
    @State private var speed = ""
    @State private var description = ""
    @State private var assignment = "all"
    @State private var selected = Set<String>()
    @State private var choices: [ToolModelChoice] = []
    @State private var revision: String?
    @State private var error: String?
    @State private var saving = false
    @State private var loading = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Form {
                TextField(tool ? "ツールID" : "モデルID", text: $identifier)
                if tool {
                    TextField("説明", text: $description)
                    Picker("割り当て", selection: $assignment) {
                        Text("すべて").tag("all")
                        Text("選択").tag("selected")
                        Text("なし").tag("none")
                    }
                    if assignment == "selected" {
                        ScrollView {
                            VStack(alignment: .leading) {
                                ForEach(choices) { choice in
                                    Toggle("\(choice.id) (\(choice.name))", isOn: Binding(
                                        get: { selected.contains(choice.id) },
                                        set: { if $0 { selected.insert(choice.id) } else { selected.remove(choice.id) } }))
                                }
                            }
                        }.frame(height: min(160, CGFloat(max(1, choices.count)) * 26))
                    }
                } else {
                    TextField("APIモデルID", text: $apiModel)
                    TextField("接続URL", text: $baseURL)
                    Toggle("Keychain", isOn: $keychain)
                    if keychain {
                        TextField("Service", text: $service)
                        TextField("Account", text: $account)
                    }
                    TextField("同時実行数", text: $concurrent)
                    TextField("推定速度(tokens/sec)", text: $speed)
                }
            }.disabled(saving)
            if !tool {
                Text("詳細設定はmodels.jsonで設定できます。")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let error { Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Spacer()
                Button("キャンセル") { onClose() }.disabled(saving)
                Button(saving ? "保存中…" : "追加") { Task { await save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving || revision == nil || identifier.isEmpty)
            }
        }.padding(20).frame(width: 440)
            .task { await load() }
            .onReceive(NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)) { notification in
                guard revision == nil, let window = notification.object as? NSWindow,
                      window.identifier?.rawValue == (tool ? "add-tool" : "add-model") else { return }
                Task { await load() }
            }
    }

    private func load() async {
        guard let command = model.command, !saving, !loading else { return }
        loading = true
        defer { loading = false }
        revision = nil
        do {
            if tool {
                let settings = try await command.call(["tool", "--settings"], as: ToolSettings.self)
                revision = settings.revision
                choices = settings.models
            } else {
                revision = try await command.call(["model", "--settings"], as: DefinitionSettings.self).revision
            }
            error = nil
        } catch { self.error = "設定を読み込めません。設定ファイルを確認してください。" }
    }

    private func save() async {
        guard let command = model.command, let revision, !saving else { return }
        var entry: [String: Any] = ["id": identifier]
        if tool {
            entry["models"] = assignment == "all" ? ["all"] : assignment == "none" ? [] : selected.sorted()
            if !description.isEmpty { entry["description"] = description }
        } else {
            guard let count = Int(concurrent), (1...1024).contains(count) else {
                error = "同時実行数は1〜1024で入力してください。"; return
            }
            entry["model"] = apiModel
            entry["base_url"] = baseURL
            entry["max_concurrent_requests"] = count
            if !speed.isEmpty {
                guard let value = Double(speed), value.isFinite, value > 0 else {
                    error = "指定速度は正の数で入力してください。"; return
                }
                entry["tokens_per_second"] = value
            }
            if keychain { entry["keychain"] = ["service": service, "account": account] }
        }
        saving = true
        defer { saving = false }
        do {
            let data = try JSONSerialization.data(withJSONObject: ["revision": revision, tool ? "tool" : "model": entry])
            if tool {
                let result = try await command.call(["tool", "--add"], input: data, as: ToolSettings.self)
                self.revision = result.revision
                choices = result.models
            } else {
                let result = try await command.call(["model", "--add"], input: data, as: DefinitionSettings.self)
                self.revision = result.revision
            }
            await model.refresh()
            await model.refreshModels()
            await model.refreshTools()
            identifier = ""
            error = nil
            onClose()
        } catch {
            self.error = "保存を確認できません。追加済みのIDや入力内容を確認し、このウィンドウを開き直してください。"
            self.revision = nil
        }
    }
}
