import AppKit
import SwiftUI
import WebKit

/// Owns the single web view that renders the dashboard pages inside the native window.
final class WebStore: NSObject, ObservableObject, WKUIDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let webView: WKWebView
    var onRoute: ((String) -> Void)?
    @Published private(set) var loaded = false

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        super.init()
        config.userContentController.add(self, name: "pmscan")
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.setValue(false, forKey: "drawsBackground")  // no white flash in dark mode
    }

    func load(base: URL, route: String) {
        loaded = false
        guard let url = URL(string: "\(base.absoluteString)?embedded=1#/\(route)") else { return }
        webView.load(URLRequest(url: url))
    }

    func navigate(to route: String) {
        webView.evaluateJavaScript("location.hash = '#/\(route)'", completionHandler: nil)
    }

    func reload() { webView.reload() }

    // Route changes reported by the dashboard (see EMBEDDED in static/app/main.js).
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], body["type"] as? String == "route",
              let name = body["name"] as? String else { return }
        onRoute?(name)
    }

    // Links that open a new window (Polymarket, The Odds API, ...) go to the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded = true
        SnapshotTour.runIfRequested(model: AppModel.shared)
    }
}

struct DashboardView: NSViewRepresentable {
    let store: WebStore
    func makeNSView(context: Context) -> WKWebView { store.webView }
    func updateNSView(_ view: WKWebView, context: Context) {}
}
