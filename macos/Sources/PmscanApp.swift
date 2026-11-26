import AppKit
import SwiftUI
import UserNotifications

@main
struct PmscanApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel.shared

    init() {
        SingleInstance.exitIfAlreadyRunning()
    }

    var body: some Scene {
        WindowGroup("pmscan") {
            ContentView()
                .environmentObject(model)
                .environmentObject(model.backend)
                .environmentObject(model.account)
                .frame(minWidth: 980, minHeight: 640)
                .background(WindowAccessor { window in if let window { model.mainWindow = window } })
                .handlesExternalEvents(preferring: ["main"], allowing: ["*"])
        }
        .handlesExternalEvents(matching: ["main"])
        .commands {
            SidebarCommands()
            CommandGroup(replacing: .newItem) {}  // one window is enough
            CommandMenu("Go") {
                ForEach(Page.allCases) { page in
                    if let key = page.shortcut {
                        Button(page.title) { model.selection = page }
                            .keyboardShortcut(key, modifiers: .command)
                    } else {
                        Button(page.title) { model.selection = page }
                    }
                }
            }
            CommandMenu("Scanner") {
                Button("Reload View") { model.web.reload() }
                    .keyboardShortcut("r", modifiers: .command)
                Button("Restart Scanner") { model.restartScanner() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                Picker("Automatic Trading", selection: Binding(get: { model.tradingMode },
                                                               set: { model.requestTradingMode($0) })) {
                    ForEach(TradingMode.allCases) { Text($0.short).tag($0) }
                }
                Toggle("Paper Trading", isOn: $model.paperTrading)
                Button("Account and Trading Settings…") { model.openSettings() }
                Divider()
                Button("Open in Browser") { model.openInBrowser() }
                Button("Show Data Folder") { model.revealDataFolder() }
                Button("Show Scanner Log") { model.showLog() }
            }
        }
        Settings {
            SettingsView()
                .environmentObject(model)
                .environmentObject(model.backend)
                .environmentObject(model.account)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        AppModel.shared.didFinishLaunching()
        // First launch: open at a comfortable size (later launches restore the user's own size).
        DispatchQueue.main.async {
            guard !UserDefaults.standard.bool(forKey: "didSizeWindow"), let window = NSApp.windows.first else { return }
            UserDefaults.standard.set(true, forKey: "didSizeWindow")
            let screen = window.screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            window.setContentSize(NSSize(width: min(1360, screen.width - 80), height: min(900, screen.height - 60)))
            window.center()
        }
    }

    // With the menu-bar icon on, closing the window keeps the scanner and trading running.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !AppModel.shared.keepRunningInMenuBar
    }

    // Opening pmscan again (Finder, Spotlight, the Dock) while it runs in the menu bar brings the window back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { AppModel.shared.showMainWindow() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppModel.shared.backend.stopAndWait()
    }

    // Show banners even while the app is in front.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    // Clicking a notification brings the app forward on the page it's about.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        let route = response.notification.request.content.userInfo["route"] as? String
        await MainActor.run {
            NSApp.activate(ignoringOtherApps: true)
            if let route, let page = Page(rawValue: route) { AppModel.shared.selection = page }
        }
    }
}
