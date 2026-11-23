import AppKit
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var backend: Backend

    var body: some View {
        NavigationView {
            Sidebar()
            Detail()
        }
        .navigationTitle(model.selection?.title ?? "pmscan")
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Button(action: toggleSidebar) { Image(systemName: "sidebar.left") }
                    .help("Show or hide the sidebar")
            }
            ToolbarItemGroup(placement: .primaryAction) {
                ScanStatusView()
                Button { model.web.reload() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Reload the page (⌘R)")
                Button { model.openInBrowser() } label: { Image(systemName: "safari") }
                    .help("Open this page in your browser")
            }
        }
    }

    private func toggleSidebar() {
        NSApp.keyWindow?.firstResponder?.tryToPerform(#selector(NSSplitViewController.toggleSidebar(_:)), with: nil)
    }
}

struct Sidebar: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var backend: Backend

    var body: some View {
        List(selection: $model.selection) {
            Section("Opportunities") {
                ForEach([Page.overview, .arbitrage, .ladders, .sportsbook, .highProb, .nearCertain]) { row($0) }
            }
            Section("Trading") {
                ForEach([Page.paper, .rewards, .backtest, .trading, .markets]) { row($0) }
            }
        }
        .listStyle(.sidebar)
        .frame(minWidth: 210)
        .safeAreaInset(edge: .bottom) { SidebarFooter() }
    }

    private func row(_ page: Page) -> some View {
        Label(page.title, systemImage: page.symbol)
            .badge(page == .paper ? backend.paperOpen : page.countKey.flatMap { backend.counts[$0] } ?? 0)
            .tag(page)
    }
}

struct SidebarFooter: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var backend: Backend

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            Button { model.openSettings() } label: {
                Label(model.account.venue.title, systemImage: "building.columns")
                    .font(.caption).foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
            .help("The exchange pmscan scans and trades. Change it in Settings.")
            Text("Guaranteed profit now").font(.caption).foregroundColor(.secondary)
            Text(backend.arbitrageProfit, format: .currency(code: "USD"))
                .font(.system(size: 20, weight: .semibold))
            if let equity = backend.paperEquity {
                HStack(spacing: 4) {
                    Text("Paper").foregroundColor(.secondary)
                    Text(equity, format: .currency(code: "USD"))
                    if let pnl = backend.paperPnl {
                        Text("(\(pnl >= 0 ? "+" : "−")\(abs(pnl), format: .currency(code: "USD")))").foregroundColor(.secondary)
                    }
                }
                .font(.caption)
            }
            if let halted = backend.halted {
                Label("Trading halted", systemImage: "exclamationmark.octagon.fill")
                    .font(.caption).foregroundColor(.red).help(halted)
            } else {
                HStack(spacing: 6) {
                    Text("Trading").font(.caption).foregroundColor(.secondary)
                    Picker("Trading", selection: Binding(get: { model.tradingMode }, set: { model.requestTradingMode($0) })) {
                        ForEach(TradingMode.allCases) { Text($0.short).tag($0) }
                    }
                    .labelsHidden()
                    .controlSize(.small)
                    .frame(maxWidth: 110)
                    if model.tradingMode == .live {
                        Image(systemName: "bolt.fill").foregroundColor(.orange).help("Placing real orders")
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.bottom, 12)
    }
}

struct ScanStatusView: View {
    @EnvironmentObject private var backend: Backend

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            HStack(spacing: 6) {
                if backend.scanning { ProgressView().controlSize(.small) }
                Text(text(now: context.date)).font(.callout).foregroundColor(.secondary).monospacedDigit()
            }
            .frame(minWidth: 210, alignment: .trailing)
        }
    }

    private func text(now: Date) -> String {
        switch backend.state {
        case .starting: return "Starting scanner…"
        case .failed: return "Scanner stopped"
        case .running:
            if backend.scanning { return "Scanning…" }
            if backend.scanError != nil { return "Last scan failed" }
            if let next = backend.nextScanAt {
                return "Next scan in \(max(0, Int(next.timeIntervalSince(now))))s\(backend.liveFeed ? " · live feed" : "")"
            }
            return "Waiting for first scan"
        }
    }
}

struct Detail: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var backend: Backend

    var body: some View {
        switch backend.state {
        case .starting:
            VStack(spacing: 12) {
                ProgressView()
                Text("Starting the scanner…").font(.headline)
                Text("The first scan of every market takes about 20–40 seconds.")
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            VStack(spacing: 14) {
                Image(systemName: "exclamationmark.triangle").font(.system(size: 34)).foregroundColor(.orange)
                Text("The scanner isn't running").font(.headline)
                Text(message).multilineTextAlignment(.center).foregroundColor(.secondary).frame(maxWidth: 460)
                HStack {
                    Button("Show Log") { model.showLog() }
                    Button("Try Again") { model.restartScanner() }.keyboardShortcut(.defaultAction)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .running:
            DashboardView(store: model.web)
        }
    }
}
