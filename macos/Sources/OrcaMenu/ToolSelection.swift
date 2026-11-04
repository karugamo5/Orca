import SwiftUI

struct ToolModelPicker: View {
    @ObservedObject var model: MenuModel
    let toolID: String
    @State private var isPresented = false

    var body: some View {
        HStack(spacing: 8) {
            Text(toolID).lineLimit(1).truncationMode(.middle)
            Spacer()
            Button {
                isPresented.toggle()
            } label: {
                HStack(spacing: 5) {
                    if let settings = model.toolSettings, let tool = settings.tools.first(where: { $0.id == toolID }) {
                        Text(settings.summary(for: tool)).monospacedDigit()
                    }
                    Image(systemName: "chevron.down").font(.caption2)
                }
            }
            .controlSize(.small)
            .buttonStyle(.plain)
            .disabled(!model.canEditTools)
            .accessibilityLabel("\(toolID)の割り当てモデル")
            .popover(isPresented: $isPresented, arrowEdge: .bottom) {
                ToolSelectionContent(model: model, toolID: toolID)
            }
        }
        .frame(minHeight: 24)
    }
}

struct ToolSelectionContent: View {
    @ObservedObject var model: MenuModel
    let toolID: String

    func selectionBinding(for modelID: String) -> Binding<Bool> {
        Binding(get: {
            guard let settings = model.toolSettings, let tool = settings.tools.first(where: { $0.id == toolID }) else { return false }
            return settings.selectedModels(for: tool).contains(modelID)
        }, set: { model.selectToolModel(toolID: toolID, modelID: modelID, selected: $0) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(toolID).fontWeight(.semibold).lineLimit(1).truncationMode(.middle)
            if let settings = model.toolSettings, let tool = settings.tools.first(where: { $0.id == toolID }) {
                Toggle("すべてのモデル（追加分も含む）", isOn: Binding(
                    get: { model.toolSettings?.tools.first(where: { $0.id == toolID })?.includesAll ?? false },
                    set: { model.setToolModels(toolID: toolID, models: $0 ? ["all"] : []) }))
                    .disabled(!model.canEditTools)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(settings.choices(for: tool)) { choice in
                            Toggle("\(choice.id) (\(choice.name))", isOn: selectionBinding(for: choice.id))
                                .lineLimit(1).truncationMode(.middle)
                                .disabled(!model.canEditTools || choice.id == "all")
                        }
                        if settings.choices(for: tool).isEmpty {
                            Text("モデルがありません").foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: min(240, CGFloat(max(1, settings.choices(for: tool).count)) * 24))
                Text(settings.summary(for: tool)).font(.caption).foregroundStyle(.secondary)
            } else {
                Text("ツールがありません").foregroundStyle(.secondary)
            }
            if let error = model.toolError {
                Text(error).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .toggleStyle(.checkbox).controlSize(.small)
        .padding(12).frame(width: 320, alignment: .leading)
    }
}
