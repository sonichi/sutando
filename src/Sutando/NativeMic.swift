import Foundation
import AVFoundation

// PARKED 2026-04-08: ⌃V is back on the web client path while we figure out
// the voice-processing IO format problem. Symptoms: with setVoiceProcessingEnabled(true),
// engine start fails with -10875 (PerformCommand outputNode kAUInitialize) regardless
// of whether the player connects to mainMixerNode (44.1k stereo) or outputNode
// (queried bus-0 input format). Without voice processing the audio works but
// produces echo on speakers. Future work: try a stand-alone AudioUnit graph
// with kAudioUnitSubType_VoiceProcessingIO directly, or use AVAudioSession-style
// configuration. Keep this file — main.swift no longer calls it but the build
// still includes it so the API stays warm.
//
// Native microphone capture + playback ↔ WebSocket bridge for Sutando voice agent.
//
// Why this exists: the web client mic path requires a Chrome user-gesture
// to unlock AudioContext. After a tab reload or long backgrounding, the
// gesture grant is lost and ⌃V silently fails. This native path bypasses
// Chrome entirely — AVAudioEngine has no gesture restriction, so once
// ⌃V is wired here it works always.
//
// Wire format (matches src/web-client.ts on ws://localhost:9900/):
//   send: raw Int16 PCM mono 16 kHz, WebSocket binary frames
//   recv: raw Int16 PCM mono at session.config.audioFormat.outputSampleRate
//         (default 24 kHz), WebSocket binary frames + JSON text frames

final class NativeMic: NSObject {
    static let shared = NativeMic()

    private let targetSampleRate: Double = 16000
    private let serverURL = URL(string: "ws://localhost:9900/")!

    // Single engine for both input and output — required so the voice
    // processing IO unit can do acoustic echo cancellation (AEC needs to
    // see what's being played to subtract it from the mic).
    private var engine: AVAudioEngine?
    private var converter: AVAudioConverter?
    private var converterOutputFormat: AVAudioFormat?

    // Playback
    private var playerNode: AVAudioPlayerNode?
    private var playerFormat: AVAudioFormat?
    private var outputSampleRate: Double = 24000  // updated from session.config

    // WebSocket
    private var ws: URLSessionWebSocketTask?
    private var session: URLSession?

    private(set) var isRunning: Bool = false
    private var bytesSent: Int = 0
    private var framesSent: Int = 0
    private var bytesRecv: Int = 0
    private var chunksRecv: Int = 0

    func toggle() {
        if isRunning { stop() } else { start() }
    }

    func start() {
        guard !isRunning else { return }
        NSLog("NativeMic: start")
        bytesSent = 0
        framesSent = 0
        bytesRecv = 0
        chunksRecv = 0

        // 1. Open WebSocket first so the server is ready before audio flows.
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 5
        let s = URLSession(configuration: cfg)
        let task = s.webSocketTask(with: serverURL)
        task.resume()
        self.session = s
        self.ws = task
        receiveLoop()

        // 2. Build a single engine that does input + output through the
        //    voice-processing IO unit. AEC + AGC + noise suppression come
        //    for free, mirroring what Chrome's getUserMedia does.
        let engine = AVAudioEngine()
        let input = engine.inputNode

        // Enable voice processing BEFORE touching format / starting the engine.
        // Failure isn't fatal — some hardware doesn't support it; in that case
        // the user gets the same path as before (echo possible).
        do {
            try input.setVoiceProcessingEnabled(true)
            NSLog("NativeMic: voice processing enabled (AEC/AGC/NS active)")
        } catch {
            NSLog("NativeMic: voice processing not available: \(error)")
        }

        // Attach playback node to the SAME engine so AEC can subtract output.
        // With voice processing enabled, the output node's input format is
        // constrained to a specific mono rate (typically matches input). We
        // must connect the player directly to outputNode using THAT format,
        // bypassing the mainMixer (which defaults to hardware 44.1k stereo
        // and causes -10875 when voice processing is on).
        let player = AVAudioPlayerNode()
        engine.attach(player)
        let outNodeFormat = engine.outputNode.inputFormat(forBus: 0)
        NSLog("NativeMic: outputNode wants \(outNodeFormat.sampleRate)Hz \(outNodeFormat.channelCount)ch")
        engine.connect(player, to: engine.outputNode, format: outNodeFormat)
        self.playerNode = player
        self.playerFormat = outNodeFormat

        // Capture format. With voice processing on, inputNode's outputFormat
        // is forced to a fixed mono Float32 rate (typically 24 kHz on macOS).
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0 else {
            NSLog("NativeMic: input format invalid (sampleRate=0). Mic permission denied?")
            stopWebSocket()
            return
        }

        guard let outFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            NSLog("NativeMic: failed to build output format")
            stopWebSocket()
            return
        }
        self.converterOutputFormat = outFormat
        self.converter = AVAudioConverter(from: inputFormat, to: outFormat)

        let bufSize: AVAudioFrameCount = 2048
        input.installTap(onBus: 0, bufferSize: bufSize, format: inputFormat) { [weak self] buffer, _ in
            self?.handleInputBuffer(buffer)
        }

        do {
            try engine.start()
            player.play()
            self.engine = engine
            self.isRunning = true
            NSLog("NativeMic: engine started, input=\(inputFormat.sampleRate)Hz \(inputFormat.channelCount)ch → 16000Hz mono Int16; playback=\(outputSampleRate)Hz mono Float32")
        } catch {
            NSLog("NativeMic: engine start failed: \(error)")
            input.removeTap(onBus: 0)
            stopWebSocket()
        }
    }

    func stop() {
        guard isRunning else { return }
        NSLog("NativeMic: stop (sent \(framesSent) frames/\(bytesSent)B, recv \(chunksRecv) chunks/\(bytesRecv)B)")
        if let player = playerNode {
            player.stop()
        }
        if let engine = engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        playerNode = nil
        playerFormat = nil
        converter = nil
        converterOutputFormat = nil
        stopWebSocket()
        isRunning = false
    }

    private func stopWebSocket() {
        ws?.cancel(with: .goingAway, reason: nil)
        ws = nil
        session?.invalidateAndCancel()
        session = nil
    }

    // MARK: - Playback

    private func rebuildPlaybackForRate(_ rate: Double) {
        guard rate > 0, rate != outputSampleRate else { return }
        // Single-engine refactor: we can't safely tear down only the player
        // mid-call (it shares the engine with the input AEC pipeline). Just
        // record the rate; if it ever differs from default 24 kHz this is a
        // design corner to revisit. In practice bodhi uses 24 kHz output.
        NSLog("NativeMic: server requested playback rate \(rate) (current \(outputSampleRate)) — engine restart deferred")
        outputSampleRate = rate
    }

    private func playInt16PCM(_ data: Data) {
        guard let player = playerNode, let format = playerFormat else { return }
        let sampleCount = data.count / MemoryLayout<Int16>.size
        guard sampleCount > 0 else { return }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(sampleCount)) else {
            return
        }
        buffer.frameLength = AVAudioFrameCount(sampleCount)
        guard let channel = buffer.floatChannelData?[0] else { return }

        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let int16Ptr = raw.bindMemory(to: Int16.self)
            for i in 0..<sampleCount {
                channel[i] = Float32(int16Ptr[i]) / 32768.0
            }
        }

        player.scheduleBuffer(buffer, completionHandler: nil)
    }

    // MARK: - Input pipeline (unchanged)

    private func handleInputBuffer(_ buffer: AVAudioPCMBuffer) {
        guard let converter = converter,
              let outFormat = converterOutputFormat,
              let ws = ws else { return }

        let inRate = buffer.format.sampleRate
        let outFrameCapacity = AVAudioFrameCount(
            Double(buffer.frameLength) * targetSampleRate / inRate + 1024
        )
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outFrameCapacity) else {
            return
        }

        var error: NSError?
        var supplied = false
        let status = converter.convert(to: outBuffer, error: &error) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }

        if status == .error || error != nil {
            NSLog("NativeMic: convert error: \(error?.localizedDescription ?? "?")")
            return
        }
        guard outBuffer.frameLength > 0,
              let int16Ptr = outBuffer.int16ChannelData?[0] else { return }

        let byteCount = Int(outBuffer.frameLength) * MemoryLayout<Int16>.size
        let data = Data(bytes: int16Ptr, count: byteCount)
        ws.send(.data(data)) { err in
            if let err = err {
                NSLog("NativeMic: ws.send error: \(err.localizedDescription)")
            }
        }
        bytesSent += byteCount
        framesSent += 1
    }

    // MARK: - Receive

    private func receiveLoop() {
        guard let ws = ws else { return }
        ws.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                NSLog("NativeMic: ws.receive error: \(err.localizedDescription)")
                return
            case .success(let message):
                switch message {
                case .data(let data):
                    self.bytesRecv += data.count
                    self.chunksRecv += 1
                    if self.chunksRecv <= 5 {
                        NSLog("NativeMic: recv audio #\(self.chunksRecv) \(data.count)B")
                    }
                    self.playInt16PCM(data)
                case .string(let text):
                    self.handleServerJSON(text)
                @unknown default:
                    break
                }
                self.receiveLoop()
            }
        }
    }

    private func handleServerJSON(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        if let type = obj["type"] as? String, type == "session.config",
           let audioFormat = obj["audioFormat"] as? [String: Any],
           let outRate = audioFormat["outputSampleRate"] as? Double {
            DispatchQueue.main.async { [weak self] in
                self?.rebuildPlaybackForRate(outRate)
            }
        }
        // Other JSON messages (transcript, turn.end, etc) are UI-only — ignore.
    }
}
