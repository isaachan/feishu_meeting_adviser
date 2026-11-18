import AppKit
import ApplicationServices
import Foundation

// Read only the microphone action of a Feishu meeting window. The action is
// the opposite of the current state: "打开麦克风" means currently muted.
let meetingBundles = ["com.electron.lark.iron", "com.electron.lark"]
let meetingTitles = ["飞书会议", "Feishu Meeting", "Lark Meeting"]
let mutedActions = ["打开麦克风", "取消静音", "Unmute", "Turn on microphone"]
let liveActions = ["关闭麦克风", "静音", "Mute", "Turn off microphone"]

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
    while index < queue.count && index < 600 {
        let (element, depth) = queue[index]
        index += 1
        let role = stringAttribute(element, kAXRoleAttribute)
        if role == kAXButtonRole as String {
            for key in [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute] {
                let label = stringAttribute(element, key)
                if let muted = actionState(label) {
                    return (muted, label)
                }
            }
        }
        if depth < 12 {
            queue.append(contentsOf: elements(element, kAXChildrenAttribute).map { ($0, depth + 1) })
        }
    }
    return nil
}

func sample() -> [String: Any] {
    guard AXIsProcessTrusted() else {
        return ["muted": NSNull(), "source": "permission", "detail": "需要辅助功能权限", "in_meeting": false]
    }
    var foundMeeting = false
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
                    return ["muted": muted, "source": "meeting_button", "detail": label, "in_meeting": true]
                }
            }
        }
    }
    return ["muted": NSNull(), "source": foundMeeting ? "unknown" : "none",
            "detail": foundMeeting ? "未找到可用的麦克风按钮" : "未找到飞书会议窗口",
            "in_meeting": foundMeeting]
}

_ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
while true {
    if let data = try? JSONSerialization.data(withJSONObject: sample(), options: [.fragmentsAllowed]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
    Thread.sleep(forTimeInterval: 0.3)
}
