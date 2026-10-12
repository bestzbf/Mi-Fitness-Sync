import AppKit
import Foundation
import Security
import WebKit

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fputs("Session test failed: \(message)\n", stderr); exit(1) }
}

let phase = CommandLine.arguments[1]
let identifier = UUID(uuidString: CommandLine.arguments[2])!
let service = "local.xiaomi.running-sync.session-test.\(identifier.uuidString)"
_ = NSApplication.shared
let session = StravaSession(profileID: identifier, keychainService: service)
var finished = false

let sample = HTTPCookie(properties: [.name: "_xiaomi_session_test", .value: "synthetic-only", .domain: ".strava.com", .path: "/", .secure: "TRUE", HTTPCookiePropertyKey(rawValue: "HttpOnly"): "TRUE"])!
let stored = StravaCookie(sample)!
require(stored.restored()?.value == sample.value, "cookie value was not preserved")
require(stored.restored()?.isSessionOnly == true, "session-only cookie gained an artificial expiry")
require(stored.restored()?.isHTTPOnly == sample.isHTTPOnly, "HttpOnly flag changed")
require(StravaCookie(HTTPCookie(properties: [.name: "x", .value: "x", .domain: ".google.com", .path: "/"])!) == nil, "Google cookies must not be exported")
require(!StravaCookie.belongsToStrava("strava.com.untrusted.example"), "domain validation failed")

session.restore {
    if phase == "write" {
        session.dataStore.httpCookieStore.setCookie(sample) {
            session.save {
                let query: [String: Any] = [
                    kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: service,
                    kSecAttrAccount as String: "strava-web-session",
                    kSecReturnData as String: true,
                ]
                var stored: CFTypeRef?
                require(SecItemCopyMatching(query as CFDictionary, &stored) == errSecSuccess, "Keychain did not save the cookie snapshot")
                print("session-only cookie saved in Keychain")
                finished = true
            }
        }
    } else {
        session.dataStore.httpCookieStore.getAllCookies { cookies in
            let query: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: "strava-web-session",
                kSecReturnData as String: true,
            ]
            var stored: CFTypeRef?
            require(SecItemCopyMatching(query as CFDictionary, &stored) == errSecSuccess, "Keychain snapshot was not available after process restart")
            require(cookies.contains { $0.name == sample.name && $0.value == sample.value }, "session cookie did not survive process restart")
            session.logout()
            print("session-only cookie restored after process restart; test Keychain item removed")
            finished = true
        }
    }
}

let deadline = Date().addingTimeInterval(15)
while !finished && Date() < deadline {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}
require(finished, "WebKit cookie operation timed out")
