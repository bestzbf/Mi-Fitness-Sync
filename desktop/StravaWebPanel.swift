import AppKit
import UniformTypeIdentifiers
import WebKit

final class StravaWebPanel: NSObject, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private let caption = NSTextField(labelWithString: "")
    private let confirmation = NSButton(title: "已保存，标记本次已上传", target: nil, action: nil)
    private let stopAutomatic = NSButton(title: "停止同步", target: nil, action: nil)
    private let reload = NSButton(title: "刷新页面", target: nil, action: nil)
    private var preparedFiles: [URL] = []
    private var onConfirm: (() -> Void)?
    private let browserSession: StravaSession
    private var uploadReady = false
    private var popupWindows: [NSWindow] = []
    private let browserInstance = UUID().uuidString
    private var uploader: StravaUpload?
    private var revealedForLogin = false
    private let diagnosticsEnabled: Bool
    private var pendingStart: UUID?

    init(view: WKWebView? = nil, session: StravaSession = StravaSession(), scriptURL: URL? = nil,
         diagnosticsEnabled: Bool = true) {
        browserSession = session
        self.diagnosticsEnabled = diagnosticsEnabled
        super.init()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = browserSession.dataStore
        webView = view ?? WKWebView(frame: .zero, configuration: configuration)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 800), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Strava · 小米跑步同步"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 860, height: 650)
        let root = NSView()
        window.contentView = root
        let bar = NSStackView()
        bar.orientation = .horizontal
        bar.spacing = 14
        bar.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 14, right: 16)
        caption.font = NSFont.systemFont(ofSize: 12)
        caption.lineBreakMode = .byWordWrapping
        caption.maximumNumberOfLines = 2
        caption.setContentHuggingPriority(.defaultLow, for: .horizontal)
        caption.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        confirmation.target = self
        confirmation.action = #selector(confirm)
        confirmation.bezelStyle = .rounded
        reload.target = self
        reload.action = #selector(reloadPage)
        reload.bezelStyle = .rounded
        stopAutomatic.target = self
        stopAutomatic.action = #selector(stopUpload)
        stopAutomatic.bezelStyle = .rounded
        stopAutomatic.isHidden = true
        bar.addArrangedSubview(caption)
        bar.addArrangedSubview(reload)
        bar.addArrangedSubview(stopAutomatic)
        bar.addArrangedSubview(confirmation)
        for view in [bar, webView!] {
            root.addSubview(view)
            view.translatesAutoresizingMaskIntoConstraints = false
        }
        NSLayoutConstraint.activate([
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor), bar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            bar.topAnchor.constraint(equalTo: root.topAnchor), bar.heightAnchor.constraint(equalToConstant: 70),
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor), webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: bar.bottomAnchor), webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        root.layoutSubtreeIfNeeded()
        if let file = scriptURL ?? Bundle.main.resourceURL?.appendingPathComponent("strava-upload.js") {
            uploader = try? StravaUpload(webView: webView, scriptURL: file)
        }
        window.center()
    }
    func show(files: [URL], onConfirm: (() -> Void)?) {
        if uploader?.active == true {
            window.makeKeyAndOrderFront(nil)
            return
        }
        preparedFiles = files
        self.onConfirm = onConfirm
        confirmation.isHidden = false
        stopAutomatic.isHidden = true
        confirmation.isEnabled = !files.isEmpty
        caption.stringValue = files.isEmpty ? "Strava 官方页面。登录后可查看你的活动；登录状态保存在本机。" : "本次 \(files.count) 条跑步。点击网页“选择文件”会自动带入勾选的 FIT；保存成功后点击右侧标记完成。"
        browserSession.restore { [weak self] in
            guard let self = self else { return }
            if !self.uploadReady && self.webView.url?.path.hasPrefix("/upload") != true { self.loadUploadPage() }
            self.recordSessionStatus()
        }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func startUpload(_ items: [StravaUploadItem], onProgress: @escaping (String) -> Void,
                     onVerified: @escaping (StravaUploadItem, Int, Bool, @escaping (Bool) -> Void) -> Void,
                     onFinished: @escaping (String?) -> Void) {
        guard let uploader = uploader, !uploader.active, pendingStart == nil else {
            onFinished("自动上传组件无法启动，请重新打开软件。")
            return
        }
        preparedFiles = items.map(\.file)
        onConfirm = nil
        confirmation.isHidden = true
        stopAutomatic.isHidden = false
        stopAutomatic.isEnabled = true
        reload.isEnabled = false
        uploader.onProgress = { [weak self] message in self?.caption.stringValue = message; onProgress(message) }
        uploader.onLoginNeeded = { [weak self] in
            self?.revealedForLogin = true
            self?.window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
        uploader.onLoginReady = { [weak self] in
            if self?.revealedForLogin == true { self?.window.orderOut(nil); self?.revealedForLogin = false }
        }
        uploader.onDiagnostic = { [weak self] value in self?.recordUploadDiagnostic(value) }
        uploader.onVerified = onVerified
        uploader.onFinished = { [weak self] error in
            self?.stopAutomatic.isEnabled = false
            self?.reload.isEnabled = true
            self?.caption.stringValue = error ?? "所选跑步已处理，Strava 返回的结果已自动记录。"
            self?.revealedForLogin = false
            if error == nil { self?.preparedFiles = [] }
            self?.window.orderOut(nil)
            onFinished(error)
        }
        caption.stringValue = "正在连接 Strava，准备自动同步…"
        // Keep a sized WebKit view attached for the official page's layout, without
        // ordering the browser window onto the screen during normal synchronization.
        revealedForLogin = false
        window.orderOut(nil)
        recordUploadDiagnostic(["status": "starting", "count": items.count])
        let token = UUID()
        pendingStart = token
        browserSession.restore { [weak self] in
            guard let self = self, self.pendingStart == token else { return }
            self.pendingStart = nil
            uploader.start(items)
        }
    }
    private func recordUploadDiagnostic(_ value: [String: Any]) {
        guard diagnosticsEnabled else { return }
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/XiaomiRunSync")
        var report = value
        report["version"] = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "development"
        report["time"] = ISO8601DateFormatter().string(from: Date())
        if let data = try? JSONSerialization.data(withJSONObject: report) {
            let file = folder.appendingPathComponent("strava-upload-status.json")
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
    }
    func cancelUpload() {
        if pendingStart != nil {
            pendingStart = nil
            uploader?.onFinished?("自动同步已停止，未确认的文件已保留。")
        } else { uploader?.cancel() }
    }
    @objc private func stopUpload() { cancelUpload() }
    @objc private func confirm() { onConfirm?() }
    func finishBatch() {
        preparedFiles = []
        onConfirm = nil
        confirmation.isEnabled = false
        caption.stringValue = "已按你的确认标记本次跑步为已上传，文件已保留到“已上传”。"
        uploadReady = false
        loadUploadPage()
    }
    @objc private func reloadPage() { webView.reload() }
    private func loadUploadPage() {
        webView.load(URLRequest(url: URL(string: "https://www.strava.com/upload/select")!))
    }
    private func recordSessionStatus() {
        guard diagnosticsEnabled else { return }
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/XiaomiRunSync")
        let value: [String: Any] = [
            "logged_in": uploadReady, "session_saved": browserSession.snapshotSaved,
            "profile_id": StravaSession.profileID.uuidString, "browser_instance": browserInstance,
            "popup_count": popupWindows.count,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            let file = folder.appendingPathComponent("strava-browser-status.json")
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try? data.write(to: file, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
    }
    func saveSession(completion: @escaping () -> Void) { browserSession.save(completion: completion) }
    private func inspectPageIfRequested() {
        guard CommandLine.arguments.contains("--inspect-strava") else { return }
        let script = #"""
        (() => {
          const clean = e => ({tag:e.tagName,id:e.id,classes:e.className,type:e.type||'',name:e.name||''});
          return JSON.stringify({path:location.pathname,automatic_upload_readiness:window.xiaomiRunUpload ? window.xiaomiRunUpload.inspect().status : 'missing-component',
            forms:Array.from(document.forms).map(f=>({...clean(f),action:new URL(f.action).pathname,inputs:Array.from(f.elements).filter(e=>e.tagName!=='BUTTON').map(clean)})),
            controls:Array.from(document.querySelectorAll('button,input[type=submit],a.btn')).map(e=>({...clean(e),label:(e.innerText||e.value||'').trim().slice(0,100)})),
            scripts:Array.from(document.scripts).filter(e=>e.src).map(e=>({host:new URL(e.src).hostname,path:new URL(e.src).pathname})),
            containers:Array.from(document.querySelectorAll('[id]')).filter(e=>/upload|activity|file/.test(e.id)).map(clean)
          });
        })()
        """#
        let uploadScript = Bundle.main.resourceURL?.appendingPathComponent("strava-upload.js")
        let source = uploadScript.flatMap { try? String(contentsOf: $0, encoding: .utf8) } ?? ""
        let inspect = source + ";" + script
        webView.evaluateJavaScript(inspect) { value, _ in
            guard let value = value as? String, let data = value.data(using: .utf8) else { return }
            let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/XiaomiRunSync/strava-page-inspection.json")
            try? data.write(to: file, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        if uploader?.active == true { completionHandler(nil); return }
        let url = frame.request.url ?? (frame.isMainFrame ? webView.url : nil)
        let host = url?.host?.lowercased() ?? ""
        let trusted = (host == "strava.com" || host == "www.strava.com") && url?.scheme == "https" && url?.path.hasPrefix("/upload") == true
        if trusted && !preparedFiles.isEmpty && preparedFiles.allSatisfy({ FileManager.default.fileExists(atPath: $0.path) }) {
            completionHandler(parameters.allowsMultipleSelection ? preparedFiles : [preparedFiles[0]])
            return
        }
        guard trusted else { completionHandler(nil); return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [UTType(filenameExtension: "fit") ?? .data]
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil else { return nil }
        // Google sign-in needs a real popup with window.opener and the shared profile.
        // Replacing the main page breaks the callback that completes Strava login.
        let popupView = WKWebView(frame: .zero, configuration: configuration)
        popupView.uiDelegate = self
        popupView.navigationDelegate = self
        let popup = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 760), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        popup.title = "Strava 官方登录"
        popup.isReleasedWhenClosed = false
        popup.contentView = popupView
        popupWindows.append(popup)
        window.addChildWindow(popup, ordered: .above)
        popup.center()
        popup.makeKeyAndOrderFront(nil)
        return popupView
    }
    func webViewDidClose(_ webView: WKWebView) {
        if let popup = popupWindows.first(where: { $0.contentView === webView }) {
            window.removeChildWindow(popup)
            popup.close()
            popupWindows.removeAll { $0 === popup }
            browserSession.save()
        }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let url = webView.url
        if url?.host.map({ StravaCookie.belongsToStrava($0) }) == true {
            if url?.path.contains("logout") == true { browserSession.logout() }
            if webView === self.webView {
                uploader?.pageDidFinish(url)
                inspectPageIfRequested()
                webView.evaluateJavaScript("!!document.querySelector('input[type=\"file\"]')") { [weak self] result, _ in
                    self?.uploadReady = result as? Bool == true && url?.path.hasPrefix("/upload") == true
                    if self?.uploadReady == true {
                        self?.browserSession.resumeAfterLogin()
                        self?.browserSession.save { self?.recordSessionStatus() }
                    } else { self?.recordSessionStatus() }
                }
            }
            browserSession.save()
        }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled {
            caption.stringValue = "Strava 页面暂时无法打开，请检查网络并刷新。跑步文件已保留。"
        }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        window.makeKeyAndOrderFront(nil)
        let alert = NSAlert()
        alert.messageText = "Strava 页面提示"
        alert.informativeText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        window.makeKeyAndOrderFront(nil)
        let alert = NSAlert()
        alert.messageText = "Strava 页面确认"
        alert.informativeText = message
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
}
