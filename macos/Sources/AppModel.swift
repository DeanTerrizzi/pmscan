import AppKit
import Combine
import SwiftUI

enum Page: String, CaseIterable, Identifiable {
    case overview, arbitrage, ladders, sportsbook
    case highProb = "high-prob"
    case nearCertain = "near-certain"
    case paper, rewards, backtest, trading, markets

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "Overview"
        case .arbitrage: return "Neg-Risk Arbitrage"
        case .ladders: return "Ladder Arbitrage"
        case .sportsbook: return "Sportsbook Value"
        case .highProb: return "High-Probability"
        case .nearCertain: return "Near-Certain Bets"
        case .paper: return "Paper Trading"
        case .rewards: return "Liquidity Rewards"
        case .backtest: return "Backtest"
        case .trading: return "Trading"
        case .markets: return "Markets"
        }
    }

    var symbol: String {
        switch self {
        case .overview: return "square.grid.2x2"
        case .arbitrage: return "arrow.triangle.branch"
        case .ladders: return "chart.bar"
        case .sportsbook: return "sportscourt"
        case .highProb: return "target"
        case .nearCertain: return "checkmark.seal"
        case .paper: return "chart.line.uptrend.xyaxis"
        case .rewards: return "gift"
        case .backtest: return "clock.arrow.circlepath"
        case .trading: return "dollarsign.circle"
        case .markets: return "magnifyingglass"
        }
    }

    /// Strategy whose opportunity count is shown as the sidebar badge.
    var countKey: String? {
        switch self {
        case .arbitrage: return "neg_risk_arb"
        case .ladders: return "ladder_arb"
        case .sportsbook: return "sportsbook_value"
        case .highProb: return "high_prob"
        case .nearCertain: return "near_certain"
        default: return nil
        }
    }

    /// ⌘1–⌘9 for the first nine pages; the rest have none (a two-digit "key" would trap).
    var shortcut: KeyEquivalent? {
        guard let i = Page.allCases.firstIndex(of: self), i < 9 else { return nil }
        return KeyEquivalent(Character(String(i + 1)))
    }
}

/// Which Polymarket the bot scans and trades: two separate exchanges with their own markets and accounts.
enum Venue: String, CaseIterable, Identifiable {
    case us, international
    var id: String { rawValue }

    var title: String { self == .us ? "Polymarket US" : "Polymarket international" }
    var site: String { self == .us ? "polymarket.us" : "polymarket.com" }

    var explanation: String {
        switch self {
        case .us: return "The US exchange at polymarket.us, for US residents: sign in with Apple, Google or email, ID verified, no crypto wallet."
        case .international: return "polymarket.com, the original crypto-based exchange. Not available to US residents."
        }
    }
}

enum TradingMode: String, CaseIterable, Identifiable {
    case off, dryRun, live
    var id: String { rawValue }

    var title: String {
        switch self {
        case .off: return "Off"
        case .dryRun: return "Dry run: log what it would buy"
        case .live: return "Live: place real orders"
        }
    }

    var short: String {
        switch self {
        case .off: return "Off"
        case .dryRun: return "Dry run"
        case .live: return "LIVE"
        }
    }

    var explanation: String {
        switch self {
        case .off: return "The bot scans and paper trades but doesn't trade your account."
        case .dryRun: return "Re-checks each qualifying opportunity against fresh order books and logs the orders it would place. Nothing is bought."
        case .live: return "Places real orders from your connected account within the limits below. A partial fill halts trading until you resume it."
        }
    }
}

final class AppModel: ObservableObject {
    static let shared = AppModel()

    let backend = Backend()
    let web = WebStore()
    let account = AccountStore()

    @Published var selection: Page? = .overview {
        didSet {
            guard let page = selection, page.rawValue != currentRoute else { return }
            currentRoute = page.rawValue
            web.navigate(to: page.rawValue)
        }
    }

    @Published var openAtLogin: Bool = LoginItem.isEnabled {
        didSet { LoginItem.set(openAtLogin) }
    }
    @Published var keepRunningInMenuBar: Bool {
        didSet {
            UserDefaults.standard.set(keepRunningInMenuBar, forKey: "keepRunningInMenuBar")
            statusBar?.setVisible(keepRunningInMenuBar)
        }
    }
    @Published var preventSleep: Bool {
        didSet {
            UserDefaults.standard.set(preventSleep, forKey: "preventSleep")
            sleepBlocker.set(preventSleep)
        }
    }
    weak var mainWindow: NSWindow?
    private let sleepBlocker = SleepBlocker()
    private var statusBar: StatusBarController?

    @Published private(set) var tradingMode: TradingMode
    @Published var paperTrading: Bool {
        didSet {
            UserDefaults.standard.set(paperTrading, forKey: "paperTrading")
            restartScanner()
        }
    }

    private var currentRoute = Page.overview.rawValue
    private var cancellables = Set<AnyCancellable>()

    private init() {
        let d = UserDefaults.standard
        d.register(defaults: ["paperTrading": true, "keepRunningInMenuBar": true, "preventSleep": true])
        keepRunningInMenuBar = d.bool(forKey: "keepRunningInMenuBar")
        preventSleep = d.bool(forKey: "preventSleep")
        if let raw = d.string(forKey: "tradingMode"), let mode = TradingMode(rawValue: raw) {
            tradingMode = mode
        } else {
            tradingMode = d.bool(forKey: "dryRunTrading") ? .dryRun : .off  // earlier versions
        }
        paperTrading = d.bool(forKey: "paperTrading")
        if tradingMode == .live && !account.isConnected { tradingMode = .off }

        // Links inside the dashboard (e.g. a leg's "chart") move the sidebar selection.
        web.onRoute = { [weak self] name in
            guard let self else { return }
            self.currentRoute = name
            if let page = Page(rawValue: name), self.selection != page { self.selection = page }
        }
        backend.$state
            .sink { [weak self] state in
                guard let self, case .running(let url) = state else { return }
                self.web.load(base: url, route: self.currentRoute)
            }
            .store(in: &cancellables)
        backend.onNewArbitrage = { arbs in
            for arb in arbs.prefix(3) {
                Notifier.post(title: String(format: "Arbitrage: $%.2f guaranteed", arb.profit),
                              body: arb.title, route: arb.route)
            }
        }
        backend.onHalted = { reason in
            Notifier.post(title: "Trading halted", body: reason, route: Page.trading.rawValue)
        }
        // Forward nested changes so views observing the model refresh.
        account.objectWillChange.sink { [weak self] in self?.objectWillChange.send() }.store(in: &cancellables)

        Notifier.requestAuthorization()
        sleepBlocker.set(preventSleep)
        restartScanner()
    }

    /// Called once the app has finished launching (the menu bar needs a running app).
    func didFinishLaunching() {
        statusBar = StatusBarController(model: self)
        statusBar?.setVisible(keepRunningInMenuBar)
        if LoginItem.isEnabled { LoginItem.set(true) }  // refresh an older login item to start in the background
        // With the menu-bar icon on, closing the main window leaves pmscan running in the menu bar only.
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) {
            [weak self] note in
            guard let self, self.keepRunningInMenuBar, let window = note.object as? NSWindow,
                  window === self.mainWindow else { return }
            DispatchQueue.main.async { self.hideToMenuBar() }
        }
        if keepRunningInMenuBar && CommandLine.arguments.contains(LoginItem.backgroundFlag) {
            hideToMenuBar()
            DispatchQueue.main.async {
                for window in NSApp.windows where window.isVisible && window.canBecomeMain { window.close() }
            }
        }
    }

    /// No window and no Dock icon: the scanner keeps running behind the menu-bar icon.
    func hideToMenuBar() {
        guard keepRunningInMenuBar else { return }
        NSApp.setActivationPolicy(.accessory)
    }

    func showMainWindow() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if let window = mainWindow {
            window.makeKeyAndOrderFront(nil)
        } else if let url = URL(string: "pmscan://main") {
            NSWorkspace.shared.open(url)  // asks SwiftUI for a fresh main window
        }
    }

    func restartScanner() {
        var env = tradingMode == .live ? account.tradingEnvironment : nil
        if tradingMode == .live && env == nil {
            tradingMode = .off  // the key vanished from the Keychain
        }
        // Polymarket US streams prices only to connected accounts, so the live feed needs the API key
        // even when not trading. Orders still need Live mode (--live).
        if env == nil && account.venue == .us && account.usConnected { env = account.tradingEnvironment }
        if let odds = Keychain.get(account: Self.oddsKeyAccount) {
            env = (env ?? [:]).merging(["PMSCAN_ODDS_API_KEY": odds]) { a, _ in a }
        }
        backend.start(mode: tradingMode, paper: paperTrading, venue: account.venue, environment: env ?? [:])
    }

    // MARK: sportsbook comparison

    static let oddsKeyAccount = "oddsApiKey"
    @Published private(set) var oddsKeySet = Keychain.get(account: AppModel.oddsKeyAccount) != nil

    /// Checks an Odds API key (the sports list is free: no credits used), then keeps it in the Keychain.
    func saveOddsKey(_ key: String, done: @escaping (String?, Bool) -> Void) {
        var components = URLComponents(string: "https://api.the-odds-api.com/v4/sports")!
        components.queryItems = [URLQueryItem(name: "apiKey", value: key)]
        URLSession.shared.dataTask(with: components.url!) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                let http = response as? HTTPURLResponse
                guard error == nil, let http, http.statusCode == 200 else {
                    let why = http.map { $0.statusCode == 401 ? "The Odds API didn't accept that key." : "The Odds API answered \($0.statusCode)." }
                        ?? (error?.localizedDescription ?? "No answer from the Odds API.")
                    done(why, false)
                    return
                }
                guard Keychain.set(key, account: Self.oddsKeyAccount) else {
                    done("Couldn't save the key to the Keychain.", false)
                    return
                }
                self.oddsKeySet = true
                let left = http.value(forHTTPHeaderField: "x-requests-remaining").map { " \($0) requests left this month." } ?? ""
                done("Saved. Sportsbook comparison is on.\(left)", true)
                self.restartScanner()
            }
        }.resume()
    }

    func removeOddsKey() {
        Keychain.delete(account: Self.oddsKeyAccount)
        oddsKeySet = false
        restartScanner()
    }

    /// Switching exchanges: each has its own markets, results, paper portfolios and account.
    func setVenue(_ venue: Venue) {
        guard venue != account.venue else { return }
        if tradingMode == .live {
            tradingMode = .off
            UserDefaults.standard.set(TradingMode.off.rawValue, forKey: "tradingMode")
        }
        account.venue = venue
        restartScanner()
    }

    /// Changing the trading mode. Going live needs a connected account and an explicit confirmation.
    func requestTradingMode(_ mode: TradingMode) {
        guard mode != tradingMode else { return }
        if mode == .live {
            guard account.isConnected else {
                let alert = NSAlert()
                alert.messageText = "Connect your account first"
                alert.informativeText = "Live trading needs your \(account.venue.title) account. Open Settings › Account to connect it."
                alert.addButton(withTitle: "Open Settings")
                alert.addButton(withTitle: "Cancel")
                if alert.runModal() == .alertFirstButtonReturn { openSettings() }
                return
            }
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Start live trading?"
            alert.informativeText = """
            pmscan will place real orders on \(account.venue.title) from \(account.displayName) whenever an opportunity qualifies.

            \(backend.limitsDescription ?? "Defaults: neg-risk arbitrage only, up to $25 per trade and $100 per day.")

            Every order is checked against fresh order books first, and a partial fill stops trading. Limits are in pmscan.toml.
            """
            alert.addButton(withTitle: "Start Live Trading")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        tradingMode = mode
        UserDefaults.standard.set(mode.rawValue, forKey: "tradingMode")
        restartScanner()
    }

    func openSettings() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        NSApp.sendAction(Selector(("showPreferencesWindow:")), to: nil, from: nil)
    }

    func openInBrowser() {
        guard case .running(let url) = backend.state,
              let target = URL(string: "\(url.absoluteString)#/\(currentRoute)") else { return }
        NSWorkspace.shared.open(target)
    }

    func revealDataFolder() {
        guard let config = BackendConfig.load() else { return }
        var data = URL(fileURLWithPath: config.projectDir).appendingPathComponent("data")
        if account.venue == .us { data.appendPathComponent("us") }
        try? FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
        NSWorkspace.shared.activateFileViewerSelecting([data])
    }

    /// Opens pmscan.toml (created from the example on first use) in the default editor.
    func editConfig() {
        guard let config = BackendConfig.load() else { return }
        let dir = URL(fileURLWithPath: config.projectDir)
        let toml = dir.appendingPathComponent("pmscan.toml")
        if !FileManager.default.fileExists(atPath: toml.path) {
            try? FileManager.default.copyItem(at: dir.appendingPathComponent("pmscan.example.toml"), to: toml)
        }
        let editor = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.TextEdit")
        if let editor {
            NSWorkspace.shared.open([toml], withApplicationAt: editor, configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(toml)
        }
    }

    func showLog() { NSWorkspace.shared.open(Backend.logURL) }
}
