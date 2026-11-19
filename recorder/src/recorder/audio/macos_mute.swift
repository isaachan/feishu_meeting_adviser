import AppKit
import ApplicationServices
import Foundation
import ImageIO
import ScreenCaptureKit

// Read only the microphone action of a Feishu meeting window. The action is
// the opposite of the current state: "打开麦克风" means currently muted.
let meetingBundles = ["com.electron.lark.iron", "com.electron.lark"]
let meetingTitles = ["飞书会议", "Feishu Meeting", "Lark Meeting"]
let mutedActions = ["打开麦克风", "Turn on microphone"]
let liveActions = ["关闭麦克风", "Turn off microphone"]

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
        return nil
    }
    return value
}

func stringAttribute(_ element: AXUIElement, _ name: String) -> String {
    return attribute(element, name) as? String ?? ""
}

func elements(_ element: AXUIElement, _ name: String) -> [AXUIElement] {
    return attribute(element, name) as? [AXUIElement] ?? []
}

func actionState(_ label: String) -> Bool? {
    let text = label.trimmingCharacters(in: .whitespacesAndNewlines)
    if mutedActions.contains(text) { return true }
    if liveActions.contains(text) { return false }
    return nil
}

func inspect(_ window: AXUIElement) -> (Bool, String)? {
    var queue: [(AXUIElement, Int)] = [(window, 0)]
    var index = 0
    var liveLabel: String? = nil
    while index < queue.count && index < 600 {
        let (element, depth) = queue[index]
        index += 1
        let role = stringAttribute(element, kAXRoleAttribute)
        if role == kAXButtonRole as String {
            for key in [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute] {
                let label = stringAttribute(element, key)
                if let muted = actionState(label) {
                    // Several controls may be exposed in one window. An
                    // explicit mute action wins over an unrelated live one.
                    if muted { return (true, label) }
                    liveLabel = label
                }
            }
        }
        if depth < 12 {
            queue.append(contentsOf: elements(element, kAXChildrenAttribute).map { ($0, depth + 1) })
        }
    }
    return liveLabel.map { (false, $0) }
}

func accessibilitySample() -> (Bool?, String, Bool) {
    guard AXIsProcessTrusted() else { return (nil, "辅助功能未授权", false) }
    var foundMeeting = false
    var liveLabel: String? = nil
    for bundle in meetingBundles {
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: bundle) {
            let root = AXUIElementCreateApplication(app.processIdentifier)
            AXUIElementSetMessagingTimeout(root, 0.2)
            for window in elements(root, kAXWindowsAttribute) {
                let title = stringAttribute(window, kAXTitleAttribute)
                // The meeting is a separate .iron app on current Feishu. For
                // the main app, require a meeting-specific window title.
                if bundle != "com.electron.lark.iron" &&
                    !meetingTitles.contains(where: { title.localizedCaseInsensitiveContains($0) }) {
                    continue
                }
                foundMeeting = true
                if let (muted, label) = inspect(window) {
                    if muted { return (true, label, true) }
                    liveLabel = label
                }
            }
        }
    }
    if let label = liveLabel { return (false, label, true) }
    return (nil, foundMeeting ? "未找到可用的麦克风按钮" : "未找到飞书会议窗口", foundMeeting)
}

// A visible mute signal always closes the gate. AX alone cannot open it: Feishu
// may expose an unrelated participant control while hiding the local mic.
func reconciledMute(_ ax: Bool?, _ icon: Bool?) -> Bool? {
    if ax == true || icon == true { return true }
    if icon == false { return false }
    return nil
}

struct IconStats {
    let red: Int
    let gray: Int
    let area: Int

    var muted: Bool? {
        let signal = max(25, area / 12)
        if red >= signal && red > gray { return true }
        if gray >= signal && red < max(8, area / 200) { return false }
        return nil
    }
}

func iconStats(_ image: CGImage, centerX: Int, centerY: Int, radius: Int) -> IconStats? {
    let rect = CGRect(x: centerX - radius, y: centerY - radius,
                      width: radius * 2, height: radius * 2).integral
    guard rect.minX >= 0, rect.minY >= 0,
          rect.maxX <= CGFloat(image.width), rect.maxY <= CGFloat(image.height),
          let crop = image.cropping(to: rect) else { return nil }
    let width = crop.width
    let height = crop.height
    var pixels = [UInt8](repeating: 0, count: width * height * 4)
    let space = CGColorSpaceCreateDeviceRGB()
    let bitmap = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
    guard let context = CGContext(data: &pixels, width: width, height: height,
                                  bitsPerComponent: 8, bytesPerRow: width * 4,
                                  space: space, bitmapInfo: bitmap) else { return nil }
    context.draw(crop, in: CGRect(x: 0, y: 0, width: width, height: height))
    var red = 0
    var gray = 0
    for index in stride(from: 0, to: pixels.count, by: 4) {
        let r = Int(pixels[index])
        let g = Int(pixels[index + 1])
        let b = Int(pixels[index + 2])
        if r > 170 && r > g * 3 / 2 && r > b * 3 / 2 && g < 170 {
            red += 1
        } else if r > 35 && r < 180 && abs(r - g) < 35 && abs(g - b) < 35 {
            gray += 1
        }
    }
    return IconStats(red: red, gray: gray, area: width * height)
}

// The toolbar expands when reactions or other controls appear. Locate the
// camera-off icon first, then inspect the microphone button immediately to its
// left. A missing camera anchor leaves the state unknown instead of reading an
// unrelated gray toolbar icon as an unmuted microphone.
func toolbarMute(_ image: CGImage, scale: CGFloat, compact: Bool,
                 frameWidth: CGFloat) -> Bool? {
    let centerY = image.height - Int((compact ? 22.0 : 32.0) * scale)
    let radius = max(8, Int(14 * scale))
    let start = compact ? 130 : 180
    let end = min(Int(frameWidth * (compact ? 0.78 : 0.6)), compact ? 200 : 500)
    var groups: [(Int, Int)] = []
    var groupStart: Int? = nil
    var groupEnd = 0
    if start > end { return nil }
    for x in stride(from: start, through: end, by: 3) {
        let stats = iconStats(image, centerX: Int(CGFloat(x) * scale),
                              centerY: centerY, radius: radius)
        let red = stats.map { $0.red >= max(25, $0.area / 12) } ?? false
        if red {
            if groupStart == nil { groupStart = x }
            groupEnd = x
        } else if let first = groupStart {
            groups.append((first, groupEnd))
            groupStart = nil
        }
    }
    if let first = groupStart { groups.append((first, groupEnd)) }
    guard let camera = groups.last, camera.1 - camera.0 >= 6 else { return nil }
    let cameraCenter = (camera.0 + camera.1) / 2
    let micCenter = cameraCenter - (compact ? 65 : 70)
    return iconStats(image, centerX: Int(CGFloat(micCenter) * scale),
                     centerY: centerY, radius: radius)?.muted
}

func toolbarFixture(_ kind: String) -> CGImage? {
    let width = 1856
    let height = 1438
    var pixels = [UInt8](repeating: 255, count: width * height * 4)
    func square(_ centerX: Int, _ color: (UInt8, UInt8, UInt8)) {
        for y in 1359..<1389 {
            for x in (centerX - 15)..<(centerX + 15) {
                let index = (y * width + x) * 4
                pixels[index] = color.0
                pixels[index + 1] = color.1
                pixels[index + 2] = color.2
            }
        }
    }
    square(316, (95, 100, 110)) // gray chat at the old mic position
    square(552, kind == "muted" ? (235, 55, 65) : (95, 100, 110))
    if kind != "no-camera" { square(696, (235, 55, 65)) }
    let bitmap = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
    let context = CGContext(data: &pixels, width: width, height: height,
                            bitsPerComponent: 8, bytesPerRow: width * 4,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: bitmap)
    return context?.makeImage()
}

func visualSample() async -> (Bool?, String, Bool) {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)
        let windows = content.windows.filter {
            $0.owningApplication?.bundleIdentifier == "com.electron.lark.iron"
                && $0.frame.width >= 150 && $0.frame.height >= 100
        }
        var observed: Bool? = nil
        for window in windows {
            let filter = SCContentFilter(desktopIndependentWindow: window)
            let config = SCStreamConfiguration()
            let scale = CGFloat(filter.pointPixelScale)
            config.width = max(1, Int(filter.contentRect.width * scale))
            config.height = max(1, Int(filter.contentRect.height * scale))
            let image = try await SCScreenshotManager.captureImage(
                contentFilter: filter, configuration: config)
            let compact = window.frame.width / window.frame.height > 2.4
                && window.frame.height < 150
            guard let state = toolbarMute(image, scale: scale, compact: compact,
                                          frameWidth: window.frame.width) else { continue }
            if let previous = observed, previous != state {
                return (nil, "多个会议窗口的麦克风图标状态不一致", true)
            }
            observed = state
        }
        if let state = observed {
            return (state, state ? "红色划线麦克风图标" : "灰色麦克风图标", true)
        }
        return (nil, windows.isEmpty ? "未找到屏幕上的飞书会议窗口" : "未识别到麦克风图标",
                !windows.isEmpty)
    } catch {
        return (nil, "会议窗口截图不可用：\(error.localizedDescription)", false)
    }
}

func sample() async -> [String: Any] {
    let (axMuted, axDetail, axMeeting) = accessibilitySample()
    // A confirmed mute needs no screenshot. An apparent unmute does: the AX
    // tree can expose another meeting control before the currently visible one.
    let (iconMuted, iconDetail, iconMeeting): (Bool?, String, Bool) = axMuted == true
        ? (nil, "skipped", false) : await visualSample()
    if let muted = reconciledMute(axMuted, iconMuted) {
        return ["muted": muted, "source": "meeting_state",
                "detail": "AX: \(axDetail); icon: \(iconDetail)", "in_meeting": true,
                "ax_muted": axMuted as Any? ?? NSNull(),
                "icon_muted": iconMuted as Any? ?? NSNull()]
    }
    let inMeeting = axMeeting || iconMeeting
    return ["muted": NSNull(), "source": inMeeting ? "unknown" : "none",
            "detail": inMeeting ? iconDetail : axDetail + "；" + iconDetail,
            "in_meeting": inMeeting]
}

@main
struct HelperMain {
    static func main() async {
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--check-toolbar" {
            if let image = toolbarFixture(CommandLine.arguments[2]) {
                let result = toolbarMute(image, scale: 2, compact: false, frameWidth: 928)
                print(result.map(String.init) ?? "unknown")
            }
            return
        }
        if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--check-decision" {
            func parse(_ value: String) -> Bool? {
                if value == "true" { return true }
                if value == "false" { return false }
                return nil
            }
            let result = reconciledMute(parse(CommandLine.arguments[2]),
                                        parse(CommandLine.arguments[3]))
            print(result.map(String.init) ?? "unknown")
            return
        }
        if CommandLine.arguments.count == 6 && CommandLine.arguments[1] == "--check-icon" {
            let url = URL(fileURLWithPath: CommandLine.arguments[2])
            if let source = CGImageSourceCreateWithURL(url as CFURL, nil),
               let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
               let x = Int(CommandLine.arguments[3]),
               let y = Int(CommandLine.arguments[4]),
               let radius = Int(CommandLine.arguments[5]),
               let stats = iconStats(image, centerX: x, centerY: y, radius: radius) {
                print("red=\(stats.red) gray=\(stats.gray) area=\(stats.area) muted=\(String(describing: stats.muted))")
            }
            return
        }
        _ = NSApplication.shared
        while true {
            if let data = try? JSONSerialization.data(withJSONObject: await sample()),
               let line = String(data: data, encoding: .utf8) {
                print(line)
                fflush(stdout)
            }
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
    }
}
