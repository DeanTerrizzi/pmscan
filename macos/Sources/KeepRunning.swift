import AppKit
import Combine
import IOKit.pwr_mgt
import SwiftUI

/// Opens pmscan at login with a per-user LaunchAgent (works on macOS 12, which lacks SMAppService).
/// It starts in the background: menu-bar icon only, no window or Dock icon.
enum LoginItem {
    static let backgroundFlag = "--background"

    private static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/app.pmscan.desktop.login.plist")
    }

    static var isEnabled: Bool { FileManager.default.fileExists(atPath: plistURL.path) }

    static func set(_ enabled: Bool) {
        if enabled {
            let plist: [String: Any] = [
                "Label": "app.pmscan.desktop.login",
                "ProgramArguments": ["/usr/bin/open", "-g", "-a", Bundle.main.bundlePath, "--args", backgroundFlag],
                "RunAtLoad": true,
            ]
            try? FileManager.default.createDirectory(at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0) {
                try? data.write(to: plistURL)
            }
        } else {
            try? FileManager.default.removeItem(at: plistURL)
        }
    }
}

/// Stops idle system sleep while pmscan runs (the display can still sleep).
final class SleepBlocker {
    private var assertion: IOPMAssertionID = 0
    private var held = false

    func set(_ enabled: Bool) {
        if enabled && !held {
            held = IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                                               IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                               "pmscan is scanning and trading" as CFString, &assertion) == kIOReturnSuccess
        } else if !enabled && held {
            IOPMAssertionRelease(assertion)
            held = false
        }
    }
}

/// The menu-bar icon: open arbitrage count in the bar, status and controls in its menu.
final class StatusBarController: NSObject, NSMenuDelegate {
    private var item: NSStatusItem?
    private var cancellables = Set<AnyCancellable>()
    private unowned let model: AppModel

    init(model: AppModel) {
        self.model = model
        super.init()
        model.backend.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in DispatchQueue.main.async { self?.refreshTitle() } }
            .store(in: &cancellables)
    }

    func setVisible(_ visible: Bool) {
        if visible, item == nil {
            let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            let image = NSImage(systemSymbolName: "chart.line.uptrend.xyaxis", accessibilityDescription: "pmscan")
            image?.isTemplate = true
            item.button?.image = image
            item.button?.imagePosition = .imageLeading
            let menu = NSMenu()
            menu.delegate = self
            item.menu = menu
            self.item = item
            refreshTitle()
        } else if !visible, let item {
            NSStatusBar.system.removeStatusItem(item)
            self.item = nil
        }
    }

    private var arbitrageCount: Int {
        (model.backend.counts["neg_risk_arb"] ?? 0) + (model.backend.counts["ladder_arb"] ?? 0)
    }

    private func refreshTitle() {
        let n = arbitrageCount
        item?.button?.title = n > 0 ? " \(n)" : ""
        item?.button?.toolTip = n > 0 ? "\(n) arbitrage opportunit\(n == 1 ? "y" : "ies") open" : "pmscan"
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let b = model.backend
        let money = NumberFormatter()
        money.numberStyle = .currency
        money.currencyCode = "USD"
        func info(_ text: String) {
            let i = NSMenuItem(title: text, action: nil, keyEquivalent: "")
            i.isEnabled = false
            menu.addItem(i)
        }
        info(statusLine(b))
        info("Guaranteed profit now: \(money.string(from: NSNumber(value: b.arbitrageProfit)) ?? "–") in \(arbitrageCount) basket\(arbitrageCount == 1 ? "" : "s")")
        if let equity = b.paperEquity {
            info("Paper equity: \(money.string(from: NSNumber(value: equity)) ?? "–")")
        }
        info("Trading: \(model.tradingMode.short)\(b.halted != nil ? " (halted)" : "")")
        menu.addItem(.separator())
        add(menu, "Open pmscan", #selector(open), "o")
        add(menu, "Settings…", #selector(settings), ",")
        menu.addItem(.separator())
        add(menu, "Quit pmscan", #selector(quit), "q")
    }

    private func statusLine(_ b: Backend) -> String {
        switch b.state {
        case .starting: return "Starting scanner…"
        case .failed: return "Scanner stopped"
        case .running:
            if b.scanning { return "Scanning…" }
            if let next = b.nextScanAt { return "Next scan in \(max(0, Int(next.timeIntervalSinceNow)))s\(b.liveFeed ? " · live feed on" : "")" }
            return "Running"
        }
    }

    private func add(_ menu: NSMenu, _ title: String, _ action: Selector, _ key: String) {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        menu.addItem(i)
    }

    @objc private func open() { model.showMainWindow() }
    @objc private func settings() { model.openSettings() }
    @objc private func quit() { NSApp.terminate(nil) }
}

/// Captures the SwiftUI window hosting a view, so the menu bar can bring it back.
struct WindowAccessor: NSViewRepresentable {
    let onWindow: (NSWindow?) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { onWindow(view.window) }
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async { onWindow(view.window) }
    }
}
