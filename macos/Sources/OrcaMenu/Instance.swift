import Foundation
import Darwin

final class MenuInstance {
    private let descriptor: Int32
    private let pidPath: String

    private init(descriptor: Int32, pidPath: String) {
        self.descriptor = descriptor
        self.pidPath = pidPath
    }

    static func claim(home: String) -> MenuInstance? {
        let directory = URL(fileURLWithPath: home)
        let lock = directory.appendingPathComponent("menu.lock").path
        let pidPath = directory.appendingPathComponent("menu.pid").path
        let descriptor = Darwin.open(lock, O_RDWR | O_CREAT | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { return nil }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor)
            return nil
        }
        // Claim before initializing SwiftUI, but publish readiness only after
        // AppKit finishes launching. A transient startup lock is not readiness.
        return MenuInstance(descriptor: descriptor, pidPath: pidPath)
    }

    func publishPID() -> Bool {
        let pidFD = Darwin.open(pidPath, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard pidFD >= 0 else { return false }
        let data = Data(String(getpid()).utf8)
        let written = data.withUnsafeBytes { Darwin.write(pidFD, $0.baseAddress, $0.count) }
        Darwin.close(pidFD)
        return written == data.count
    }

    deinit {
        Darwin.unlink(pidPath)
        Darwin.close(descriptor)
    }
}
