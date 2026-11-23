import Foundation
import Security

/// The signing key lives only in the macOS Keychain; non-secret details live in UserDefaults.
enum Keychain {
    private static let service = "app.pmscan.desktop"

    static func set(_ value: String, account: String) -> Bool {
        delete(account: account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }

    static func get(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func delete(account: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        return SecItemDelete(query as CFDictionary) == errSecSuccess
    }
}

/// Runs `python -m pmscan.cli …` from the project and captures its output.
enum CLI {
    static func run(_ arguments: [String], environment extra: [String: String] = [:],
                    completion: @escaping (_ status: Int32, _ output: String) -> Void) {
        guard let config = BackendConfig.load() else {
            completion(-1, "This build doesn't know where the pmscan project is.")
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: config.python)
        proc.arguments = ["-m", "pmscan.cli"] + arguments
        proc.currentDirectoryURL = URL(fileURLWithPath: config.projectDir)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["COLUMNS"] = "200"
        extra.forEach { env[$0.key] = $0.value }
        proc.environment = env
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                try proc.run()
            } catch {
                DispatchQueue.main.async { completion(-1, error.localizedDescription) }
                return
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()  // read before waiting: no pipe deadlock
            proc.waitUntilExit()
            let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            DispatchQueue.main.async { completion(proc.terminationStatus, text) }
        }
    }
}

struct CheckedAccount {
    let wallet: String
    let walletType: String
    let walletTypeLabel: String
    let balance: String
    let sessionKeysSupported: Bool
}

/// The Polymarket account the bot trades for: a polymarket.com wallet, or a Polymarket US API key.
final class AccountStore: ObservableObject {
    enum KeyKind: String { case session, owner }

    static let logURL: URL = Backend.logURL.deletingLastPathComponent().appendingPathComponent("account.log")
    private static let usSecretAccount = "usSecretKey"

    /// Which exchange the bot scans and trades. Polymarket US unless chosen otherwise.
    @Published var venue: Venue = Venue(rawValue: UserDefaults.standard.string(forKey: "venue") ?? "") ?? .us {
        didSet { UserDefaults.standard.set(venue.rawValue, forKey: "venue") }
    }
    @Published private(set) var usKeyId: String? = UserDefaults.standard.string(forKey: "account.usKeyId")

    @Published private(set) var wallet: String? = UserDefaults.standard.string(forKey: "account.wallet")
    @Published private(set) var keyKind: KeyKind? = UserDefaults.standard.string(forKey: "account.keyKind").flatMap(KeyKind.init)
    @Published private(set) var sessionAddress: String? = UserDefaults.standard.string(forKey: "account.sessionAddress")
    @Published private(set) var validUntil: String? = UserDefaults.standard.string(forKey: "account.validUntil")
    @Published var checked: CheckedAccount?
    @Published var busy = false
    @Published var message: String?
    @Published var messageIsError = false

    /// Connected for the selected exchange.
    var isConnected: Bool { venue == .us ? usConnected : walletConnected }
    var usConnected: Bool { usKeyId != nil && Keychain.get(account: Self.usSecretAccount) != nil }
    var walletConnected: Bool { wallet != nil && Keychain.get(account: "signingKey") != nil }

    /// How the connected account is described in confirmations.
    var displayName: String {
        venue == .us ? "API key \(Self.mask(usKeyId))" : "wallet \(wallet ?? "")"
    }

    static func mask(_ s: String?) -> String {
        guard let s, s.count > 10 else { return s ?? "" }
        return "\(s.prefix(4))…\(s.suffix(4))"
    }

    /// Environment the scanner needs to trade live on the selected exchange.
    var tradingEnvironment: [String: String]? {
        if venue == .us {
            guard let id = usKeyId, let secret = Keychain.get(account: Self.usSecretAccount) else { return nil }
            return ["POLYMARKET_US_KEY_ID": id, "POLYMARKET_US_SECRET_KEY": secret]
        }
        guard let wallet, let key = Keychain.get(account: "signingKey") else { return nil }
        return ["POLYMARKET_PRIVATE_KEY": key, "POLYMARKET_WALLET_ADDRESS": wallet]
    }

    // MARK: Polymarket US

    /// Checks the API key against Polymarket US and, if it works, keeps the secret in the Keychain.
    func connectUS(keyId: String, secret: String, done: @escaping (Bool) -> Void) {
        start("Connecting to Polymarket US…")
        let env = ["POLYMARKET_US_KEY_ID": keyId, "POLYMARKET_US_SECRET_KEY": secret]
        CLI.run(["trade", "check-account", "--venue", "us", "--json"], environment: env) { [weak self] status, output in
            guard let self else { return }
            guard status == 0, let obj = Self.lastJSON(output), obj["error"] == nil else {
                self.fail("check-account (us)", output, fallback: "Couldn't connect with that API key.")
                done(false)
                return
            }
            guard Keychain.set(secret, account: Self.usSecretAccount) else {
                self.finish(error: "Couldn't save the key to the Keychain.")
                done(false)
                return
            }
            UserDefaults.standard.set(keyId, forKey: "account.usKeyId")
            self.usKeyId = keyId
            self.finish(info: "Connected to Polymarket US · \(obj["collateral_balance"] as? String ?? "balance unknown").")
            done(true)
        }
    }

    /// Step 1: connect with the pasted wallet and key and find out what kind of account it is.
    /// Nothing is stored yet.
    func checkAccount(wallet: String, key: String) {
        checked = nil
        start("Connecting to Polymarket…")
        CLI.run(["trade", "check-account", "--json"],
                environment: ["POLYMARKET_PRIVATE_KEY": key, "POLYMARKET_WALLET_ADDRESS": wallet]) { [weak self] status, output in
            guard let self else { return }
            guard status == 0, let obj = Self.lastJSON(output), obj["error"] == nil else {
                self.fail("check-account", output, fallback: "Couldn't connect to that account.")
                return
            }
            self.checked = CheckedAccount(
                wallet: obj["wallet"] as? String ?? wallet,
                walletType: obj["wallet_type"] as? String ?? "",
                walletTypeLabel: obj["wallet_type_label"] as? String ?? "",
                balance: obj["collateral_balance"] as? String ?? "?",
                sessionKeysSupported: obj["session_keys_supported"] as? Bool ?? false)
            self.finish(info: "")
        }
    }

    /// Uses the owner's key once to authorize a trading-only session key; only the session key is kept.
    func createSessionKey(wallet: String, ownerKey: String, relayerKey: String, relayerAddress: String,
                          setUpApprovals: Bool, done: @escaping (Bool) -> Void) {
        start("Creating a session key and authorizing it for your wallet…")
        let env = [
            "POLYMARKET_OWNER_PRIVATE_KEY": ownerKey, "POLYMARKET_WALLET_ADDRESS": wallet,
            "POLYMARKET_RELAYER_API_KEY": relayerKey, "POLYMARKET_RELAYER_API_KEY_ADDRESS": relayerAddress,
        ]
        let args = ["trade", "authorize-session", "--json"] + (setUpApprovals ? [] : ["--no-approvals"])
        CLI.run(args, environment: env) { [weak self] status, output in
            guard let self else { return }
            guard status == 0, let obj = Self.lastJSON(output),
                  let key = obj["session_private_key"] as? String, let walletOut = obj["wallet"] as? String else {
                self.fail("authorize-session", output, fallback: "Creating the session key failed.")
                done(false)
                return
            }
            guard Keychain.set(key, account: "signingKey") else {
                self.finish(error: "Couldn't save the key to the Keychain.")
                done(false)
                return
            }
            self.save(wallet: walletOut, kind: .session, session: obj["session_address"] as? String,
                      validUntil: obj["valid_until"] as? String)
            self.checked = nil
            self.finish(info: "Connected. The session key can place orders but can't withdraw funds.")
            done(true)
        }
    }

    /// For wallets that don't support session keys: store the account's own key.
    func useExistingKey(wallet: String, key: String, done: @escaping (Bool) -> Void) {
        guard Keychain.set(key, account: "signingKey") else {
            finish(error: "Couldn't save the key to the Keychain.")
            done(false)
            return
        }
        save(wallet: wallet, kind: .owner, session: nil, validUntil: nil)
        checked = nil
        finish(info: "Connected. The bot signs orders with this key, stored in this Mac's Keychain.")
        done(true)
    }

    /// Buys about $1 of YES and NO in a busy market and sells both straight back, to prove the order path.
    func runTestTrade() {
        guard venue == .us, let env = tradingEnvironment else {
            finish(error: "Connect your Polymarket US account first.")
            return
        }
        start("Placing the $1 test trade… (about 10 seconds)")
        CLI.run(["trade", "test-order", "--venue", "us", "--yes", "--json"], environment: env) { [weak self] _, output in
            guard let self else { return }
            guard let obj = Self.lastJSON(output), obj["error"] == nil, let verdict = obj["verdict"] as? String else {
                self.fail("test-order", output, fallback: "The test trade didn't run.")
                return
            }
            let steps = (obj["steps"] as? [[String: Any]] ?? []).map { s -> String in
                let ok = s["ok"] as? Bool ?? false
                let detail = (s["message"] as? String).map { " (\($0))" } ?? ""
                return "\(ok ? "✓" : "✗") \(s["step"] as? String ?? "")\(detail)"
            }
            let market = obj["market"] as? String ?? ""
            let text = ([verdict, "Market: \(market)"] + steps).joined(separator: "\n")
            if obj["ok"] as? Bool == true { self.finish(info: text) } else { self.finish(error: text) }
        }
    }

    func testConnection(done: ((Bool) -> Void)? = nil) {
        guard let env = tradingEnvironment else {
            finish(error: "No account connected.")
            done?(false)
            return
        }
        start("Checking the connection…")
        CLI.run(["trade", "status", "--venue", venue.rawValue, "--json"], environment: env) { [weak self] status, output in
            guard let self else { return }
            if status == 0, let obj = Self.lastJSON(output), obj["error"] == nil {
                let balance = obj["collateral_balance"] as? String ?? "?"
                self.finish(info: "Connected to \(obj["wallet"] as? String ?? "your account"). Balance \(balance).")
                done?(true)
            } else {
                self.fail("status", output, fallback: "The connection test failed.")
                done?(false)
            }
        }
    }

    func disconnect() {
        if venue == .us {
            Keychain.delete(account: Self.usSecretAccount)
            UserDefaults.standard.removeObject(forKey: "account.usKeyId")
            usKeyId = nil
            finish(info: "Disconnected. The secret key was removed from this Mac's Keychain. You can also delete the key at polymarket.us/developer.")
            return
        }
        Keychain.delete(account: "signingKey")
        for key in ["wallet", "keyKind", "sessionAddress", "validUntil"] {
            UserDefaults.standard.removeObject(forKey: "account.\(key)")
        }
        wallet = nil; keyKind = nil; sessionAddress = nil; validUntil = nil
        finish(info: "Disconnected. The key was removed from this Mac's Keychain.")
    }

    private func save(wallet: String, kind: KeyKind, session: String?, validUntil: String?) {
        let d = UserDefaults.standard
        d.set(wallet, forKey: "account.wallet")
        d.set(kind.rawValue, forKey: "account.keyKind")
        d.set(session, forKey: "account.sessionAddress")
        d.set(validUntil, forKey: "account.validUntil")
        self.wallet = wallet; self.keyKind = kind; self.sessionAddress = session; self.validUntil = validUntil
    }

    /// The last line of CLI output, parsed as a JSON object.
    static func lastJSON(_ output: String) -> [String: Any]? {
        guard let line = output.split(separator: "\n").last else { return nil }
        return (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
    }

    /// Shows a readable error and appends it, with anything key-shaped removed, to account.log.
    private func fail(_ action: String, _ output: String, fallback: String) {
        let message = (Self.lastJSON(output)?["error"] as? String) ?? (output.isEmpty ? fallback : output)
        let redacted = output
            .replacingOccurrences(of: "(0x)?[0-9a-fA-F]{64}", with: "<redacted>", options: .regularExpression)
            .replacingOccurrences(of: "[A-Za-z0-9+/]{40,}={0,2}", with: "<redacted>", options: .regularExpression)
        let line = "\(Date()) \(action) failed: \(redacted)\n"
        if let handle = try? FileHandle(forWritingTo: Self.logURL) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: Self.logURL)
        }
        finish(error: message)
    }

    private func start(_ text: String) { busy = true; message = text; messageIsError = false }
    private func finish(info: String) { busy = false; message = info; messageIsError = false }
    private func finish(error: String) { busy = false; message = error; messageIsError = true }
}
