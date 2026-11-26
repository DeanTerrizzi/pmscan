import AppKit
import Foundation

/// Where the Python project lives. Written into the bundle by macos/build.sh.
struct BackendConfig: Decodable {
    let projectDir: String
    let python: String

    enum CodingKeys: String, CodingKey {
        case projectDir = "project_dir"
        case python
    }

    static func load() -> BackendConfig? {
        if let dir = ProcessInfo.processInfo.environment["PMSCAN_PROJECT_DIR"] {
            return BackendConfig(projectDir: dir, python: "\(dir)/.venv/bin/python")
        }
        guard let url = Bundle.main.url(forResource: "backend", withExtension: "json"),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(BackendConfig.self, from: data)
    }
}

struct Arbitrage {
    let key: String
    let title: String
    let profit: Double
    let route: String
}

/// Runs `pmscan serve` as a child process and keeps a summary of its latest scan.
final class Backend: ObservableObject {
    enum State: Equatable {
        case starting
        case running(URL)
        case failed(String)
    }

    static let logURL: URL = {
        let dir = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/pmscan", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("scanner.log")
    }()

    @Published private(set) var state: State = .starting
    @Published private(set) var counts: [String: Int] = [:]
    @Published private(set) var arbitrageProfit: Double = 0
    @Published private(set) var scanning = false
    @Published private(set) var nextScanAt: Date?
    @Published private(set) var scanError: String?
    @Published private(set) var tradingMode = "off"
    @Published private(set) var halted: String?
    @Published private(set) var limitsDescription: String?
    @Published private(set) var paperOpen = 0
    @Published private(set) var paperEquity: Double?
    @Published private(set) var paperPnl: Double?
    @Published private(set) var liveFeed = false

    var onNewArbitrage: (([Arbitrage]) -> Void)?
    var onHalted: ((String) -> Void)?
    /// Asked to start the scanner again after it crashed, hung or failed to start.
    var onNeedsRestart: (() -> Void)?

    /// Polls missed in a row before a running scanner counts as hung (polls are 10 s apart).
    /// Timers don't fire while the Mac sleeps, so sleep never counts against it.
    static let maxMissedPolls = 18

    private var process: Process?
    private var pollTimer: Timer?
    private var generation = 0
    private var seenArbitrage: Set<String>?  // nil until the first scan is seen: don't notify for old ones
    private var lastHalted: String?
    private var restartAttempts = 0
    private var restartWork: DispatchWorkItem?
    private var runningSince: Date?
    private var missedPolls = 0

    // MARK: lifecycle

    func start(mode: TradingMode, paper: Bool, venue: Venue, environment extra: [String: String]) {
        stop()
        restartWork?.cancel()
        restartWork = nil
        generation += 1
        let gen = generation
        state = .starting
        runningSince = nil
        missedPolls = 0
        counts = [:]
        seenArbitrage = nil

        guard let config = BackendConfig.load() else {
            state = .failed("This build doesn't know where the pmscan project is. Rebuild it with macos/build.sh.")
            return
        }
        guard FileManager.default.isExecutableFile(atPath: config.python) else {
            state = .failed("Python environment not found at \(config.python).\nRun `uv sync` in \(config.projectDir), then restart.")
            return
        }

        let port = Ports.pick(preferred: 8766)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.python)
        var args: [String] = ["-m", "pmscan.cli", "serve", "--port", String(port), "--exit-with-parent"]
        args += ["--venue", venue.rawValue]
        if mode != .off { args.append("--trade") }
        if mode == .live { args.append("--live") }
        if !paper { args.append("--no-paper") }
        proc.arguments = args
        if let dataDir = ProcessInfo.processInfo.environment["PMSCAN_DATA_DIR"] {  // e.g. for test runs
            proc.arguments! += ["--data-dir", dataDir]
        }
        proc.currentDirectoryURL = URL(fileURLWithPath: config.projectDir)  // picks up .env and pmscan.toml
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        extra.forEach { env[$0.key] = $0.value }  // live-trading credentials from the Keychain, never on disk
        proc.environment = env

        // Append, so the reason for a crash is still there after the automatic restart; start over past 5 MB.
        let logSize = (try? FileManager.default.attributesOfItem(atPath: Self.logURL.path)[.size] as? Int) ?? nil
        if logSize == nil || logSize! > 5_000_000 {
            FileManager.default.createFile(atPath: Self.logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: Self.logURL) {
            log.seekToEndOfFile()
            log.write("pmscan scanner started \(Date()) on port \(port), \(venue.title), trading \(mode.short), paper \(paper ? "on" : "off")\n".data(using: .utf8)!)
            proc.standardOutput = log
            proc.standardError = log
        }
        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self, self.generation == gen, self.process === p else { return }
                self.process = nil
                self.pollTimer?.invalidate()
                self.scheduleRestart("The scanner stopped unexpectedly (exit code \(p.terminationStatus)).", generation: gen)
            }
        }
        do {
            try proc.run()
        } catch {
            state = .failed("Couldn't start the scanner: \(error.localizedDescription)")
            return
        }
        process = proc
        waitUntilReady(url: URL(string: "http://127.0.0.1:\(port)/")!, generation: gen, deadline: Date().addingTimeInterval(90))
    }

    func stop() {
        pollTimer?.invalidate()
        pollTimer = nil
        guard let proc = process else { return }
        process = nil
        if proc.isRunning {
            proc.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
            }
        }
    }

    /// Used when the app quits: make sure the scanner is gone before we are.
    func stopAndWait() {
        pollTimer?.invalidate()
        guard let proc = process else { return }
        process = nil
        guard proc.isRunning else { return }
        proc.terminate()
        let deadline = Date().addingTimeInterval(2)
        while proc.isRunning && Date() < deadline { usleep(50_000) }
        if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
    }

    private func waitUntilReady(url: URL, generation gen: Int, deadline: Date) {
        let probe = url.appendingPathComponent("api/latest")
        URLSession.shared.dataTask(with: probe) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self, self.generation == gen, self.process != nil else { return }
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.state = .running(url)
                    self.runningSince = Date()
                    self.startPolling(url: url, generation: gen)
                } else if Date() > deadline {
                    self.stop()
                    self.scheduleRestart("The scanner didn't start within 90 seconds.", generation: gen)
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                        self.waitUntilReady(url: url, generation: gen, deadline: deadline)
                    }
                }
            }
        }.resume()
    }

    // MARK: polling the scanner for the sidebar, Dock badge and notifications

    private func startPolling(url: URL, generation gen: Int) {
        poll(url: url, generation: gen)
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.poll(url: url, generation: gen)
        }
    }

    private func poll(url: URL, generation gen: Int) {
        var request = URLRequest(url: url.appendingPathComponent("api/latest"))
        request.timeoutInterval = 20
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            let obj = data.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
            DispatchQueue.main.async {
                guard let self, self.generation == gen, self.process != nil else { return }
                guard let obj else { return self.missedPoll(generation: gen) }
                self.missedPolls = 0
                if let since = self.runningSince, Date().timeIntervalSince(since) > 300 {
                    self.restartAttempts = 0  // it has been healthy for a while: next failure retries quickly
                }
                self.apply(obj)
            }
        }.resume()
    }

    /// The scanner is running but not answering: after a few minutes of that, replace it.
    private func missedPoll(generation gen: Int) {
        missedPolls += 1
        guard missedPolls >= Self.maxMissedPolls else { return }
        stop()
        scheduleRestart("The scanner stopped responding.", generation: gen)
    }

    /// Crashes and hangs restart on their own: after 5 s, then 10, 20, ... up to 2 minutes apart.
    private func scheduleRestart(_ reason: String, generation gen: Int) {
        let delay = min(5.0 * pow(2.0, Double(restartAttempts)), 120)
        restartAttempts += 1
        state = .failed("\(reason) Restarting in \(Int(delay)) seconds. The log has details.")
        if let log = try? FileHandle(forWritingTo: Self.logURL) {
            log.seekToEndOfFile()
            log.write("pmscan app: \(reason) Restarting in \(Int(delay))s (attempt \(restartAttempts)) \(Date())\n".data(using: .utf8)!)
        }
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == gen else { return }  // someone restarted it already
            self.onNeedsRestart?()
        }
        restartWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func apply(_ obj: [String: Any]) {
        let status = obj["status"] as? [String: Any] ?? [:]
        scanning = status["scanning"] as? Bool ?? false
        scanError = status["error"] as? String
        nextScanAt = (status["next_scan_in_s"] as? Double).map { Date().addingTimeInterval($0) }
        liveFeed = (status["fast"] as? [String: Any])?["connected"] as? Bool ?? false

        let trading = obj["trading"] as? [String: Any] ?? [:]
        tradingMode = trading["mode"] as? String ?? "off"
        if let strategies = trading["strategies"] as? [String],
           let perTrade = trading["max_usd_per_trade"] as? Double, let perDay = trading["max_usd_per_day"] as? Double {
            let names = strategies.map { $0.replacingOccurrences(of: "_", with: " ") }.joined(separator: ", ")
            limitsDescription = String(format: "Strategies: %@ · up to $%.0f per trade and $%.0f per day.", names, perTrade, perDay)
        }
        let paper = obj["paper"] as? [String: Any] ?? [:]
        paperOpen = paper["open_positions"] as? Int ?? 0
        paperEquity = (paper["enabled"] as? Bool ?? false) ? paper["equity"] as? Double : nil
        paperPnl = paper["pnl"] as? Double
        halted = trading["halted"] as? String
        if let reason = halted, reason != lastHalted { onHalted?(reason) }
        lastHalted = halted

        guard let result = obj["result"] as? [String: Any] else { return }
        var newCounts: [String: Int] = [:]
        var arbs: [Arbitrage] = []
        for strategy in result["strategies"] as? [[String: Any]] ?? [] {
            guard let name = strategy["name"] as? String else { continue }
            let opps = strategy["opportunities"] as? [[String: Any]] ?? []
            newCounts[name] = opps.count
            guard name == "neg_risk_arb" || name == "ladder_arb" else { continue }
            for o in opps {
                let key = [name, o["url"], o["kind"], o["subtitle"]].map { "\($0 ?? "")" }.joined(separator: "|")
                arbs.append(Arbitrage(key: key, title: o["title"] as? String ?? "Arbitrage",
                                      profit: o["profit"] as? Double ?? 0,
                                      route: name == "ladder_arb" ? Page.ladders.rawValue : Page.arbitrage.rawValue))
            }
        }
        counts = newCounts
        arbitrageProfit = arbs.reduce(0) { $0 + $1.profit }
        NSApp.dockTile.badgeLabel = arbs.isEmpty ? nil : String(arbs.count)

        let keys = Set(arbs.map(\.key))
        if let seen = seenArbitrage {
            let fresh = arbs.filter { !seen.contains($0.key) }.sorted { $0.profit > $1.profit }
            if !fresh.isEmpty { onNewArbitrage?(fresh) }
        }
        seenArbitrage = keys
    }
}

/// Picks a local port: the preferred one if it's free, otherwise any free one.
enum Ports {
    static func pick(preferred: UInt16) -> UInt16 {
        bind(preferred) ?? bind(0) ?? preferred
    }

    private static func bind(_ port: UInt16) -> UInt16? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }
        var out = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        return UInt16(bigEndian: out.sin_port)
    }
}
