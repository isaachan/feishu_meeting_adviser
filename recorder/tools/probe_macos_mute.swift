// One-shot, opt-in diagnostic for Feishu's inaccessible meeting controls.
// Writes only a small crop around the microphone control, never a full window.
import AppKit
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers

@main
struct Probe {
    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            fputs("usage: probe_macos_mute <output-directory>\n", stderr)
            exit(2)
        }
        let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        do {
            _ = NSApplication.shared
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: true)
            let windows = content.windows.filter {
                $0.owningApplication?.bundleIdentifier == "com.electron.lark.iron"
                    && $0.frame.width >= 150 && $0.frame.height >= 100
            }
            if windows.isEmpty {
                print("未找到屏幕上的飞书会议窗口")
                return
            }
            for window in windows {
                let filter = SCContentFilter(desktopIndependentWindow: window)
                let config = SCStreamConfiguration()
                config.width = max(1, Int(filter.contentRect.width * CGFloat(filter.pointPixelScale)))
                config.height = max(1, Int(filter.contentRect.height * CGFloat(filter.pointPixelScale)))
                let image = try await SCScreenshotManager.captureImage(
                    contentFilter: filter, configuration: config)
                let width = image.width
                let height = image.height
                let compact = Double(width) / Double(height) > 2.4 && height < 600
                let rect: CGRect
                if compact {
                    rect = CGRect(x: CGFloat(width) * 0.25, y: CGFloat(height) * 0.35,
                                  width: CGFloat(width) * 0.25, height: CGFloat(height) * 0.6)
                } else {
                    rect = CGRect(x: CGFloat(width) * 0.14, y: CGFloat(height) * 0.88,
                                  width: CGFloat(width) * 0.16, height: CGFloat(height) * 0.12)
                }
                guard let crop = image.cropping(to: rect.integral) else { continue }
                let output = directory.appendingPathComponent("mic-\(window.windowID).png")
                guard let destination = CGImageDestinationCreateWithURL(
                    output as CFURL, UTType.png.identifier as CFString, 1, nil) else {
                    throw NSError(domain: "probe", code: 1)
                }
                CGImageDestinationAddImage(destination, crop, nil)
                guard CGImageDestinationFinalize(destination) else {
                    throw NSError(domain: "probe", code: 2)
                }
                print("\(output.path) (\(width)x\(height), \(compact ? "compact" : "full"))")
            }
        } catch {
            fputs("采样失败：\(error)\n", stderr)
            exit(1)
        }
    }
}
