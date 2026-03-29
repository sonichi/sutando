import Cocoa
import SwiftUI
import CoreGraphics
import Combine
import Carbon.HIToolbox

// MARK: - Data Model

struct TaskInfo: Identifiable, Decodable {
    let id: String
    let status: String
    let text: String
    var time: Double?
}

struct APIResponse: Decodable {
    let tasks: [TaskInfo]?
    let claude: Bool?
    let watcher: Bool?
}

struct TaskSubmitResponse: Decodable {
    let ok: Bool?
    let task_id: String?
    let error: String?
}

struct TaskResultResponse: Decodable {
    let status: String?
    let result: String?
}

struct CancelTaskResponse: Decodable {
    let ok: Bool?
    let task_id: String?
    let status: String?
    let result: String?
    let error: String?
}

struct VoiceControlStateResponse: Decodable {
    let clientConnected: Bool?
    let muted: Bool?
    let pttMode: Bool?
    let pttHeld: Bool?
    let liveUserText: String?
    let liveUserFinal: Bool?
    let updatedAt: Double?
}

struct LiveTextSubmitResponse: Decodable {
    let ok: Bool?
    let mode: String?
    let error: String?
}

private func stableMessageID(role: String, text: String, time: Date) -> String {
    let payload = "\(role)|\(Int(time.timeIntervalSince1970 * 1000))|\(text)"
    var hash: UInt64 = 1469598103934665603
    for byte in payload.utf8 {
        hash ^= UInt64(byte)
        hash &*= 1099511628211
    }
    return "\(role):\(String(hash, radix: 16))"
}

struct Message: Identifiable, Equatable {
    let id: String
    let role: String // "user" or "assistant"
    let text: String
    let time: Date

    init(role: String, text: String, time: Date) {
        self.role = role
        self.text = text
        self.time = time
        self.id = stableMessageID(role: role, text: text, time: time)
    }

    static func == (lhs: Message, rhs: Message) -> Bool { lhs.id == rhs.id }
}

enum VoiceMode: String {
    case alwaysOn = "Always On"
    case pushToTalk = "Push to Talk"
    case muted = "Muted"

    var next: VoiceMode {
        switch self {
        case .alwaysOn: return .pushToTalk
        case .pushToTalk: return .muted
        case .muted: return .alwaysOn
        }
    }
}

struct SutandoPaths {
    static let shared = SutandoPaths()

    let repoDir: String
    let resultsDir: String
    let killAllScript: String

    init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        let fileManager = FileManager.default
        let envRepo = environment["SUTANDO_REPO_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let executableDir = URL(fileURLWithPath: CommandLine.arguments[0])
            .standardizedFileURL
            .deletingLastPathComponent()
            .path
        let cwd = fileManager.currentDirectoryPath

        let candidates = [envRepo, executableDir, cwd].compactMap { rawPath -> String? in
            guard let rawPath, !rawPath.isEmpty else { return nil }
            return URL(fileURLWithPath: (rawPath as NSString).expandingTildeInPath)
                .standardizedFileURL
                .path
        }

        func looksLikeRepo(_ path: String) -> Bool {
            fileManager.fileExists(atPath: path + "/src")
                && fileManager.fileExists(atPath: path + "/package.json")
        }

        repoDir = candidates.first(where: looksLikeRepo)
            ?? candidates.first(where: { fileManager.fileExists(atPath: $0 + "/src") })
            ?? executableDir
        resultsDir = repoDir + "/results"
        killAllScript = repoDir + "/src/kill-all.sh"
    }
}

class WidgetState: ObservableObject {
    struct PendingLiveEcho {
        let text: String
        let time: Date
    }

    @Published var tasks: [TaskInfo] = []
    @Published var voiceOnline = false
    @Published var voiceClientConnected = false
    @Published var narration = ""
    @Published var coreStatus = ""
    @Published var inputText = ""
    @Published var messages: [Message] = []
    @Published var expandedMessageId: String? = nil
    @Published var isExpanded = false
    @Published var pttActive = false // PTT key held
    @Published var voiceMode: VoiceMode = .alwaysOn
    @Published var liveUserText = ""
    @Published var liveUserFinal = false
    var pttLabel = PTTConfig.defaultConfig.label
    private let paths = SutandoPaths.shared
    private var pendingLiveEchoes: [PendingLiveEcho] = []
    private var timer: Timer?
    private var liveTimer: Timer?
    private var pendingPolls: [String: Timer] = [:]

    init() {
        try? FileManager.default.createDirectory(
            atPath: paths.resultsDir,
            withIntermediateDirectories: true
        )
    }

    func startPolling() {
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.poll()
        }
        pollLiveState()
        liveTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: true) { [weak self] _ in
            self?.pollLiveState()
        }
    }

    private func poll() {
        // Poll tasks
        guard let url = URL(string: "http://localhost:7843/tasks/active") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data,
                  let resp = try? JSONDecoder().decode(APIResponse.self, from: data) else {
                return
            }
            DispatchQueue.main.async {
                self?.tasks = resp.tasks ?? []
            }
        }.resume()

        // Poll core status
        if let coreUrl = URL(string: "http://localhost:7843/core-status") {
            URLSession.shared.dataTask(with: coreUrl) { [weak self] data, _, _ in
                guard let self, let data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    DispatchQueue.main.async { self?.coreStatus = "" }
                    return
                }
                let status = json["status"] as? String ?? "idle"
                let step = json["step"] as? String ?? ""
                DispatchQueue.main.async {
                    self.coreStatus = status == "running" ? step : ""
                }
            }.resume()
        }

        // Check voice agent
        var req = URLRequest(url: URL(string: "http://localhost:9900")!)
        req.httpMethod = "HEAD"
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            let online = (resp as? HTTPURLResponse)?.statusCode != nil
            DispatchQueue.main.async {
                self?.voiceOnline = online
                if online == false {
                    self?.voiceClientConnected = false
                    self?.liveUserText = ""
                    self?.liveUserFinal = false
                }
            }
        }.resume()

        // Check latest status (only if recent — < 60s old)
        let resultsDir = paths.resultsDir
        if let files = try? FileManager.default.contentsOfDirectory(atPath: resultsDir) {
            let statusFiles = files.filter { $0.hasPrefix("status-") && $0.hasSuffix(".txt") }.sorted()
            if let latest = statusFiles.last {
                let path = resultsDir + "/" + latest
                let attrs = try? FileManager.default.attributesOfItem(atPath: path)
                let modDate = attrs?[.modificationDate] as? Date ?? Date.distantPast
                if Date().timeIntervalSince(modDate) < 60,
                   let content = try? String(contentsOfFile: path, encoding: .utf8) {
                    DispatchQueue.main.async { self.narration = content.trimmingCharacters(in: .whitespacesAndNewlines) }
                } else {
                    DispatchQueue.main.async { self.narration = "" }
                }
            }
        }

        // Poll voice conversation
        let voicePath = resultsDir + "/voice-conversation.json"
        if let data = try? Data(contentsOf: URL(fileURLWithPath: voicePath)),
           let items = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
            let attrs = try? FileManager.default.attributesOfItem(atPath: voicePath)
            let modDate = attrs?[.modificationDate] as? Date ?? Date.distantPast
            // Only use if file is recent (< 5 min old)
            if Date().timeIntervalSince(modDate) < 300 {
                let voiceMsgs = items.compactMap { item -> Message? in
                    guard let role = item["role"] as? String,
                          let text = item["text"] as? String,
                          !text.isEmpty,
                          !text.hasPrefix("[System:") else { return nil }
                    let time = Date(timeIntervalSince1970: (item["time"] as? Double) ?? Date().timeIntervalSince1970)
                    return Message(role: role, text: text, time: time)
                }
                DispatchQueue.main.async {
                    self.prunePendingLiveEchoes()
                    var existingIds = Set(self.messages.map { $0.id })
                    for vm in voiceMsgs {
                        if self.consumePendingLiveEcho(for: vm) {
                            continue
                        }
                        guard existingIds.insert(vm.id).inserted else { continue }
                        self.messages.append(vm)
                    }
                    self.messages.sort { $0.time < $1.time }
                }
            }
        }
    }

    func submitTask(_ text: String) {
        guard !text.isEmpty else { return }
        let localTime = Date()
        let msg = Message(role: "user", text: text, time: localTime)
        DispatchQueue.main.async { self.messages.append(msg) }

        if voiceClientConnected {
            pendingLiveEchoes.append(PendingLiveEcho(text: text, time: localTime))
            submitLiveText(text, localTime: localTime)
            return
        }

        submitTaskBridge(text)
    }

    private func submitTaskBridge(_ text: String) {
        guard let url = URL(string: "http://localhost:7843/task") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.addValue("application/json", forHTTPHeaderField: "Content-Type")
        let body = try? JSONSerialization.data(withJSONObject: ["from": "widget", "task": text])
        req.httpBody = body

        URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            guard let data = data,
                  let resp = try? JSONDecoder().decode(TaskSubmitResponse.self, from: data),
                  let taskId = resp.task_id else {
                DispatchQueue.main.async {
                    self?.messages.append(Message(role: "assistant", text: "(Failed to send — agent API not reachable)", time: Date()))
                }
                return
            }
            self?.pollForResult(taskId: taskId)
        }.resume()
    }

    private func submitLiveText(_ text: String, localTime: Date) {
        guard let url = URL(string: "http://localhost:9901/text-input") else {
            removePendingLiveEcho(text: text, time: localTime)
            submitTaskBridge(text)
            return
        }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.addValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 2
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])

        URLSession.shared.dataTask(with: req) { [weak self] data, response, _ in
            let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            let ok = data.flatMap { try? JSONDecoder().decode(LiveTextSubmitResponse.self, from: $0) }?.ok == true
            guard statusCode == 200, ok else {
                DispatchQueue.main.async {
                    self?.removePendingLiveEcho(text: text, time: localTime)
                    self?.submitTaskBridge(text)
                }
                return
            }
        }.resume()
    }

    private func pollForResult(taskId: String) {
        guard let url = URL(string: "http://localhost:7843/result/\(taskId)") else { return }
        let timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] t in
            URLSession.shared.dataTask(with: url) { data, _, _ in
                guard let data = data,
                      let resp = try? JSONDecoder().decode(TaskResultResponse.self, from: data),
                      resp.status == "completed",
                      let result = resp.result else { return }
                t.invalidate()
                DispatchQueue.main.async {
                    self?.pendingPolls.removeValue(forKey: taskId)
                    self?.messages.append(Message(role: "assistant", text: result, time: Date()))
                }
            }.resume()
        }
        pendingPolls[taskId] = timer
    }

    func setRemoteMuted(_ muted: Bool) {
        guard let url = URL(string: "http://localhost:9901/mute") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.addValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 2
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["muted": muted])
        URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
    }

    func cancelTask(_ taskId: String) {
        guard let url = URL(string: "http://localhost:7843/task/cancel") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.addValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 2
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["task_id": taskId])
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self,
                  let data,
                  let resp = try? JSONDecoder().decode(CancelTaskResponse.self, from: data) else {
                return
            }
            DispatchQueue.main.async {
                if resp.ok == true {
                    self.tasks = self.tasks.map { task in
                        guard task.id == taskId else { return task }
                        return TaskInfo(id: task.id, status: "cancelled", text: task.text, time: Date().timeIntervalSince1970)
                    }
                } else if let error = resp.error {
                    self.messages.append(Message(role: "assistant", text: "(Couldn't cancel: \(error))", time: Date()))
                }
            }
        }.resume()
    }

    private func pollLiveState() {
        guard let url = URL(string: "http://localhost:9901/state") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self,
                  let data,
                  let resp = try? JSONDecoder().decode(VoiceControlStateResponse.self, from: data) else {
                return
            }
            DispatchQueue.main.async {
                self.voiceClientConnected = resp.clientConnected ?? false
                self.liveUserText = resp.liveUserText ?? ""
                self.liveUserFinal = resp.liveUserFinal ?? false
            }
        }.resume()
    }

    private func prunePendingLiveEchoes(reference: Date = Date()) {
        pendingLiveEchoes.removeAll { reference.timeIntervalSince($0.time) > 20 }
    }

    private func consumePendingLiveEcho(for message: Message) -> Bool {
        guard message.role == "user" else { return false }
        if let idx = pendingLiveEchoes.firstIndex(where: { pending in
            pending.text == message.text && abs(pending.time.timeIntervalSince(message.time)) < 15
        }) {
            pendingLiveEchoes.remove(at: idx)
            return true
        }
        return false
    }

    private func removePendingLiveEcho(text: String, time: Date) {
        if let idx = pendingLiveEchoes.firstIndex(where: { pending in
            pending.text == text && abs(pending.time.timeIntervalSince(time)) < 1
        }) {
            pendingLiveEchoes.remove(at: idx)
        }
    }
}

// MARK: - Suggestion Chips

let suggestions = [
    "What's on my calendar today?",
    "What's on my screen?",
    "Take a note: ",
    "Join my next meeting",
]

// MARK: - SwiftUI View

struct WidgetView: View {
    @ObservedObject var state: WidgetState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            headerBar

            Divider().background(Color.white.opacity(0.1))

            if state.isExpanded {
                expandedContent
            } else {
                compactContent
            }

            Divider().background(Color.white.opacity(0.1))

            // PTT listening bar
            if state.pttActive && state.voiceMode == .pushToTalk {
                HStack(spacing: 8) {
                    Circle()
                        .fill(Color.green)
                        .frame(width: 10, height: 10)
                    Image(systemName: "mic.fill")
                        .font(.system(size: 14))
                    Text("Listening...")
                        .font(.system(size: 13, weight: .semibold))
                }
                .foregroundColor(.green)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
                .background(Color.green.opacity(0.2))
            }

            // Input bar
            inputBar
        }
        .frame(width: state.isExpanded ? 380 : 300)
        .background(VisualEffectBlur())
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(state.pttActive ? Color.green.opacity(0.6) : Color.white.opacity(0.08), lineWidth: state.pttActive ? 2 : 1)
        )
        .animation(.easeInOut(duration: 0.15), value: state.pttActive)
    }

    // MARK: - Header

    var headerBar: some View {
        HStack {
            // Voice status — tappable to cycle modes
            if state.voiceOnline {
                let isListening = state.voiceMode == .pushToTalk && state.pttActive
                let dotColor: Color = isListening || state.voiceMode == .alwaysOn ? .green
                    : state.voiceMode == .pushToTalk ? .orange : .red
                let label = isListening ? "Listening" : state.voiceMode.rawValue
                let textColor = dotColor

                HStack(spacing: 4) {
                    Circle()
                        .fill(dotColor)
                        .frame(width: 8, height: 8)
                    Text(label)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(textColor)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 7))
                        .foregroundColor(.gray.opacity(0.4))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(dotColor.opacity(0.1))
                .cornerRadius(10)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(dotColor.opacity(0.2), lineWidth: 1))
                .onTapGesture {
                    let old = state.voiceMode
                    state.voiceMode = state.voiceMode.next
                    // Sync mic state with voice agent
                    if old == .alwaysOn && state.voiceMode == .pushToTalk {
                        state.setRemoteMuted(true) // mute mic for PTT default
                    } else if old == .pushToTalk && state.voiceMode == .muted {
                        state.setRemoteMuted(true)
                    } else if old == .muted && state.voiceMode == .alwaysOn {
                        state.setRemoteMuted(false) // unmute
                    }
                }
            } else {
                Circle()
                    .fill(Color.gray)
                    .frame(width: 8, height: 8)
                Text("Idle")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.gray)
            }

            Spacer()

            // PTT hint — only show in PTT mode
            if state.voiceOnline && state.voiceMode == .pushToTalk {
                Text("PTT: \(state.pttLabel)")
                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                    .foregroundColor(state.pttActive ? .green : .gray.opacity(0.5))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(state.pttActive ? Color.green.opacity(0.15) : Color.clear)
                    .cornerRadius(3)
            }

            // Expand/collapse
            Button(action: { state.isExpanded.toggle() }) {
                Image(systemName: state.isExpanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 10))
                    .foregroundColor(.gray.opacity(0.6))
            }
            .buttonStyle(.plain)

            // Open Web UI
            Button(action: openWebUI) {
                Image(systemName: "globe")
                    .font(.system(size: 10))
                    .foregroundColor(.gray.opacity(0.6))
            }
            .buttonStyle(.plain)
            .help("Open Web UI")

            // End All
            Button(action: endAll) {
                Text("End All")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(.red.opacity(0.8))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.red.opacity(0.15))
                    .cornerRadius(4)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 6)
    }

    // MARK: - Compact content

    var compactContent: some View {
        VStack(alignment: .leading, spacing: 4) {
            let activeTasks = visibleTasks(limit: 3)

            if activeTasks.isEmpty && state.messages.isEmpty && state.liveUserText.isEmpty {
                // Show suggestions when idle
                suggestionsView
            } else {
                // Show tasks
                ForEach(Array(activeTasks)) { task in
                    taskRow(task)
                }

                if !state.liveUserText.isEmpty {
                    liveTranscriptRow
                }

                // Show latest message
                if let last = state.messages.last {
                    HStack(spacing: 6) {
                        Text(last.role == "user" ? "You:" : "Sutando:")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(last.role == "user" ? Color(red: 0.5, green: 0.7, blue: 0.88) : Color(red: 0.66, green: 0.85, blue: 0.69))
                        Text(String(last.text.prefix(45)) + (last.text.count > 45 ? "..." : ""))
                            .font(.system(size: 11))
                            .foregroundColor(.gray)
                            .lineLimit(1)
                    }
                    .padding(.top, 2)
                    .onTapGesture { state.isExpanded = true }
                }
            }

            // Status line (narration or core status)
            if !state.narration.isEmpty {
                HStack(spacing: 4) {
                    Text("\u{2591}")
                        .font(.system(size: 9))
                        .foregroundColor(.green.opacity(0.5))
                    Text(String(state.narration.prefix(45)) + (state.narration.count > 45 ? "..." : ""))
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                        .lineLimit(1)
                }
            } else if !state.coreStatus.isEmpty {
                HStack(spacing: 4) {
                    Circle()
                        .fill(Color.blue.opacity(0.6))
                        .frame(width: 6, height: 6)
                    Text(String(state.coreStatus.prefix(45)) + (state.coreStatus.count > 45 ? "..." : ""))
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                        .lineLimit(1)
                }
            }

            // Start Voice button when offline
            if !state.voiceOnline {
                Button(action: openWebUI) {
                    HStack(spacing: 6) {
                        Image(systemName: "mic.fill")
                            .font(.system(size: 10))
                        Text("Start Voice")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundColor(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .background(Color(red: 0.12, green: 0.32, blue: 0.16))
                    .cornerRadius(8)
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color(red: 0.17, green: 0.48, blue: 0.23), lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)
                .padding(.top, 4)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Expanded content

    var expandedContent: some View {
        VStack(alignment: .leading, spacing: 4) {
            let activeTasks = visibleTasks(limit: 4)
            if !activeTasks.isEmpty {
                ForEach(Array(activeTasks)) { task in
                    taskRow(task)
                }
                Divider().background(Color.white.opacity(0.05)).padding(.vertical, 2)
            }

            // Conversation — latest exchange prominent, history collapsed
            if state.messages.isEmpty && state.liveUserText.isEmpty {
                suggestionsView
            } else {
                if !state.liveUserText.isEmpty {
                    liveTranscriptRow
                }
                let latestPair = getLatestExchange(state.messages)
                let historyMessages = Array(state.messages.dropLast(latestPair.count))

                // History (older messages) — collapsed, small
                if !historyMessages.isEmpty {
                    DisclosureGroup("History (\(historyMessages.count))") {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 4) {
                                ForEach(historyMessages) { msg in
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(msg.role == "user" ? "You" : "Sutando")
                                            .font(.system(size: 8, weight: .semibold))
                                            .foregroundColor(msg.role == "user" ? Color(red: 0.5, green: 0.7, blue: 0.88).opacity(0.5) : Color(red: 0.66, green: 0.85, blue: 0.69).opacity(0.5))
                                        Text(String(msg.text.prefix(80)) + (msg.text.count > 80 ? "..." : ""))
                                            .font(.system(size: 9))
                                            .foregroundColor(.white.opacity(0.35))
                                            .lineLimit(2)
                                    }
                                }
                            }
                        }
                        .frame(maxHeight: 120)
                    }
                    .font(.system(size: 10))
                    .foregroundColor(.gray.opacity(0.5))
                    .padding(.bottom, 4)
                }

                // Latest exchange — prominent
                ForEach(latestPair) { msg in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(msg.role == "user" ? "You" : "Sutando")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundColor(msg.role == "user" ? Color(red: 0.5, green: 0.7, blue: 0.88) : Color(red: 0.66, green: 0.85, blue: 0.69))
                        Text(state.expandedMessageId == msg.id ? msg.text : String(msg.text.prefix(200)) + (msg.text.count > 200 ? "..." : ""))
                            .font(.system(size: 11))
                            .foregroundColor(.white.opacity(0.85))
                            .textSelection(.enabled)
                            .onTapGesture {
                                state.expandedMessageId = state.expandedMessageId == msg.id ? nil : msg.id
                            }
                    }
                }
            }

            // Status line (narration or core status)
            if !state.narration.isEmpty {
                HStack(spacing: 4) {
                    Text("\u{2591}")
                        .font(.system(size: 9))
                        .foregroundColor(.green.opacity(0.5))
                    Text(state.narration)
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                        .lineLimit(2)
                }
            } else if !state.coreStatus.isEmpty {
                HStack(spacing: 4) {
                    Circle()
                        .fill(Color.blue.opacity(0.6))
                        .frame(width: 6, height: 6)
                    Text(state.coreStatus)
                        .font(.system(size: 10))
                        .foregroundColor(.white.opacity(0.5))
                        .lineLimit(2)
                }
            }

            // Start Voice
            if !state.voiceOnline {
                Button(action: openWebUI) {
                    HStack(spacing: 6) {
                        Image(systemName: "mic.fill")
                            .font(.system(size: 10))
                        Text("Start Voice")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundColor(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .background(Color(red: 0.12, green: 0.32, blue: 0.16))
                    .cornerRadius(8)
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color(red: 0.17, green: 0.48, blue: 0.23), lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)
                .padding(.top, 4)
                .frame(maxWidth: .infinity, alignment: .center)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Suggestions

    var suggestionsView: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Try asking")
                .font(.system(size: 9))
                .foregroundColor(.gray.opacity(0.5))
                .padding(.bottom, 2)
            ForEach(suggestions, id: \.self) { s in
                Button(action: {
                    state.submitTask(s)
                }) {
                    Text(s)
                        .font(.system(size: 11))
                        .foregroundColor(.gray.opacity(0.7))
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.white.opacity(0.04))
                        .cornerRadius(12)
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .stroke(Color.white.opacity(0.08), lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Task row

    func taskRow(_ task: TaskInfo) -> some View {
        HStack(spacing: 6) {
            Text(task.status == "working" ? "\u{2699}" : task.status == "done" ? "\u{2713}" : task.status == "cancelled" ? "\u{2715}" : "\u{23F3}")
                .font(.system(size: 10))
            Text(String(task.text.prefix(40)) + (task.text.count > 40 ? "..." : ""))
                .font(.system(size: 11))
                .foregroundColor(task.status == "working" ? .blue : task.status == "cancelled" ? .red.opacity(0.8) : .gray)
                .lineLimit(1)
            Spacer()
            if let t = task.time {
                let ago = Int(Date().timeIntervalSince1970 - t)
                Text(ago < 60 ? "\(ago)s" : "\(ago / 60)m")
                    .font(.system(size: 9))
                    .foregroundColor(.gray.opacity(0.6))
            }
            if task.status == "working" || task.status == "pending" {
                Button(action: { state.cancelTask(task.id) }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 12))
                        .foregroundColor(.red.opacity(0.8))
                }
                .buttonStyle(.plain)
                .help("Cancel task")
            }
        }
    }

    // MARK: - Helpers

    func visibleTasks(limit: Int) -> [TaskInfo] {
        Array(state.tasks
            .filter { $0.status == "working" || $0.status == "pending" }
            .sorted {
                let lhsOrder = taskSortOrder($0.status)
                let rhsOrder = taskSortOrder($1.status)
                if lhsOrder != rhsOrder { return lhsOrder < rhsOrder }
                return ($0.time ?? 0) > ($1.time ?? 0)
            }
            .prefix(limit))
    }

    func taskSortOrder(_ status: String) -> Int {
        switch status {
        case "working": return 0
        case "pending": return 1
        case "cancelled": return 2
        default: return 3
        }
    }

    var liveTranscriptRow: some View {
        HStack(alignment: .top, spacing: 6) {
            Text("You:")
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(Color(red: 0.5, green: 0.7, blue: 0.88).opacity(state.liveUserFinal ? 0.95 : 0.7))
            Text(String(state.liveUserText.prefix(70)) + (state.liveUserText.count > 70 ? "..." : ""))
                .font(.system(size: 11))
                .foregroundColor(.white.opacity(state.liveUserFinal ? 0.85 : 0.6))
                .lineLimit(state.isExpanded ? nil : 4)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    func getLatestExchange(_ messages: [Message]) -> [Message] {
        guard !messages.isEmpty else { return [] }
        var result: [Message] = []
        // Walk backwards: grab last assistant reply + last user message
        for msg in messages.reversed() {
            if result.isEmpty || (result.count == 1 && result[0].role != msg.role) {
                result.insert(msg, at: 0)
            }
            if result.count >= 2 { break }
        }
        return result
    }

    // MARK: - Input bar

    var inputBar: some View {
        HStack(spacing: 6) {
            TextField("Type a message...", text: $state.inputText)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(Color.white.opacity(0.05))
                .cornerRadius(8)
                .onSubmit { sendMessage() }

            Button(action: sendMessage) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 18))
                    .foregroundColor(state.inputText.isEmpty ? .gray.opacity(0.3) : Color(red: 0.31, green: 0.8, blue: 0.64))
            }
            .buttonStyle(.plain)
            .disabled(state.inputText.isEmpty)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
    }

    // MARK: - Actions

    func sendMessage() {
        let text = state.inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        state.inputText = ""
        state.submitTask(text)
    }

    func openWebUI() {
        NSWorkspace.shared.open(URL(string: "http://localhost:8080")!)
    }

    func endAll() {
        // Notify Claude Code before killing services
        if let url = URL(string: "http://localhost:7843/task") {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.addValue("application/json", forHTTPHeaderField: "Content-Type")
            req.timeoutInterval = 2
            req.httpBody = try? JSONSerialization.data(withJSONObject: [
                "from": "widget",
                "task": "User pressed End All — graceful shutdown. Stop proactive loop, save state, clean up."
            ])
            URLSession.shared.dataTask(with: req) { _, _, _ in }.resume()
        }

        // Give the task a moment to land, then kill everything via kill-all.sh
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            proc.arguments = [SutandoPaths.shared.killAllScript]
            try? proc.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                NSApplication.shared.terminate(nil)
            }
        }
    }
}

// MARK: - Visual Effect (translucent background)

struct VisualEffectBlur: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

// MARK: - Floating Panel

class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }

    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.nonactivatingPanel, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        isFloatingPanel = true
        level = .floating
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        isMovableByWindowBackground = true
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
    }
}

// MARK: - PTT Key Configuration

struct PTTConfig {
    let keyCode: Int64
    let modifierMask: CGEventFlags
    let label: String

    var carbonModifierMask: UInt32 {
        var mask: UInt32 = 0
        if modifierMask.contains(.maskCommand) { mask |= UInt32(cmdKey) }
        if modifierMask.contains(.maskAlternate) { mask |= UInt32(optionKey) }
        if modifierMask.contains(.maskControl) { mask |= UInt32(controlKey) }
        if modifierMask.contains(.maskShift) { mask |= UInt32(shiftKey) }
        return mask
    }

    static let keyNames: [String: Int64] = [
        "space": 49, "tab": 48, "return": 36, "escape": 53,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
        "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    ]

    static let modifierNames: [String: CGEventFlags] = [
        "option": .maskAlternate, "alt": .maskAlternate,
        "ctrl": .maskControl, "control": .maskControl,
        "cmd": .maskCommand, "command": .maskCommand,
        "shift": .maskShift,
    ]

    static let modifierSymbols: [String: String] = [
        "option": "\u{2325}", "alt": "\u{2325}",
        "ctrl": "\u{2303}", "control": "\u{2303}",
        "cmd": "\u{2318}", "command": "\u{2318}",
        "shift": "\u{21E7}",
    ]

    static let keySymbols: [String: String] = [
        "space": "Space", "tab": "Tab", "return": "Return", "escape": "Esc",
    ]

    static func parse(_ string: String) -> PTTConfig {
        let parts = string.lowercased().split(separator: "+").map(String.init)
        guard parts.count == 2 else { return PTTConfig.defaultConfig }

        let modName = parts[0]
        let keyName = parts[1]

        guard let mod = modifierNames[modName],
              let key = keyNames[keyName] ?? Int64(keyName) else {
            return PTTConfig.defaultConfig
        }

        let modSym = modifierSymbols[modName] ?? modName
        let keySym = keySymbols[keyName] ?? keyName.capitalized
        return PTTConfig(keyCode: key, modifierMask: mod, label: "\(modSym)\(keySym)")
    }

    static let defaultConfig = PTTConfig(keyCode: 49, modifierMask: .maskControl, label: "\u{2303}Space")
}

// MARK: - Global PTT Hotkey

private func fourCharCode(_ string: String) -> OSType {
    string.utf8.prefix(4).reduce(0) { ($0 << 8) | UInt32($1) }
}

class GlobalPTT {
    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    var config: PTTConfig
    private var hotKeyRef: EventHotKeyRef?
    private var eventHandler: EventHandlerRef?
    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var isPressed = false
    private let hotKeyID = EventHotKeyID(signature: fourCharCode("SUTA"), id: 1)

    init(config: PTTConfig = .defaultConfig) {
        self.config = config
    }

    func start() {
        if startCarbonHotKey() {
            return
        }
        startEventTapFallback()
    }

    private func startCarbonHotKey() -> Bool {
        var eventTypes = [
            EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: UInt32(kEventHotKeyPressed)
            ),
            EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: UInt32(kEventHotKeyReleased)
            ),
        ]

        let refcon = Unmanaged.passUnretained(self).toOpaque()
        let handlerStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData in
                guard let event, let userData else { return noErr }
                let ptt = Unmanaged<GlobalPTT>.fromOpaque(userData).takeUnretainedValue()
                var eventHotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &eventHotKeyID
                )
                guard status == noErr,
                      eventHotKeyID.signature == ptt.hotKeyID.signature,
                      eventHotKeyID.id == ptt.hotKeyID.id else {
                    return noErr
                }

                switch GetEventKind(event) {
                case UInt32(kEventHotKeyPressed):
                    ptt.handlePress()
                case UInt32(kEventHotKeyReleased):
                    ptt.handleRelease()
                default:
                    break
                }
                return noErr
            },
            eventTypes.count,
            &eventTypes,
            refcon,
            &eventHandler
        )

        guard handlerStatus == noErr else {
            print("[PTT] Failed to install Carbon hotkey handler (status \(handlerStatus))")
            return false
        }

        let registerStatus = RegisterEventHotKey(
            UInt32(config.keyCode),
            config.carbonModifierMask,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )

        guard registerStatus == noErr else {
            if let handler = eventHandler {
                RemoveEventHandler(handler)
                eventHandler = nil
            }
            let conflictHint = registerStatus == eventHotKeyExistsErr
                ? " — another app or macOS shortcut already owns it"
                : ""
            print("[PTT] Carbon hotkey registration failed for \(config.label)\(conflictHint). Trying Accessibility event tap fallback.")
            return false
        }

        print("[PTT] Global hotkey active: \(config.label)")
        return true
    }

    private func startEventTapFallback() {
        let mask: CGEventMask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue) | (1 << CGEventType.flagsChanged.rawValue)

        let callback: CGEventTapCallBack = { _, type, event, refcon in
            guard let ptt = refcon.map({ Unmanaged<GlobalPTT>.fromOpaque($0).takeUnretainedValue() }) else {
                return Unmanaged.passUnretained(event)
            }

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = ptt.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
            let flags = event.flags

            if type == .flagsChanged && ptt.isPressed && !flags.contains(ptt.config.modifierMask) {
                ptt.handleRelease()
                return Unmanaged.passUnretained(event)
            }

            if keyCode == ptt.config.keyCode && flags.contains(ptt.config.modifierMask) {
                if type == .keyDown {
                    if event.getIntegerValueField(.keyboardEventAutorepeat) == 0 {
                        ptt.handlePress()
                    }
                    return nil // consume the event
                } else if type == .keyUp {
                    ptt.handleRelease()
                    return nil
                }
            }

            return Unmanaged.passUnretained(event)
        }

        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: mask,
            callback: callback,
            userInfo: refcon
        ) else {
            print("[PTT] Failed to create event tap — grant Accessibility permission in System Settings > Privacy & Security > Accessibility")
            return
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), runLoopSource, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        print("[PTT] Global hotkey active via Accessibility fallback: \(config.label)")
    }

    private func handlePress() {
        guard !isPressed else { return }
        isPressed = true
        onPress?()
    }

    private func handleRelease() {
        guard isPressed else { return }
        isPressed = false
        onRelease?()
    }

    func stop() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
        }
        if let eventHandler {
            RemoveEventHandler(eventHandler)
        }
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, .commonModes)
        }
        hotKeyRef = nil
        eventHandler = nil
        eventTap = nil
        runLoopSource = nil
        isPressed = false
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: FloatingPanel!
    let state = WidgetState()
    let ptt: GlobalPTT = {
        if let envKey = ProcessInfo.processInfo.environment["SUTANDO_PTT_KEY"] {
            return GlobalPTT(config: PTTConfig.parse(envKey))
        }
        return GlobalPTT()
    }()
    var cancellables = Set<AnyCancellable>()

    let compactSize = NSSize(width: 300, height: 260)
    let expandedSize = NSSize(width: 380, height: 480)

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Position in top-right corner
        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let panelWidth: CGFloat = 300
        let panelHeight: CGFloat = 260
        let origin = NSPoint(
            x: screenFrame.maxX - panelWidth - 16,
            y: screenFrame.maxY - panelHeight - 16
        )
        let rect = NSRect(origin: origin, size: NSSize(width: panelWidth, height: panelHeight))

        panel = FloatingPanel(contentRect: rect)
        let hostingView = NSHostingView(rootView: WidgetView(state: state))
        panel.contentView = hostingView
        panel.orderFrontRegardless()

        state.pttLabel = ptt.config.label
        state.startPolling()

        // Resize panel when expanded/collapsed — anchor right+top edge
        state.$isExpanded
            .dropFirst()
            .sink { [weak self] expanded in
                guard let self = self else { return }
                let newSize = expanded ? self.expandedSize : self.compactSize
                let oldFrame = self.panel.frame
                // Anchor right edge and top edge
                let newOrigin = NSPoint(
                    x: oldFrame.maxX - newSize.width,
                    y: oldFrame.maxY - newSize.height
                )
                let newFrame = NSRect(origin: newOrigin, size: newSize)
                self.panel.setFrame(newFrame, display: true, animate: false)
            }
            .store(in: &cancellables)

        // Setup global PTT
        ptt.onPress = { [weak self] in
            guard let self = self, self.state.voiceMode == .pushToTalk else { return }
            DispatchQueue.main.async { self.state.pttActive = true }
            self.state.setRemoteMuted(false)
        }
        ptt.onRelease = { [weak self] in
            guard let self = self, self.state.voiceMode == .pushToTalk else { return }
            DispatchQueue.main.async { self.state.pttActive = false }
            self.state.setRemoteMuted(true)
        }
        ptt.start()

        // Auto-open web UI with voice autoconnect unless startup.sh
        // already handled it for this launch.
        let shouldAutoOpenWebUI = ProcessInfo.processInfo.environment["SUTANDO_AUTOOPEN_WEBUI"] != "0"
        if shouldAutoOpenWebUI {
            NSWorkspace.shared.open(URL(string: "http://localhost:8080?autoconnect=1")!)
        }

        // Hide dock icon
        NSApp.setActivationPolicy(.accessory)
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
