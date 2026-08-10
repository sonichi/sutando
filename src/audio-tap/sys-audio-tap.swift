// sys-audio-tap <out.wav> — records system audio via a Core Audio process tap
// until SIGINT/SIGTERM. Uses AudioDeviceCreateIOProcIDWithBlock — AVAudioEngine no-ops on a tap aggregate.

import AVFoundation
import CoreAudio
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("sys-audio-tap: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count == 2 else { fail("usage: sys-audio-tap <out.wav>") }
let outURL = URL(fileURLWithPath: CommandLine.arguments[1])

// -- 1. Create the process tap (all processes, unmuted, private) -------------
let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDesc.name = "sutando-recorder-tap"
tapDesc.muteBehavior = .unmuted
tapDesc.isPrivate = true

var tapID = AudioObjectID(kAudioObjectUnknown)
var err = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard err == noErr, tapID != kAudioObjectUnknown else {
    fail("AudioHardwareCreateProcessTap failed (\(err)) — likely missing audio-capture permission (TCC)")
}
defer { AudioHardwareDestroyProcessTap(tapID) }

// -- 2. Read the tap's stream format ----------------------------------------
var fmtAddr = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyFormat,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
var asbd = AudioStreamBasicDescription()
var asbdSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
err = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &asbdSize, &asbd)
guard err == noErr, let tapFormat = AVAudioFormat(streamDescription: &asbd) else {
    fail("could not read tap format (\(err))")
}

// -- 3. Default output device UID (main sub-device of the aggregate) --------
func defaultOutputUID() -> String {
    var devID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devID) == noErr
    else { fail("no default output device") }
    var uid: CFString = "" as CFString
    var uidSize = UInt32(MemoryLayout<CFString>.size)
    var uidAddr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(devID, &uidAddr, 0, nil, &uidSize, &uid) == noErr
    else { fail("could not read output device UID") }
    return uid as String
}

// -- 4. Private aggregate device containing the tap --------------------------
let aggUID = UUID().uuidString
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "sutando-recorder-agg",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceMainSubDeviceKey: defaultOutputUID(),
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [
        [kAudioSubDeviceUIDKey: defaultOutputUID()]
    ],
    kAudioAggregateDeviceTapListKey: [
        [
            kAudioSubTapDriftCompensationKey: true,
            kAudioSubTapUIDKey: tapDesc.uuid.uuidString,
        ]
    ],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
err = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
guard err == noErr, aggID != kAudioObjectUnknown else { fail("aggregate creation failed (\(err))") }
defer { AudioHardwareDestroyAggregateDevice(aggID) }

// -- 5. WAV writer + IO proc --------------------------------------------------
let wavSettings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: tapFormat.sampleRate,
    AVNumberOfChannelsKey: tapFormat.channelCount,
    AVLinearPCMBitDepthKey: 16,
    AVLinearPCMIsFloatKey: false,
    AVLinearPCMIsBigEndianKey: false,
]
let file: AVAudioFile
do {
    file = try AVAudioFile(forWriting: outURL, settings: wavSettings,
                           commonFormat: tapFormat.commonFormat,
                           interleaved: tapFormat.isInterleaved)
} catch { fail("cannot open \(outURL.path): \(error)") }

var framesWritten: Int64 = 0
var ioProcID: AudioDeviceIOProcID?
err = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggID, nil) { _, inInputData, _, _, _ in
    guard let pcm = AVAudioPCMBuffer(pcmFormat: tapFormat,
                                     bufferListNoCopy: inInputData, deallocator: nil),
          pcm.frameLength > 0 else { return }
    do {
        try file.write(from: pcm)
        framesWritten += Int64(pcm.frameLength)
    } catch { /* keep the callback realtime-safe; a failed write is dropped */ }
}
guard err == noErr, let procID = ioProcID else { fail("IO proc creation failed (\(err))") }

err = AudioDeviceStart(aggID, procID)
guard err == noErr else { fail("AudioDeviceStart failed (\(err))") }

// -- 6. Run until SIGINT/SIGTERM ---------------------------------------------
let stop = DispatchSemaphore(value: 0)
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { stop.signal() }
    src.resume()
    // keep sources alive for process lifetime
    _ = Unmanaged.passRetained(src as AnyObject)
}
FileHandle.standardError.write("sys-audio-tap: recording → \(outURL.path)\n".data(using: .utf8)!)

DispatchQueue.global().async {
    stop.wait()
    AudioDeviceStop(aggID, procID)
    AudioDeviceDestroyIOProcID(aggID, procID)
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
    FileHandle.standardError.write("sys-audio-tap: stopped, \(framesWritten) frames\n".data(using: .utf8)!)
    exit(framesWritten > 0 ? 0 : 2)  // exit 2 = ran but captured nothing
}
dispatchMain()
