// swift-tools-version: 5.9
import PackageDescription
import Foundation

var orcaTargets: [Target] = [.executableTarget(name: "OrcaMenu")]
let localTests = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .appendingPathComponent("Tests/OrcaMenuTests").path
if FileManager.default.fileExists(atPath: localTests) {
    orcaTargets.append(.testTarget(name: "OrcaMenuTests", dependencies: ["OrcaMenu"]))
}

let package = Package(
    name: "OrcaMenu",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "OrcaMenu", targets: ["OrcaMenu"])],
    targets: orcaTargets
)
