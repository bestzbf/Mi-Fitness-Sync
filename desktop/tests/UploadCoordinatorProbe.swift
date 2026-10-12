import AppKit
import WebKit

final class FixtureWebView: WKWebView {
    var page: URL?
    var selectedOnPage = false
    var selects = 0
    var saves = 0
    override var url: URL? { page }
    override var isLoading: Bool { false }
    override func load(_ request: URLRequest) -> WKNavigation? {
        page = request.url
        selectedOnPage = false
        return nil  // No fixture request reaches the network.
    }
    override func evaluateJavaScript(_ javaScriptString: String, completionHandler: (@MainActor @Sendable (Any?, Error?) -> Void)? = nil) {
        let expression = javaScriptString.components(separatedBy: "; JSON.stringify(window.xiaomiRunUpload.").last ?? ""
        var value: [String: Any]
        if expression.hasPrefix("selectFile(") {
            selects += 1
            selectedOnPage = true
            value = ["status": "selected"]
        } else if expression.hasPrefix("save()") {
            saves += 1
            value = ["status": "saving", "activity_id": 12345]
        } else if !selectedOnPage {
            value = ["status": "ready_for_file"]
        } else if selects == 1 {
            value = ["status": "ready_to_save", "activity_id": 12345]
        } else {
            value = ["status": "duplicate", "activity_id": 67890]
        }
        let data = try! JSONSerialization.data(withJSONObject: value)
        let result = String(data: data, encoding: .utf8)!
        DispatchQueue.main.async { completionHandler?(result, nil) }
    }
}

func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { print("Upload coordinator test failed: \(message)"); exit(1) }
}

_ = NSApplication.shared
let configuration = WKWebViewConfiguration()
configuration.websiteDataStore = .nonPersistent()
let web = FixtureWebView(frame: .zero, configuration: configuration)
let upload = try StravaUpload(webView: web, scriptURL: URL(fileURLWithPath: CommandLine.arguments[1]))
let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: folder) }
let files = ["run_20261012_120000_0123456789abcdef.fit", "run_20261012_130000_fedcba9876543210.fit"].map { folder.appendingPathComponent($0) }
for file in files { try Data([1, 2, 3]).write(to: file) }
var verified: [String] = []
var commit: ((Bool) -> Void)?
var finished = false
var finishError: String?
upload.onVerified = { item, id, duplicate, completion in
    verified.append(item.id)
    if item.id == "first" {
        require(id == 12345 && !duplicate, "wrong first result")
        commit = completion
    } else {
        require(id == 67890 && duplicate, "wrong duplicate mapping")
        completion(false)  // A failed local confirmation must stop subsequent uploads.
    }
}
upload.onFinished = { error in finished = true; finishError = error }
upload.start([StravaUploadItem(id: "first", file: files[0]), StravaUploadItem(id: "second", file: files[1])])
let deadline = Date().addingTimeInterval(12)
var wrongURLTested = false
var correctURLApplied = false
var confirmedAt: Date?
while !finished && Date() < deadline {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
    if web.saves == 1 && !wrongURLTested {
        web.page = URL(string: "https://www.strava.com/activities/99999")
        upload.pageDidFinish(web.page)
        wrongURLTested = true
        confirmedAt = Date()
    }
    if wrongURLTested && !correctURLApplied && Date().timeIntervalSince(confirmedAt!) > 0.8 {
        require(verified.isEmpty, "unrelated activity navigation counted as successful")
        web.page = URL(string: "https://www.strava.com/activities/12345")
        upload.pageDidFinish(web.page)
        correctURLApplied = true
    }
    if commit != nil && confirmedAt == nil { confirmedAt = Date() }
    if let saved = commit, Date().timeIntervalSince(confirmedAt!) > 2 {
        require(web.selects == 1 && web.saves == 1 && verified == ["first"], "next file started before local confirmation")
        commit = nil
        saved(true)
    }
}
require(finished && finishError != nil && !upload.active, "local record failure did not stop the batch")
require(verified == ["first", "second"] && web.selects == 2 && web.saves == 1, "duplicate was saved again or source mapping was lost")
print("Upload coordinator checks passed: matching activity navigation, local confirmation before next file, duplicate skip, stop on record failure")
