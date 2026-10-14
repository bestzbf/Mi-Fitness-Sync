import AppKit
import WebKit

// This WebKit fixture never loads a URL or sends a request to Strava.
final class BackgroundWebView: WKWebView {
    enum Scenario { case normal, login, rejected, cancelled }
    let scenario: Scenario
    var page: URL?
    var authenticated = false
    var selected = false
    var loads = 0
    var selects = 0
    var saves = 0
    override var url: URL? { page }
    override var isLoading: Bool { false }
    init(_ scenario: Scenario) {
        self.scenario = scenario
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        super.init(frame: .zero, configuration: configuration)
    }
    required init?(coder: NSCoder) { fatalError("unused") }
    override func load(_ request: URLRequest) -> WKNavigation? {
        loads += 1
        selected = false
        page = URL(string: scenario == .login && !authenticated ? "https://www.strava.com/login" : "https://www.strava.com/upload/select")
        return nil
    }
    override func evaluateJavaScript(_ code: String, completionHandler: (@MainActor @Sendable (Any?, Error?) -> Void)? = nil) {
        guard code.contains("; JSON.stringify(window.xiaomiRunUpload.") else {
            completionHandler?(page?.path.hasPrefix("/upload") == true, nil)
            return
        }
        let expression = code.components(separatedBy: "; JSON.stringify(window.xiaomiRunUpload.").last ?? ""
        var result: [String: Any]
        if expression.hasPrefix("selectFile(") {
            selects += 1
            selected = true
            result = ["status": "selected"]
        } else if expression.hasPrefix("save()") {
            saves += 1
            result = ["status": "saving", "activity_id": 12345]
            page = URL(string: "https://www.strava.com/activities/12345")
            navigationDelegate?.webView?(self, didFinish: nil)
        } else if page?.path == "/login" {
            result = ["status": "login"]
        } else if page?.path == "/dashboard" {
            result = ["status": "other_page"]
        } else if !selected {
            result = ["status": "ready_for_file"]
        } else if scenario == .rejected {
            result = ["status": "error", "reason": "file-rejected", "detail": "Fixture: invalid data"]
        } else {
            result = ["status": "ready_to_save", "activity_id": 12345]
        }
        let data = try! JSONSerialization.data(withJSONObject: result)
        DispatchQueue.main.async { completionHandler?(String(data: data, encoding: .utf8)!, nil) }
    }
}

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { print("Background sync check failed: \(message)"); exit(1) }
}

_ = NSApplication.shared
let scriptURL = URL(fileURLWithPath: CommandLine.arguments[1])
let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: folder) }
let file = folder.appendingPathComponent("run_20261012_120000_0123456789abcdef.fit")
try Data([1, 2, 3]).write(to: file)

for scenario in [BackgroundWebView.Scenario.normal, .login, .rejected, .cancelled] {
    let web = BackgroundWebView(scenario)
    let profile = UUID()
    let session = StravaSession(profileID: profile, keychainService: "local.xiaomi.running-sync.background-test.\(profile)")
    let panel = StravaWebPanel(view: web, session: session, scriptURL: scriptURL, diagnosticsEnabled: false)
    var done = false
    var error: String?
    var verified = 0
    var loginShown = false
    panel.startUpload([StravaUploadItem(id: "fixture", file: file)], onProgress: { _ in }, onVerified: { _, id, duplicate, completion in
        require(id == 12345 && !duplicate, "incorrect verified activity")
        verified += 1
        completion(true)
    }, onFinished: { message in error = message; done = true })
    if scenario == .cancelled { panel.cancelUpload() }
    let deadline = Date().addingTimeInterval(12)
    while !done && Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        let visible = NSApplication.shared.windows.filter { $0.isVisible }
        if scenario == .login && !web.authenticated && !visible.isEmpty {
            require(web.selects == 0 && web.saves == 0, "file submitted before login")
            loginShown = true
            web.authenticated = true
            web.page = URL(string: "https://www.strava.com/dashboard")
        } else if scenario == .login && web.selects == 0 {
            // The login window can stay visible until the upload page is ready.
        } else {
            require(visible.isEmpty, "browser window appeared during background sync")
        }
    }
    require(done, "background sync timed out")
    if scenario == .cancelled {
        RunLoop.main.run(until: Date().addingTimeInterval(1))
        require(error != nil && verified == 0 && web.loads == 0 && web.selects == 0 && web.saves == 0, "cancelled startup resumed after cookie restore")
    } else if scenario == .rejected {
        require(error?.contains("Fixture: invalid data") == true && verified == 0 && web.saves == 0, "rejection was hidden or recorded as successful")
    } else {
        require(error == nil && verified == 1 && web.selects == 1 && web.saves == 1, "background save was not confirmed once")
    }
    if scenario == .login { require(loginShown && web.loads == 2, "dashboard login redirect did not resume the batch") }
    require(FileManager.default.fileExists(atPath: file.path), "unconfirmed fixture file was deleted")
    session.logout()
}
print("Background sync checks passed: no browser during upload, login-only window, Google dashboard resume, official save confirmation, errors stay in app, cancellation before session restore")
