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

    private var process: Process?
    private var pollTimer: Timer?
    private var generation = 0
    private var seenArbitrage: Set<String>?  // nil until the first scan is seen: don't notify for old ones
    private var lastHalted: String?

    // MARK: lifecycle

    func start(mode: TradingMode, paper: Bool, venue: Venue, environment extra: [String: String]) {
        stop()
        generation += 1
        let gen = generation
        state = .starting
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

        FileManager.default.createFile(atPath: Self.logURL.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: Self.logURL) {
            log.write("pmscan scanner started \(Date()) on port \(port), \(venue.title), trading \(mode.short), paper \(paper ? "on" : "off")\n".data(using: .utf8)!)
            proc.standardOutput = log
            proc.standardError = log
        }
        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self, self.generation == gen, self.process === p else { return }
                self.process = nil
                self.pollTimer?.invalidate()
                self.state = .failed("The scanner stopped unexpectedly (exit code \(p.terminationStatus)). The log has details.")
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
                    self.startPolling(url: url, generation: gen)
                } else if Date() > deadline {
                    self.state = .failed("The scanner didn't start within 90 seconds. The log has details.")
                    self.stop()
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
        URLSession.shared.dataTask(with: url.appendingPathComponent("api/latest")) { [weak self] data, _, _ in
            guard let data, let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let self, self.generation == gen else { return }
                self.apply(obj)
            }
        }.resume()
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
