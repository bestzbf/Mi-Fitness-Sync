import Foundation
import WebKit

struct StravaUploadItem {
    let id: String
    let file: URL
}

final class StravaUpload {
    private enum Phase {
        case waiting, selecting, processing, saving(Int), confirming
    }
    private let webView: WKWebView
    private let script: String
    private var items: [StravaUploadItem] = []
    private var index = 0
    private var phase = Phase.waiting
    private var timer: Timer?
    private var evaluating = false
    private var waitingForLogin = false
    private var deadline = Date()
    private var completedNavigation: URL?
    private var lastMessage = ""
    private(set) var active = false
    var onProgress: ((String) -> Void)?
    var onLoginNeeded: (() -> Void)?
    var onVerified: ((StravaUploadItem, Int, Bool, @escaping (Bool) -> Void) -> Void)?
    var onFinished: ((String?) -> Void)?

    init(webView: WKWebView, scriptURL: URL) throws {
        self.webView = webView
        self.script = try String(contentsOf: scriptURL, encoding: .utf8)
    }

    func start(_ items: [StravaUploadItem]) {
        guard !active else { return }
        guard !items.isEmpty && items.count <= 15 else {
            onFinished?("请选择 1 至 15 条跑步后开始同步。")
            return
        }
        self.items = items
        index = 0
        active = true
        timer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in self?.poll() }
        next()
    }

    func pageDidFinish(_ url: URL?) { completedNavigation = url }

    func cancel() {
        if active { finish("自动同步已停止；已确认的活动会跳过，未确认的文件已保留。") }
    }

    private func announce(_ message: String) {
        if message != lastMessage { lastMessage = message; onProgress?(message) }
    }

    private func next() {
        if index == items.count { finish(nil); return }
        phase = .waiting
        evaluating = false
        waitingForLogin = false
        completedNavigation = nil
        deadline = Date().addingTimeInterval(60)
        announce("正在同步第 \(index + 1) / \(items.count) 条跑步…")
        webView.load(URLRequest(url: URL(string: "https://www.strava.com/upload/select")!))
    }

    private func finish(_ error: String?) {
        active = false
        timer?.invalidate()
        timer = nil
        onFinished?(error)
    }

    private func trusted() -> Bool {
        guard let url = webView.url else { return false }
        return url.scheme == "https" && ["www.strava.com", "strava.com"].contains(url.host?.lowercased() ?? "")
    }

    private func poll() {
        guard active, !evaluating else { return }
        if case .confirming = phase { return }
        if case .saving(let id) = phase, trusted(), !webView.isLoading,
            completedNavigation?.path == "/activities/\(id)", webView.url?.path == "/activities/\(id)" {
            verify(id: id, duplicate: false)
            return
        }
        if !waitingForLogin && Date() > deadline {
            finish("Strava 暂未确认本次保存，已停止自动同步。文件和已完成记录已保留，请稍后重试。")
            return
        }
        guard trusted(), !webView.isLoading else { return }
        evaluate("inspect()") { [weak self] value in
            guard let self = self, self.active else { return }
            let status = value["status"] as? String ?? ""
            switch status {
            case "login":
                guard case .waiting = self.phase else {
                    self.finish("Strava 登录在上传期间失效，已停止自动同步。请登录后重试，已确认记录会自动跳过。")
                    return
                }
                if !self.waitingForLogin {
                    self.waitingForLogin = true
                    self.announce("请在 Strava 官方页面完成登录，随后会自动继续同步。")
                    self.onLoginNeeded?()
                }
            case "ready_for_file":
                guard case .waiting = self.phase else { return }
                self.waitingForLogin = false
                self.deadline = Date().addingTimeInterval(240)
                self.select()
            case "ready_to_save":
                guard case .processing = self.phase,
                    let id = value["activity_id"] as? Int, id > 0 else { return }
                self.phase = .saving(id)
                self.deadline = Date().addingTimeInterval(90)
                self.announce("第 \(self.index + 1) / \(self.items.count) 条已解析，正在保存到 Strava…")
                self.evaluate("save()") { result in
                    guard self.active else { return }
                    let status = result["status"] as? String
                    if status != "saving" && status != "already_saving" {
                        self.finish("Strava 保存按钮暂不可用，本次已停止，文件已保留。")
                    }
                }
            case "duplicate":
                guard case .processing = self.phase,
                    let id = value["activity_id"] as? Int, id > 0 else { return }
                self.announce("Strava 已有第 \(self.index + 1) 条活动，正在记录并跳过重复上传…")
                self.verify(id: id, duplicate: true)
            case "error":
                let reason = value["reason"] as? String ?? ""
                let message: String
                switch reason {
                case "rate-limited": message = "Strava 暂时限制上传，请稍后再试。"
                case "email-must-be-verified": message = "请先在 Strava 验证邮箱，再重新同步。"
                case "not-running": message = "Strava 未将本次文件识别为跑步，已停止保存，请检查活动。"
                case "previous-upload-unfinished", "unexpected-activities": message = "Strava 页面上还有其他待处理活动，已停止自动保存，请先处理这些活动。"
                case "saving-error": message = "Strava 保存失败，请检查网络后重试。"
                default: message = "Strava 未接受本次文件，已停止自动同步，请查看官方页面提示。"
                }
                self.finish(message + " 已完成记录和未确认文件已保留。")
            default: break
            }
        }
    }

    private func select() {
        let item = items[index]
        guard item.file.isFileURL, item.file.pathExtension.lowercased() == "fit" else {
            finish("待上传文件类型无效，已停止同步。")
            return
        }
        guard item.file.isFileURL, let data = try? Data(contentsOf: item.file),
            !data.isEmpty, data.count <= 25 * 1024 * 1024,
            let encoded = try? JSONSerialization.data(withJSONObject: ["name": item.file.lastPathComponent, "data": data.base64EncodedString()]),
            let payload = String(data: encoded, encoding: .utf8) else {
            finish("待上传文件无法读取或超过 25 MB，已停止同步。")
            return
        }
        phase = .selecting
        evaluate("selectFile(\(payload))") { [weak self] result in
            guard let self = self, self.active else { return }
            if result["status"] as? String == "selected" {
                self.phase = .processing
                self.announce("正在上传第 \(self.index + 1) / \(self.items.count) 条跑步…")
            } else {
                self.finish("Strava 页面暂不支持自动选择文件，本次已停止，文件已保留。")
            }
        }
    }

    private func evaluate(_ expression: String, completion: @escaping ([String: Any]) -> Void) {
        guard trusted() else { completion([:]); return }
        evaluating = true
        let code = script + "; JSON.stringify(window.xiaomiRunUpload.\(expression))"
        webView.evaluateJavaScript(code) { [weak self] result, error in
            guard let self = self else { return }
            self.evaluating = false
            if let value = result as? String, let data = value.data(using: .utf8),
                let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                completion(object)
            } else {
                // Navigation after save can discard the JavaScript callback; never submit twice.
                if case .saving = self.phase { completion(["status": "saving"]) }
                else if self.active { self.finish("无法读取 Strava 页面状态，已停止自动同步，文件已保留。") }
            }
        }
    }

    private func verify(id: Int, duplicate: Bool) {
        phase = .confirming
        guard let onVerified = onVerified else { finish("无法记录同步结果，已停止同步。"); return }
        onVerified(items[index], id, duplicate) { [weak self] saved in
            guard let self = self, self.active else { return }
            if saved { self.index += 1; self.next() }
            else { self.finish("Strava 已返回活动结果，但本机记录未保存成功。本次已停止，文件已保留。") }
        }
    }
}
