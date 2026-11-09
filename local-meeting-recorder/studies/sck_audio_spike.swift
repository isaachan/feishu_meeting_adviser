// 5.1 spike: ScreenCaptureKit per-app audio capture (Feishu/Lark).
// Usage:
//   sck_spike --list
//   sck_spike [--app substr] [--duration 10] [--out out.wav] [--rate 16000] [--channels 1]
import Foundation
import ScreenCaptureKit
import CoreMedia
import AudioToolbox

struct Options {
    var listOnly = false
    var appQuery = ""
    var duration: Double = 10
    var out = "sck_spike.wav"
    var rate = 0
    var channels = 0
    var withVideo = false
    var displayWide = false
}

func parseOptions() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    while let a = args.first {
        args.removeFirst()
        switch a {
        case "--list": o.listOnly = true
        case "--app": if !args.isEmpty { o.appQuery = args.removeFirst() }
        case "--duration": if !args.isEmpty { o.duration = Double(args.removeFirst()) ?? 10 }
        case "--out": if !args.isEmpty { o.out = args.removeFirst() }
        case "--rate": if !args.isEmpty { o.rate = Int(args.removeFirst()) ?? 0 }
        case "--channels": if !args.isEmpty { o.channels = Int(args.removeFirst()) ?? 0 }
        case "--with-video": o.withVideo = true
        case "--display": o.displayWide = true
        default: break
        }
    }
    return o
}

final class VideoSink: NSObject, SCStreamOutput {
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {}
}

final class AudioCollector: NSObject, SCStreamOutput, SCStreamDelegate {
    let lock = NSLock()
    private var pcm = Data()
    private var asbd = AudioStreamBasicDescription()
    private var sawFormat = false
    private var buffers = 0
    private var lastError: String?

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
        guard st == noErr, abl.mNumberBuffers >= 1, let data = abl.mBuffers.mData else {
            lock.lock(); lastError = "audio extraction failed, status \(st)"; lock.unlock()
            return
        }
        lock.lock()
        buffers += 1
        if !sawFormat {
            if let fd = CMSampleBufferGetFormatDescription(sampleBuffer),
               let desc = CMAudioFormatDescriptionGetStreamBasicDescription(fd) {
                asbd = desc.pointee
                sawFormat = true
            }
        }
        pcm.append(Data(bytes: data, count: Int(abl.mBuffers.mDataByteSize)))
        lock.unlock()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock(); lastError = "stream stopped: \(error.localizedDescription)"; lock.unlock()
    }

    func take() -> (Data, AudioStreamBasicDescription?, String?, Int) {
        lock.lock(); defer { lock.unlock() }
        return (pcm, sawFormat ? asbd : nil, lastError, buffers)
    }
}

func le32(_ v: UInt32) -> Data { var x = v.littleEndian; return withUnsafeBytes(of: &x) { Data($0) } }
func le16(_ v: UInt16) -> Data { var x = v.littleEndian; return withUnsafeBytes(of: &x) { Data($0) } }
func tag(_ s: String) -> Data { Data(s.utf8) }

func writeWav(_ pcm: Data, asbd: AudioStreamBasicDescription?, to path: String) throws {
    let floatCount = pcm.count / 4
    var samples = [Int16](repeating: 0, count: floatCount)
    pcm.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        let fp = raw.bindMemory(to: Float32.self)
        for i in 0..<floatCount {
            let v = max(-1.0, min(1.0, fp[i]))
            samples[i] = Int16(v * 32766.0)
        }
    }
    let rate = Int(asbd?.mSampleRate ?? 48000)
    let channels = Int(asbd?.mChannelsPerFrame ?? 1)
    var out = Data()
    out += tag("RIFF"); out += le32(UInt32(36 + samples.count * 2)); out += tag("WAVE")
    out += tag("fmt "); out += le32(16); out += le16(1); out += le16(UInt16(channels))
    out += le32(UInt32(rate)); out += le32(UInt32(rate * channels * 2)); out += le16(UInt16(channels * 2)); out += le16(16)
    out += tag("data"); out += le32(UInt32(samples.count * 2))
    samples.withUnsafeBytes { out += Data($0) }
    try out.write(to: URL(fileURLWithPath: path))
}

func printRms(_ pcm: Data, rate: Int, channels: Int) {
    let fl = pcm.withUnsafeBytes { Array($0.bindMemory(to: Float32.self)) }
    guard rate > 0, channels > 0, !fl.isEmpty else { return }
    let perSec = rate * channels
    var i = 0
    var sec = 0
    var peak: Float = 0
    while i < fl.count {
        let end = min(i + perSec, fl.count)
        var sum: Float = 0
        for j in i..<end {
            let a = abs(fl[j])
            sum += a * a
            if a > peak { peak = a }
        }
        let rms = (sum / Float(end - i)).squareRoot()
        let db = 20 * log10(max(rms, 1e-9))
        print(String(format: "sec %02d: rms %7.1f dB", sec, db))
        sec += 1
        i = end
    }
    print(String(format: "peak amplitude: %.3f", peak))
}

@main
struct SpikeMain {
    static func main() async {
        let opts = parseOptions()
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            let apps = content.applications
            if opts.listOnly {
                if apps.isEmpty {
                    FileHandle.standardError.write("no applications visible — Screen Recording permission may be missing\n".data(using: .utf8)!)
                    exit(3)
                }
                print("bundle_id\tname\tpid")
                for a in apps { print("\(a.bundleIdentifier)\t\(a.applicationName)\t\(a.processID)") }
                return
            }

            let query = opts.appQuery.lowercased()
            let hints = ["com.electron.lark", "lark", "feishu", "飞书"]
            let matches = apps.filter { a -> Bool in
                let bid = a.bundleIdentifier.lowercased()
                let nm = a.applicationName.lowercased()
                if !query.isEmpty { return bid.contains(query) || nm.contains(query) }
                return hints.contains { bid.contains($0) || nm.contains($0) }
            }
            guard !matches.isEmpty else {
                FileHandle.standardError.write("ERROR: target app not found (try --list / --app <substr>)\n".data(using: .utf8)!)
                exit(2)
            }
            for a in matches { print("capturing app: \(a.bundleIdentifier) (\(a.applicationName)) pid=\(a.processID)") }

            guard let display = content.displays.first else {
                FileHandle.standardError.write("ERROR: no display found\n".data(using: .utf8)!)
                exit(2)
            }
            let filter: SCContentFilter
            if opts.displayWide {
                print("mode: display-wide (all system audio)")
                filter = SCContentFilter(display: display, excludingWindows: [])
            } else {
                filter = SCContentFilter(display: display, including: matches, exceptingWindows: [])
            }
            let collector = AudioCollector()
            var stream: SCStream? = nil
            var attempt = opts.withVideo ? 2 : 1
            while true {
                let cfg = SCStreamConfiguration()
                cfg.capturesAudio = true
                if attempt == 2 { cfg.width = 2; cfg.height = 2 }
                if opts.rate > 0 { cfg.sampleRate = opts.rate }
                if opts.channels > 0 { cfg.channelCount = opts.channels }
                let s = SCStream(filter: filter, configuration: cfg, delegate: collector)
                do {
                    try s.addStreamOutput(collector, type: .audio, sampleHandlerQueue: DispatchQueue(label: "sck.audio"))
                    if attempt == 2 {
                        try s.addStreamOutput(VideoSink(), type: .screen, sampleHandlerQueue: DispatchQueue(label: "sck.video"))
                    }
                    try await s.startCapture()
                    stream = s
                    print(attempt == 1 ? "stream started (audio-only)" : "stream started (2x2 video fallback)")
                    break
                } catch {
                    if attempt == 1 {
                        print("audio-only start failed: \(error) — retrying with video fallback")
                        attempt = 2
                        continue
                    }
                    throw error
                }
            }

            try await Task.sleep(nanoseconds: UInt64(opts.duration * 1_000_000_000))
            try await stream!.stopCapture()

            let (pcm, asbd, err, buffers) = collector.take()
            if let err = err { print("warning: \(err)") }
            print("audio buffers: \(buffers), bytes: \(pcm.count)")
            guard let asbd = asbd else {
                FileHandle.standardError.write("ERROR: no audio format observed — app produced no audio\n".data(using: .utf8)!)
                exit(3)
            }
            let rate = Int(asbd.mSampleRate)
            let channels = Int(asbd.mChannelsPerFrame)
            let frames = pcm.count / 4 / max(channels, 1)
            print(String(format: "format: %.0f Hz, %d ch, interleaved float32, %d frames (%.1f s)",
                         asbd.mSampleRate, channels, frames, Double(frames) / Double(max(rate, 1))))
            printRms(pcm, rate: rate, channels: channels)
            try writeWav(pcm, asbd: asbd, to: opts.out)
            print("wrote \(opts.out)")
            if pcm.isEmpty {
                FileHandle.standardError.write("NOTE: captured nothing — make sure Feishu is actually playing audio\n".data(using: .utf8)!)
                exit(3)
            }
        } catch {
            FileHandle.standardError.write("ERROR: \(error)\n".data(using: .utf8)!)
            let msg = "\(error)".lowercased()
            if msg.contains("-3801") || msg.contains("denied") || msg.contains("not authorized") || msg.contains("权限") {
                FileHandle.standardError.write("hint: grant Screen Recording permission to the terminal app (System Settings > Privacy & Security > Screen Recording), restart the terminal, retry\n".data(using: .utf8)!)
            }
            exit(1)
        }
    }
}
