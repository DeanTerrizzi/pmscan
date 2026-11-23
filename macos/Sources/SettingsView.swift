import AppKit
import SwiftUI

struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            AccountSettings().tabItem { Label("Account", systemImage: "person.crop.circle") }
            TradingSettings().tabItem { Label("Trading", systemImage: "dollarsign.circle") }
        }
        .frame(width: 660)
        .padding(20)
    }
}

private func short(_ address: String?) -> String {
    guard let a = address, a.count > 12 else { return address ?? "" }
    return "\(a.prefix(6))…\(a.suffix(4))"
}

struct AccountSettings: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var account: AccountStore

    @State private var wallet = ""
    @State private var key = ""
    @State private var relayerKey = ""
    @State private var relayerAddress = ""
    @State private var approvals = true
    @State private var usKeyId = ""
    @State private var usSecret = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VenuePicker()
                if account.venue == .us {
                    if account.usConnected { usConnected } else { usSetup }
                } else if account.isConnected {
                    connected
                } else if let checked = account.checked {
                    stepOneDone(checked)
                    stepTwo(checked)
                } else {
                    stepOne
                }
                if let msg = account.message, !msg.isEmpty {
                    HStack(alignment: .top, spacing: 8) {
                        if account.busy { ProgressView().controlSize(.small) }
                        Text(msg)
                            .font(.callout)
                            .foregroundColor(account.messageIsError ? .red : .secondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.trailing, 8)
        }
        .frame(minHeight: 460)
    }

    // MARK: Polymarket US

    private var usConnected: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Connected to Polymarket US", systemImage: "checkmark.seal.fill").font(.headline).foregroundColor(.green)
            VStack(alignment: .leading, spacing: 6) {
                row("API key", AccountStore.mask(account.usKeyId))
                row("Secret key", "stored in this Mac's Keychain")
            }
            HStack {
                Button("Test Connection") { account.testConnection() }.disabled(account.busy)
                Button("Run $1 Test Trade…") { confirmTestTrade() }.disabled(account.busy)
                Button("Disconnect") {
                    if model.tradingMode == .live { model.requestTradingMode(.off) }
                    account.disconnect()
                }
                .disabled(account.busy)
            }
            .padding(.top, 4)
            Text("Before going live, run the test trade once: it buys about $1 of YES in a busy market and sells it straight back, then does the same with NO, so you can see real orders fill. It costs a few cents in spread and fees.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("The Polymarket US API has endpoints for trading and reading your account, none for withdrawals. To cut the bot off completely, delete the key at polymarket.us/developer.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var usSetup: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Text("Polymarket US accounts don't have a wallet or private key. The bot connects with an **API key** instead.")
                    .fixedSize(horizontal: false, vertical: true)
                instruction(1, "**Verify your identity** in the Polymarket US app if you haven't yet. API keys only work once verification is approved.", link: nil)
                instruction(2, "Open the developer page and **sign in the same way you sign into the app** (Apple, Google or email). Create a new API key. If it asks for an invite code, email support@polymarket.us.",
                            link: ("Open polymarket.us/developer", "https://polymarket.us/developer"))
                instruction(3, "Copy the **Key ID** and the **Secret Key** into the fields below. The secret is shown only once: if you lost it, delete that key and create another.", link: nil)
                Form {
                    TextField("Key ID", text: $usKeyId, prompt: Text("Key ID"))
                    SecureField("Secret Key", text: $usSecret, prompt: Text("the long secret shown once"))
                    hint(usSecret, valid: isUSSecret(usSecret), message: "That doesn't look like a Secret Key: it's a long code of letters, numbers, + and /, often ending in =.")
                }
                HStack {
                    Text("Checked with Polymarket US, then kept only in this Mac's Keychain.")
                        .font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Connect") {
                        account.connectUS(keyId: trimmed(usKeyId), secret: trimmed(usSecret)) { ok in
                            if ok { usKeyId = ""; usSecret = "" }
                        }
                    }
                    .disabled(account.busy || trimmed(usKeyId).isEmpty || !isUSSecret(usSecret))
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding(6)
        } label: {
            Text("Connect your Polymarket US account").font(.headline)
        }
    }

    // MARK: connected

    private var connected: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Connected", systemImage: "checkmark.seal.fill").font(.headline).foregroundColor(.green)
            VStack(alignment: .leading, spacing: 6) {
                row("Wallet", account.wallet ?? "")
                if account.keyKind == .session {
                    row("Bot signs with", "session key \(short(account.sessionAddress)): can trade, can't withdraw")
                    if let until = account.validUntil, !until.isEmpty { row("Valid until", until) }
                } else {
                    row("Bot signs with", "your account's own key")
                }
                row("Stored in", "this Mac's Keychain")
            }
            HStack {
                Button("Test Connection") { account.testConnection() }.disabled(account.busy)
                Button("Disconnect") {
                    if model.tradingMode == .live { model.requestTradingMode(.off) }
                    account.disconnect()
                }
                .disabled(account.busy)
            }
            .padding(.top, 4)
            Text(account.keyKind == .session
                 ? "The session key expires on its own after 180 days. To revoke it sooner, run `pmscan trade revoke-session --address \(account.sessionAddress ?? "<address>")` with your owner key."
                 : "Disconnecting removes the key from this Mac. If you ever think the key was exposed, move your funds to a new Polymarket account.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: step 1

    private var stepOne: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                instruction(1, "Copy your **wallet address**: on polymarket.com, click your profile picture (top right). The address under your name starts with 0x.",
                            link: ("Open polymarket.com", "https://polymarket.com"))
                instruction(2, "Copy your **private key**. For email or Google sign-ins, Polymarket keeps it at Magic: open the export page, sign in with the **same** email or Google account, and reveal the key.",
                            link: ("Open the key export page", "https://reveal.magic.link/polymarket"))
                instruction(3, "Paste both below and check the account. Nothing is saved yet.", link: nil)

                Form {
                    TextField("Wallet address", text: $wallet, prompt: Text("0x…"))
                    hint(wallet, valid: isAddress(wallet), message: "Should be 0x followed by 40 letters and numbers.")
                    SecureField("Private key", text: $key, prompt: Text("64 letters and numbers"))
                    hint(key, valid: isKey(key), message: "Should be 64 letters and numbers (0-9, a-f), optionally starting with 0x.")
                }
                .onChange(of: wallet) { _ in account.checked = nil }
                .onChange(of: key) { _ in account.checked = nil }

                HStack {
                    Text("Polymarket will never ask for this key. pmscan only uses it on this Mac.")
                        .font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Check Account") { account.checkAccount(wallet: trimmed(wallet), key: trimmed(key)) }
                        .disabled(account.busy || !isAddress(wallet) || !isKey(key))
                        .keyboardShortcut(account.checked == nil ? .defaultAction : .init(.return, modifiers: .option))
                }
            }
            .padding(6)
        } label: {
            Text("Step 1 · Your Polymarket wallet").font(.headline)
        }
    }

    private func stepOneDone(_ checked: CheckedAccount) -> some View {
        GroupBox {
            HStack {
                Label("Wallet \(short(checked.wallet)) · \(checked.walletTypeLabel)", systemImage: "checkmark.circle.fill")
                    .foregroundColor(.green)
                Spacer()
                Button("Change") { account.checked = nil }.disabled(account.busy)
            }
            .padding(4)
        } label: {
            Text("Step 1 · Your Polymarket wallet").font(.headline)
        }
    }

    // MARK: step 2

    @ViewBuilder
    private func stepTwo(_ checked: CheckedAccount) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Text("Found your account · balance **\(checked.balance)**")
                if checked.sessionKeysSupported {
                    Text("Your account supports **session keys**: a separate key that can place orders but **can't withdraw**, expires after 180 days, and can be revoked. Creating one takes a Relayer API key (free, made in your Polymarket settings). Your private key is then discarded.")
                        .fixedSize(horizontal: false, vertical: true)
                    instruction(1, "On Polymarket open **Settings › API Keys › Relayer API Keys** and create a key. Copy the **API Key** and the **Signer Address** shown with it.",
                                link: ("Open Polymarket API keys", "https://polymarket.com/settings?tab=api-keys"))
                    Form {
                        TextField("Relayer API key", text: $relayerKey, prompt: Text("the API Key"))
                        TextField("Signer address", text: $relayerAddress, prompt: Text("0x… shown with the key"))
                        hint(relayerAddress, valid: isAddress(relayerAddress), message: "Should be 0x followed by 40 letters and numbers.")
                        Toggle("Also set up trading approvals (needed once per wallet)", isOn: $approvals)
                    }
                    HStack {
                        Button("Use my private key instead") {
                            account.useExistingKey(wallet: checked.wallet, key: trimmed(key)) { ok in if ok { clear() } }
                        }
                        .buttonStyle(.link)
                        .help("Works, but that key can move funds. Session keys are safer.")
                        Spacer()
                        Button("Create Session Key") {
                            account.createSessionKey(wallet: checked.wallet, ownerKey: trimmed(key), relayerKey: trimmed(relayerKey),
                                                     relayerAddress: trimmed(relayerAddress), setUpApprovals: approvals) { ok in if ok { clear() } }
                        }
                        .disabled(account.busy || trimmed(relayerKey).isEmpty || !isAddress(relayerAddress))
                        .keyboardShortcut(.defaultAction)
                    }
                } else {
                    Text("This is an older account type, so it can't create session keys. The bot has to sign orders with your account's own key, which can also move funds. pmscan keeps it only in this Mac's Keychain.")
                        .fixedSize(horizontal: false, vertical: true)
                    Text("To limit the risk, keep only the money you want the bot to trade in this account. If you haven't traded multi-outcome markets on polymarket.com before, make one small trade there first: it sets up the approvals the bot's orders need.")
                        .font(.callout).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Spacer()
                        Button("Use This Key for Trading") {
                            account.useExistingKey(wallet: checked.wallet, key: trimmed(key)) { ok in if ok { clear() } }
                        }
                        .disabled(account.busy)
                        .keyboardShortcut(.defaultAction)
                    }
                }
            }
            .padding(6)
        } label: {
            Text("Step 2 · Connect the bot").font(.headline)
        }
    }

    // MARK: helpers

    private func instruction(_ n: Int, _ text: String, link: (String, String)?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("\(n).").foregroundColor(.secondary).monospacedDigit()
            VStack(alignment: .leading, spacing: 3) {
                Text(.init(text)).fixedSize(horizontal: false, vertical: true)
                if let link, let url = URL(string: link.1) {
                    Link("\(link.0) ↗", destination: url).font(.callout)
                }
            }
        }
    }

    @ViewBuilder
    private func hint(_ value: String, valid: Bool, message: String) -> some View {
        if !trimmed(value).isEmpty && !valid {
            Text(message).font(.caption).foregroundColor(.orange)
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundColor(.secondary).frame(width: 110, alignment: .leading)
            Text(value).textSelection(.enabled)
        }
    }

    private func confirmTestTrade() {
        let alert = NSAlert()
        alert.messageText = "Place a $1 test trade?"
        alert.informativeText = "pmscan buys about $1 of YES in a busy Polymarket US market and sells it straight back, then does the same with NO. This uses real money: expect to lose a few cents to the spread and fees. You need at least about $2 of buying power."
        alert.addButton(withTitle: "Place Test Trade")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn { account.runTestTrade() }
    }

    private func clear() { wallet = ""; key = ""; relayerKey = ""; relayerAddress = "" }
    private func trimmed(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }
    private func isAddress(_ s: String) -> Bool { trimmed(s).range(of: "^0x[0-9a-fA-F]{40}$", options: .regularExpression) != nil }
    private func isKey(_ s: String) -> Bool { trimmed(s).range(of: "^(0x)?[0-9a-fA-F]{64}$", options: .regularExpression) != nil }
    private func isUSSecret(_ s: String) -> Bool {
        guard let data = Data(base64Encoded: trimmed(s)) else { return false }
        return data.count == 32 || data.count == 64
    }
}

/// The Odds API key that turns on the sportsbook comparison.
struct SportsbookKeySettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var key = ""
    @State private var message: String?
    @State private var isError = false
    @State private var busy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Sportsbook comparison").font(.headline)
            Text("Compares Polymarket game prices with bookmakers' odds and lists the bets Polymarket prices too cheaply. It needs a free key from The Odds API (500 requests a month; pmscan paces itself to last the month).")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            if model.oddsKeySet {
                HStack {
                    Label("Odds API key saved in the Keychain", systemImage: "checkmark.circle.fill").foregroundColor(.green)
                    Spacer()
                    Button("Remove") { model.removeOddsKey(); message = nil }
                }
            } else {
                HStack {
                    SecureField("Odds API key", text: $key, prompt: Text("paste the key from the-odds-api.com"))
                    Button("Save") {
                        busy = true
                        model.saveOddsKey(key.trimmingCharacters(in: .whitespacesAndNewlines)) { text, ok in
                            busy = false; message = text; isError = !ok
                            if ok { key = "" }
                        }
                    }
                    .disabled(busy || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                Link("Get a free key at the-odds-api.com ↗", destination: URL(string: "https://the-odds-api.com/#get-access")!)
                    .font(.callout)
            }
            if let message {
                Text(message).font(.caption).foregroundColor(isError ? .red : .secondary)
            }
        }
    }
}

/// Polymarket US or polymarket.com: they're separate exchanges with separate accounts.
struct VenuePicker: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var account: AccountStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("Exchange", selection: Binding(get: { account.venue }, set: { model.setVenue($0) })) {
                ForEach(Venue.allCases) { Text("\($0.title) · \($0.site)").tag($0) }
            }
            .pickerStyle(.radioGroup)
            Text(account.venue.explanation + " Switching restarts the scanner; each exchange keeps its own results and paper portfolios.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct GeneralSettings: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Markets").font(.headline)
            VenuePicker()
            Divider()
            SportsbookKeySettings()
            Divider()
            Text("Keep the bot running").font(.headline)
            VStack(alignment: .leading, spacing: 10) {
                Toggle("Open pmscan in the background when you log in", isOn: $model.openAtLogin)
                Toggle("Keep running in the menu bar when the window is closed", isOn: $model.keepRunningInMenuBar)
                Toggle("Keep this Mac awake while pmscan runs", isOn: $model.preventSleep)
            }
            Text("Closing the window leaves pmscan running behind its menu-bar icon (no Dock icon); choose Open pmscan there to bring it back. At login it starts the same way, without a window. pmscan only scans, paper trades and trades while it's running and the Mac is awake. Keeping the Mac awake stops idle sleep; the display can still turn off. A laptop with its lid closed still sleeps unless it's on power with an external display.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .frame(minHeight: 560, alignment: .top)
    }
}

struct TradingSettings: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var backend: Backend
    @EnvironmentObject private var account: AccountStore

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Automatic trading").font(.headline)
                Picker("", selection: Binding(get: { model.tradingMode }, set: { model.requestTradingMode($0) })) {
                    ForEach(TradingMode.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
                Text(model.tradingMode.explanation).font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Limits").font(.headline)
                Text(backend.limitsDescription ?? "Defaults: neg-risk arbitrage only, $25 per trade, $100 per day.")
                    .font(.callout)
                Button("Edit Limits…") { model.editConfig() }
                Text("Limits, strategies and the paper stake live in pmscan.toml in the project folder. Changes apply after Restart Scanner (⇧⌘R).")
                    .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Toggle("Paper trading", isOn: $model.paperTrading).font(.headline)
                Text("A simulated portfolio that buys every listed position priced 90¢ or more, filled against the live order book. Nothing real is bought. See the Paper page.")
                    .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .frame(minHeight: 380, alignment: .top)
    }
}
