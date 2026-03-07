@preconcurrency import AVFoundation
import Foundation
import VMicSharedC

private let targetSampleRate = VMIC_DEFAULT_SAMPLE_RATE
private let targetChannels = AVAudioChannelCount(VMIC_DEFAULT_CHANNELS)
private let defaultCapacityFrames = UInt64(targetSampleRate * 30.0)

enum CLIError: Error, CustomStringConvertible {
    case usage(String)
    case message(String)

    var description: String {
        switch self {
        case .usage(let message), .message(let message):
            return message
        }
    }
}

struct SharedStatus: Encodable {
    let path: String
    let snapshot: SnapshotPayload?
}

struct SnapshotPayload: Encodable {
    let version: UInt32
    let state: String
    let sampleRate: Double
    let channels: UInt32
    let capacityFrames: UInt64
    let queuedFrames: UInt64
    let queuedSeconds: Double
    let generation: UInt32
}

struct ActionPayload: Encodable {
    let action: String
    let path: String
    let snapshot: SnapshotPayload?
    let note: String?
}

final class ConverterInputBox: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    var consumed = false

    init(buffer: AVAudioPCMBuffer) {
        self.buffer = buffer
    }
}

@main
struct VMicCLI {
    static func main() {
        do {
            try run(Array(CommandLine.arguments.dropFirst()))
        } catch {
            fputs("vmicctl: \(error)\n", stderr)
            exit(1)
        }
    }

    static func run(_ args: [String]) throws {
        guard let command = args.first else {
            throw CLIError.usage(usage())
        }

        switch command {
        case "path":
            try printJSON(["path": try sharedPath()])
        case "status":
            try printStatus()
        case "clear":
            try clearSharedBuffer()
        case "prime-sine":
            try primeSine(arguments: Array(args.dropFirst()))
        case "enqueue":
            try enqueueFile(arguments: Array(args.dropFirst()))
        case "help", "--help", "-h":
            print(usage())
        default:
            throw CLIError.usage(usage())
        }
    }

    static func usage() -> String {
        """
        用法：
          vox-vmicctl path
          vox-vmicctl status
          vox-vmicctl clear
          vox-vmicctl prime-sine [--seconds 2.0] [--frequency 440]
          vox-vmicctl enqueue <audio-file>

        说明：
          - 当前 MVP 只负责把音频放进共享环形缓冲区。
          - 真正把它暴露成系统麦克风，还要配合 `driver/` 里的 AudioServerPlugIn。
        """
    }

    static func sharedPath() throws -> String {
        var buffer = [CChar](repeating: 0, count: Int(PATH_MAX))
        let result = vmic_default_shared_path(&buffer, buffer.count)
        guard result == 0 else {
            throw CLIError.message("无法解析共享路径：\(String(cString: strerror(Int32(result))))")
        }
        let prefix = buffer.prefix { $0 != 0 }
        return String(decoding: prefix.map(UInt8.init(bitPattern:)), as: UTF8.self)
    }

    static func writer(capacityFrames: UInt64) throws -> OpaquePointer {
        var err: Int32 = 0
        guard let raw = vmic_writer_open(nil, capacityFrames, UInt32(VMIC_DEFAULT_CHANNELS), targetSampleRate, &err) else {
            throw CLIError.message("打开共享缓冲失败：\(String(cString: strerror(err)))")
        }
        return raw
    }

    static func readerIfAvailable() throws -> OpaquePointer? {
        var err: Int32 = 0
        guard let raw = vmic_reader_open(nil, &err) else {
            if err == ENOENT {
                return nil
            }
            throw CLIError.message("读取共享缓冲失败：\(String(cString: strerror(err)))")
        }
        return raw
    }

    static func printStatus() throws {
        let path = try sharedPath()
        guard let rawReader = try readerIfAvailable() else {
            try printJSON(SharedStatus(path: path, snapshot: nil))
            return
        }
        defer { vmic_reader_close(rawReader) }

        var snapshot = VMicSnapshot()
        let status = vmic_reader_snapshot(rawReader, &snapshot)
        guard status == 0 else {
            throw CLIError.message("读取状态失败：\(String(cString: strerror(Int32(status))))")
        }

        try printJSON(SharedStatus(path: path, snapshot: snapshotPayload(from: snapshot)))
    }

    static func clearSharedBuffer() throws {
        let rawWriter = try writer(capacityFrames: defaultCapacityFrames)
        defer { vmic_writer_close(rawWriter) }

        let status = vmic_writer_reset(rawWriter)
        guard status == 0 else {
            throw CLIError.message("清空缓冲失败：\(String(cString: strerror(Int32(status))))")
        }
        try printAction(name: "clear", note: "buffer reset")
    }

    static func primeSine(arguments: [String]) throws {
        var seconds = 2.0
        var frequency = 440.0
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--seconds":
                index += 1
                guard index < arguments.count, let value = Double(arguments[index]), value > 0 else {
                    throw CLIError.usage("prime-sine 需要正数 --seconds")
                }
                seconds = value
            case "--frequency":
                index += 1
                guard index < arguments.count, let value = Double(arguments[index]), value > 0 else {
                    throw CLIError.usage("prime-sine 需要正数 --frequency")
                }
                frequency = value
            default:
                throw CLIError.usage(usage())
            }
            index += 1
        }

        let frameCount = Int(seconds * targetSampleRate)
        var samples = [Float](repeating: 0, count: frameCount)
        for frame in 0..<frameCount {
            let phase = (Double(frame) / targetSampleRate) * frequency * .pi * 2.0
            samples[frame] = Float(sin(phase) * 0.2)
        }

        try writeSamples(samples)
        try printAction(name: "prime-sine", note: "frames=\(frameCount) frequency=\(frequency)")
    }

    static func enqueueFile(arguments: [String]) throws {
        guard let input = arguments.first, arguments.count == 1 else {
            throw CLIError.usage(usage())
        }

        let url = URL(fileURLWithPath: input)
        let samples = try decodeToMonoFloat32(url: url)
        try writeSamples(samples)
        try printAction(name: "enqueue", note: "input=\(url.path) frames=\(samples.count)")
    }

    static func snapshotPayload(from snapshot: VMicSnapshot) -> SnapshotPayload {
        SnapshotPayload(
            version: snapshot.version,
            state: String(cString: vmic_state_name(snapshot.state)),
            sampleRate: snapshot.sampleRate,
            channels: snapshot.channels,
            capacityFrames: snapshot.capacityFrames,
            queuedFrames: snapshot.queuedFrames,
            queuedSeconds: Double(snapshot.queuedFrames) / snapshot.sampleRate,
            generation: snapshot.generation
        )
    }

    static func currentStatus() throws -> SharedStatus {
        let path = try sharedPath()
        guard let rawReader = try readerIfAvailable() else {
            return SharedStatus(path: path, snapshot: nil)
        }
        defer { vmic_reader_close(rawReader) }

        var snapshot = VMicSnapshot()
        let status = vmic_reader_snapshot(rawReader, &snapshot)
        guard status == 0 else {
            throw CLIError.message("读取状态失败：\(String(cString: strerror(Int32(status))))")
        }
        return SharedStatus(path: path, snapshot: snapshotPayload(from: snapshot))
    }

    static func printAction(name: String, note: String? = nil) throws {
        let status = try currentStatus()
        try printJSON(ActionPayload(action: name, path: status.path, snapshot: status.snapshot, note: note))
    }

    static func printJSON<T: Encodable>(_ value: T) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(value)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }

    static func writeSamples(_ samples: [Float]) throws {
        let capacityFrames = max(UInt64(samples.count) + UInt64(targetSampleRate * 2.0), defaultCapacityFrames)
        let rawWriter = try writer(capacityFrames: capacityFrames)
        defer { vmic_writer_close(rawWriter) }

        let resetStatus = vmic_writer_reset(rawWriter)
        guard resetStatus == 0 else {
            throw CLIError.message("重置缓冲失败：\(String(cString: strerror(Int32(resetStatus))))")
        }

        let enqueueStatus = samples.withUnsafeBufferPointer { pointer in
            vmic_writer_enqueue(rawWriter, pointer.baseAddress, UInt64(pointer.count))
        }
        guard enqueueStatus == 0 else {
            throw CLIError.message("写入缓冲失败：\(String(cString: strerror(Int32(enqueueStatus))))")
        }
    }

    static func decodeToMonoFloat32(url: URL) throws -> [Float] {
        let file = try AVAudioFile(forReading: url)
        let sourceFormat = file.processingFormat
        guard let targetFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                               sampleRate: targetSampleRate,
                                               channels: targetChannels,
                                               interleaved: false),
              let converter = AVAudioConverter(from: sourceFormat, to: targetFormat) else {
            throw CLIError.message("无法创建音频转换器")
        }

        let sourceCapacity: AVAudioFrameCount = 4096
        var samples: [Float] = []

        while true {
            guard let sourceBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: sourceCapacity) else {
                throw CLIError.message("无法创建源缓冲")
            }
            try file.read(into: sourceBuffer, frameCount: sourceCapacity)
            if sourceBuffer.frameLength == 0 {
                break
            }

            let ratio = targetFormat.sampleRate / sourceFormat.sampleRate
            let outputCapacity = AVAudioFrameCount(ceil(Double(sourceBuffer.frameLength) * ratio)) + 64
            guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outputCapacity) else {
                throw CLIError.message("无法创建输出缓冲")
            }

            var error: NSError?
            let box = ConverterInputBox(buffer: sourceBuffer)
            let status = converter.convert(to: outputBuffer, error: &error) { _, outStatus in
                if box.consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                box.consumed = true
                outStatus.pointee = .haveData
                return box.buffer
            }

            if let error {
                throw error
            }
            if status == .error {
                throw CLIError.message("音频转换失败")
            }

            let frames = Int(outputBuffer.frameLength)
            if frames > 0, let channel = outputBuffer.floatChannelData?.pointee {
                samples.append(contentsOf: UnsafeBufferPointer(start: channel, count: frames))
            }
        }

        guard !samples.isEmpty else {
            throw CLIError.message("音频文件为空：\(url.path)")
        }
        return samples
    }
}
