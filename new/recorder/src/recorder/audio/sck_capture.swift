// 5.2 ScreenCaptureKit streaming helper (product capture layer, macOS only).
//
// Protocol (documented in sck_capture.py):
//   stdout: raw interleaved float32 PCM (binary; format confirmed by "format" event)
//   stderr: one JSON object per line:
//     {"event":"target","apps":[["bundle","name",pid],...]}
//     {"event":"started","mode":"audio-only"|"video-fallback"}
//     {"event":"format","rate":16000,"channels":1}
//     {"event":"error","message":"..."}
//     {"event":"stopped","reason":"stop-request"|"stream-error"|"no-target"|"start-failed"}
//   stdin:  the line "stop" (or EOF) requests a graceful stop; SIGTERM/SIGINT too.
//
// Exit codes: 0 stopped on request, 2 no matching target app, 1 other failure.
import Foundation
import ScreenCaptureKit
import CoreMedia
import AudioToolbox

struct Options {
    var rate = 16000
    var channels = 1
    var apps: [String] = []
    var displayWide = false
}

func parseOptions() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    while let a = args.first {
        args.removeFirst()
        switch a {
        case "--rate": if !args.isEmpty { o.rate = Int(args.removeFirst()) ?? 16000 }
        case "--channels": if !args.isEmpty { o.channels = Int(args.removeFirst()) ?? 1 }
        case "--app": if !args.isEmpty { o.apps.append(args.removeFirst()) }
        case "--display": o.displayWide = true
        default: break
        }
    }
    return o
}

let defaultHints = ["com.electron.lark", "lark", "feishu", "飞书"]

func emit(_ event: String, _ fields: [String: Any] = [:]) {
    var payload: [String: Any] = ["event": event]
    for (k, v) in fields { payload[k] = v }
    if let data = try? JSONSerialization.data(withJSONObject: payload),
       let line = String(data: data, encoding: .utf8) {
        FileHandle.standardError.write((line + "\n").data(using: .utf8)!)
    }
}

func emitErrorAndExit(_ message: String, _ code: Int32, reason: String) -> Never {
    emit("error", ["message": message])
    emit("stopped", ["reason": reason])
    exit(code)
}

final class VideoSink: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {}
}

final class AudioPump: NSObject, SCStreamOutput, SCStreamDelegate {
    let queue = DispatchQueue(label: "sck.audio")
    private let out = FileHandle.standardOutput
    let lock = NSLock()
    private var sawFormat = false
    private var formatRate = 0
    private var formatChannels = 0
    var streamFailure: String? = nil

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferGetNumSamples(sampleBuffer) > 0 else { return }
        var abl = AudioBufferList()
        var needed = 0
        var bb: CMBlockBuffer? = nil
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &needed,
            bufferListOut: &abl,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &bb)
        guard st == noErr, abl.mNumberBuffers >= 1, let data = abl.mBuffers.mData, abl.mBuffers.mDataByteSize > 0 else { return }
        if !sawFormat {
            if let fd = CMSampleBufferGetFormatDescription(sampleBuffer),
               let desc = CMAudioFormatDescriptionGetStreamBasicDescription(fd) {
                formatRate = Int(desc.pointee.mSampleRate)
                formatChannels = Int(desc.pointee.mChannelsPerFrame)
                sawFormat = true
                emit("format", ["rate": formatRate, "channels": formatChannels])
            }
        }
        out.write(Data(bytes: data, count: Int(abl.mBuffers.mDataByteSize)))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock(); streamFailure = error.localizedDescription; lock.unlock()
    }
}

@main
struct HelperMain {
    static func main() async {
        let opts = parseOptions()

        // Graceful stop triggers: stdin "stop"/EOF, SIGTERM, SIGINT.
        let stopSignal = DispatchSemaphore(value: 0)
        Thread.detachNewThread {
            while let line = readLine() {
                if line.trimmingCharacters(in: .whitespaces) == "stop" { break }
            }
            stopSignal.signal()
        }
        for sig in [SIGTERM, SIGINT] {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { stopSignal.signal() }
            src.resume()
            signal(sig, SIG_IGN)
        }

        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                emitErrorAndExit("no display found", 2, reason: "no-target")
            }

            let hints = opts.apps.isEmpty ? defaultHints : opts.apps.map { $0.lowercased() }
            let matches = content.applications.filter { a -> Bool in
                let bid = a.bundleIdentifier.lowercased()
                let nm = a.applicationName.lowercased()
                return hints.contains { bid.contains($0) || nm.contains($0) }
            }
            let filter: SCContentFilter
            if opts.displayWide || matches.isEmpty {
                if !opts.displayWide && matches.isEmpty {
                    emitErrorAndExit("no target application matched \(hints.joined(separator: ","))",
                                     2, reason: "no-target")
                }
                emit("target", ["apps": [["display", "*", 0]]])
                filter = SCContentFilter(display: display, excludingWindows: [])
            } else {
                emit("target", ["apps": matches.map { [$0.bundleIdentifier, $0.applicationName, Int($0.processID)] }])
                filter = SCContentFilter(display: display, including: matches, exceptingWindows: [])
            }

            let pump = AudioPump()
            var stream: SCStream? = nil
            var mode = "audio-only"
            var attempt = 1
            while true {
                let cfg = SCStreamConfiguration()
                cfg.capturesAudio = true
                cfg.sampleRate = opts.rate
                cfg.channelCount = opts.channels
                if attempt == 2 {
                    mode = "video-fallback"
                    cfg.width = 2
                    cfg.height = 2
                }
                let s = SCStream(filter: filter, configuration: cfg, delegate: pump)
                do {
                    try s.addStreamOutput(pump, type: .audio, sampleHandlerQueue: pump.queue)
                    if attempt == 2 {
                        try s.addStreamOutput(VideoSink(), type: .screen, sampleHandlerQueue: DispatchQueue(label: "sck.video"))
                    }
                    try await s.startCapture()
                    stream = s
                    break
                } catch {
                    if attempt == 1 {
                        attempt = 2
                        continue
                    }
                    emitErrorAndExit("startCapture failed: \(error)", 1, reason: "start-failed")
                }
            }
            emit("started", ["mode": mode])

            stopSignal.wait()

            var failure: String? = nil
            pump.lock.lock(); failure = pump.streamFailure; pump.lock.unlock()
            do { try await stream!.stopCapture() } catch { /* already stopped */ }
            if let failure = failure {
                emitErrorAndExit(failure, 1, reason: "stream-error")
            }
            emit("stopped", ["reason": "stop-request"])
            exit(0)
        } catch {
            emitErrorAndExit("\(error)", 1, reason: "start-failed")
        }
    }
}
