import AppKit
import WebKit

/// Developer aid for checking the app's look without screen-recording permission:
///   PMSCAN_SNAPSHOT_DIR=/tmp/shots [PMSCAN_SNAPSHOT_PAGES=overview,ladders] [PMSCAN_SNAPSHOT_QUIT=1] \
///     dist/pmscan.app/Contents/MacOS/pmscan
/// After the dashboard loads, visits each page and writes <page>-window.png (native chrome)
/// and <page>-web.png (page content), then optionally quits.
enum SnapshotTour {
    static var started = false

    static func runIfRequested(model: AppModel) {
        let env = ProcessInfo.processInfo.environment
        guard !started, let dir = env["PMSCAN_SNAPSHOT_DIR"] else { return }
        started = true
        let pages = (env["PMSCAN_SNAPSHOT_PAGES"] ?? "overview").split(separator: ",").map(String.init)
        let quit = env["PMSCAN_SNAPSHOT_QUIT"] == "1"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        visit(pages[...], model: model, dir: URL(fileURLWithPath: dir), quit: quit)
    }

    private static func visit(_ pages: ArraySlice<String>, model: AppModel, dir: URL, quit: Bool) {
        guard let name = pages.first else {
            if quit { NSApp.terminate(nil) }
            return
        }
        if name.hasPrefix("settings") {  // the Settings window: native controls only
            // "settings-deposit" / "settings-proxy" preview step 2 with a made-up account (layout only).
            if name != "settings" {
                let deposit = name == "settings-deposit"
                model.account.checked = CheckedAccount(
                    wallet: "0x0000000000000000000000000000000000000001", walletType: deposit ? "DEPOSIT_WALLET" : "POLY_PROXY",
                    walletTypeLabel: deposit ? "Deposit Wallet" : "Proxy Wallet (older email or Google account)",
                    balance: "$0.00 pUSD", sessionKeysSupported: deposit)
            }
            model.openSettings()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if let frame = NSApp.keyWindow?.contentView?.superview ?? NSApp.windows.last(where: { $0.isVisible })?.contentView?.superview,
                   let rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) {
                    frame.cacheDisplay(in: frame.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("\(name)-window.png"))
                }
                NSApp.keyWindow?.close()
                visit(pages.dropFirst(), model: model, dir: dir, quit: quit)
            }
            return
        }
        guard let page = Page(rawValue: name) else { return visit(pages.dropFirst(), model: model, dir: dir, quit: quit) }
        model.selection = page
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            if let frame = NSApp.windows.first(where: { $0.isVisible && $0.title != "Account" && $0.title != "Trading" })?.contentView?.superview,
               let rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) {
                frame.cacheDisplay(in: frame.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?
                    .write(to: dir.appendingPathComponent("\(page.rawValue)-window.png"))
            }
            model.web.webView.takeSnapshot(with: nil) { image, _ in
                if let tiff = image?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) {
                    try? rep.representation(using: .png, properties: [:])?
                        .write(to: dir.appendingPathComponent("\(page.rawValue)-web.png"))
                }
                visit(pages.dropFirst(), model: model, dir: dir, quit: quit)
            }
        }
    }
}
