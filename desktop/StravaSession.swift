import Foundation
import Security
import WebKit

struct StravaCookie: Codable {
    let name: String
    let value: String
    let domain: String
    let path: String
    let expires: Date?
    let secure: Bool
    let httpOnly: Bool
    let sameSite: String?

    static func belongsToStrava(_ domain: String) -> Bool {
        let host = domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
        return host == "strava.com" || host.hasSuffix(".strava.com")
    }

    init?(_ cookie: HTTPCookie) {
        guard Self.belongsToStrava(cookie.domain), cookie.expiresDate.map({ $0 > Date() }) ?? true else { return nil }
        name = cookie.name
        value = cookie.value
        domain = cookie.domain
        path = cookie.path
        expires = cookie.expiresDate
        secure = cookie.isSecure
        httpOnly = cookie.isHTTPOnly
        sameSite = cookie.properties?[HTTPCookiePropertyKey(rawValue: "SameSite")] as? String
    }

    func restored() -> HTTPCookie? {
        guard Self.belongsToStrava(domain), expires.map({ $0 > Date() }) ?? true else { return nil }
        var properties: [HTTPCookiePropertyKey: Any] = [.name: name, .value: value, .domain: domain, .path: path]
        if secure { properties[.secure] = "TRUE" }
        if httpOnly { properties[HTTPCookiePropertyKey(rawValue: "HttpOnly")] = "TRUE" }
        if let expires = expires { properties[.expires] = expires }
        if let sameSite = sameSite { properties[HTTPCookiePropertyKey(rawValue: "SameSite")] = sameSite }
        return HTTPCookie(properties: properties)
    }
}

final class StravaSession: NSObject, WKHTTPCookieStoreObserver {
    // The profile is stable across application builds and releases.
    static let profileID = UUID(uuidString: "9E620884-0850-4E65-BFC6-031B0C566FF6")!
    static var snapshotService: String {
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "development"
        // A locally signed update can lose access to the previous binary's Keychain ACL.
        // The fixed WebKit profile migrates live cookies into this build's protected item.
        return "local.xiaomi.running-sync.strava-browser.\(build)"
    }
    let dataStore: WKWebsiteDataStore
    private let keychainService: String
    private let migrateLegacyCookies: Bool
    private var restoring = false
    private var ready = false
    private var waiting: [() -> Void] = []
    private var lastSnapshot: Data?
    private var disabled = false
    private(set) var snapshotSaved = false

    private var keychainQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: keychainService,
         kSecAttrAccount as String: "strava-web-session",
         // macOS legacy generic-password ACLs still need this to forbid a blocking dialog.
         kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail]
    }

    init(profileID: UUID = StravaSession.profileID, keychainService: String = StravaSession.snapshotService) {
        self.dataStore = WKWebsiteDataStore(forIdentifier: profileID)
        self.keychainService = keychainService
        self.migrateLegacyCookies = profileID == StravaSession.profileID
        super.init()
        dataStore.httpCookieStore.add(self)
    }
    deinit { dataStore.httpCookieStore.remove(self) }

    func restore(completion: @escaping () -> Void) {
        if ready { completion(); return }
        waiting.append(completion)
        if restoring { return }
        restoring = true
        var query = keychainQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var value: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &value)
            let saved = (status == errSecSuccess ? value as? Data : nil).flatMap { try? JSONDecoder().decode([StravaCookie].self, from: $0) } ?? []
            DispatchQueue.main.async { self?.restoreCookies(saved) }
        }
    }

    private func restoreCookies(_ saved: [StravaCookie]) {
        // Migrate existing default-profile cookies when upgrading, without exporting Google cookies.
        let mergeCookies: ([HTTPCookie]) -> Void = { [weak self] legacy in
            guard let self = self else { return }
            self.dataStore.httpCookieStore.getAllCookies { current in
                var cookies: [String: HTTPCookie] = [:]
                for cookie in legacy + saved.compactMap({ $0.restored() }) + current {
                    guard StravaCookie.belongsToStrava(cookie.domain), cookie.expiresDate.map({ $0 > Date() }) ?? true else { continue }
                    cookies["\(cookie.domain)|\(cookie.path)|\(cookie.name)"] = cookie
                }
                let group = DispatchGroup()
                for cookie in cookies.values {
                    group.enter()
                    self.dataStore.httpCookieStore.setCookie(cookie) { group.leave() }
                }
                group.notify(queue: .main) {
                    self.restoring = false
                    self.ready = true
                    let completions = self.waiting
                    self.waiting = []
                    self.save { completions.forEach { $0() } }
                }
            }
        }
        if migrateLegacyCookies { WKWebsiteDataStore.default().httpCookieStore.getAllCookies(mergeCookies) }
        else { mergeCookies([]) }
    }

    func cookiesDidChange(in cookieStore: WKHTTPCookieStore) {
        if !restoring && ready { save() }
    }

    // Local installer handoff: transfer only this app's encrypted Strava snapshot
    // through stdin, then protect it under the updated application's Keychain item.
    func importSnapshot(_ data: Data, completion: @escaping (Bool) -> Void) {
        guard ready, data.count <= 1024 * 1024,
              let records = try? JSONDecoder().decode([StravaCookie].self, from: data),
              !records.isEmpty, records.count <= 128,
              records.allSatisfy({ StravaCookie.belongsToStrava($0.domain) }) else { completion(false); return }
        let cookies = records.compactMap { $0.restored() }
        guard !cookies.isEmpty else { completion(false); return }
        snapshotSaved = false
        let group = DispatchGroup()
        for cookie in cookies {
            group.enter()
            dataStore.httpCookieStore.setCookie(cookie) { group.leave() }
        }
        group.notify(queue: .main) { self.save { completion(self.snapshotSaved) } }
    }

    func save(completion: (() -> Void)? = nil) {
        guard !disabled else { completion?(); return }
        dataStore.httpCookieStore.getAllCookies { [weak self] cookies in
            guard let self = self else { completion?(); return }
            guard !self.disabled else { completion?(); return }
            let records = cookies.compactMap(StravaCookie.init).sorted {
                "\($0.domain)|\($0.path)|\($0.name)" < "\($1.domain)|\($1.path)|\($1.name)"
            }
            guard !records.isEmpty, let encoded = try? JSONEncoder().encode(records) else { completion?(); return }
            if encoded != self.lastSnapshot {
                let changes = [kSecValueData as String: encoded]
                var status = SecItemUpdate(self.keychainQuery as CFDictionary, changes as CFDictionary)
                if status == errSecItemNotFound {
                    var item = self.keychainQuery
                    item[kSecValueData as String] = encoded
                    item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
                    status = SecItemAdd(item as CFDictionary, nil)
                }
                if status == errSecSuccess { self.lastSnapshot = encoded; self.snapshotSaved = true }
                else { self.snapshotSaved = false }
            } else { self.snapshotSaved = true }
            completion?()
        }
    }

    func logout() {
        disabled = true
        lastSnapshot = nil
        snapshotSaved = false
        SecItemDelete(keychainQuery as CFDictionary)
    }
    func resumeAfterLogin() { disabled = false; save() }
}
