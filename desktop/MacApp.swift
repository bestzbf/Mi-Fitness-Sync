import AppKit
import SwiftUI
import UniformTypeIdentifiers
import WebKit

private let teal = Color(red: 0.08, green: 0.38, blue: 0.49)
private let orange = Color(red: 1.0, green: 0.59, blue: 0.28)
private let ink = Color(red: 0.10, green: 0.16, blue: 0.21)

struct ActivityRow: Identifiable {
    let id: String
    let date: String
    let sport: String
    let distance: String
    let duration: String
    let state: String
    let selectable: Bool
    let running: Bool
    let ride: Bool
    let file: String?

    init(_ value: [String: Any]) {
        id = value["id"] as? String ?? ""
        date = value["date"] as? String ?? "—"
        sport = value["sport"] as? String ?? "—"
        distance = value["distance"] as? String ?? "—"
        duration = value["duration"] as? String ?? "—"
        state = value["state"] as? String ?? "—"
        selectable = value["selectable"] as? Bool ?? false
        running = value["running"] as? Bool ?? false
        ride = value["ride"] as? Bool ?? false
        file = value["file"] as? String
    }
}

final class SyncModel: ObservableObject {
    @Published var activities: [ActivityRow] = []
    @Published var selected: Set<String> = []
    @Published var days = 3
    @Published var loadedDays = 3
    @Published var hideRides = true
    @Published var onlyRuns = false
    @Published var busy = false
    @Published var loggedIn = false
    @Published var apiConnected = false
    @Published var message = "正在读取本机设置…"
    @Published var isError = false
    @Published var lastCheck = "尚未刷新"
    @Published var since = ""
    @Published var range = ""
    @Published var duplicateText = "查重：本地记录"
    @Published var loginVisible = false
    @Published var qrImage: NSImage?
    var outputDirectory = ""
    private var process: Process?
    private var receivedResult = false
    private var receivedError = false
    private var activeCommand = ""
    private var stravaPanel: StravaWebPanel?
    private var webSyncing = false
    private var webArchiveCompletion: ((Bool) -> Void)?

    var visible: [ActivityRow] {
        days == loadedDays ? activities.filter { (!hideRides || !$0.ride) && (!onlyRuns || $0.running) } : []
    }
    var chosen: [ActivityRow] { visible.filter { selected.contains($0.id) && $0.selectable } }
    var archiveAvailable: Bool { !chosen.isEmpty && chosen.allSatisfy { $0.file != nil } }
    var dayRange: String {
        let calendar = Calendar.current
        let end = Date()
        let start = calendar.date(byAdding: .day, value: -(days - 1), to: end) ?? end
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        return "\(formatter.string(from: start)) → \(formatter.string(from: end))"
    }

    func retainVisibleSelection() { selected.formIntersection(Set(visible.filter(\.selectable).map(\.id))) }
    func toggle(_ row: ActivityRow) {
        guard row.selectable && !busy else { return }
        if selected.contains(row.id) { selected.remove(row.id) } else { selected.insert(row.id) }
    }
    func selectAll() {
        let allowed = Set(visible.filter(\.selectable).map(\.id))
        selected = selected == allowed ? [] : allowed
    }
    func refresh() { run("refresh", arguments: ["--days", String(days)]) }
    func sync() {
        guard !chosen.isEmpty else { return }
        if !apiConnected && chosen.count > 15 {
            isError = true
            message = "免费网页每批最多 15 个文件，请减少选择后继续。"
            return
        }
        run("sync", arguments: ["--ids"] + chosen.map(\.id))
    }
    func login() {
        qrImage = nil
        loginVisible = true
        run("login")
    }
    func cancelLogin() {
        process?.terminate()
        loginVisible = false
        qrImage = nil
    }
    func stop() { process?.terminate(); stravaPanel?.cancelUpload() }
    func saveBrowserSession(completion: @escaping () -> Void) {
        if let panel = stravaPanel { panel.saveSession(completion: completion) } else { completion() }
    }
    func openFolder() {
        guard !outputDirectory.isEmpty else { return }
        do {
            try FileManager.default.createDirectory(atPath: outputDirectory, withIntermediateDirectories: true)
            NSWorkspace.shared.open(URL(fileURLWithPath: outputDirectory))
        } catch {
            message = "无法打开待上传文件夹，请检查文件权限。"
            isError = true
        }
    }
    func openStrava() { showStrava() }
    private func showStrava(batchDirectory: String? = nil, ids: [String] = [], entries: [[String: Any]] = []) {
        if stravaPanel == nil { stravaPanel = StravaWebPanel() }
        if let directory = batchDirectory {
            let items = entries.compactMap { value -> StravaUploadItem? in
                guard let id = value["id"] as? String, let name = value["file"] as? String,
                    ids.contains(id), name == URL(fileURLWithPath: name).lastPathComponent,
                    name.hasSuffix(".fit") else { return nil }
                return StravaUploadItem(id: id, file: URL(fileURLWithPath: directory).appendingPathComponent(name))
            }
            guard items.count == ids.count, Set(items.map(\.id)) == Set(ids) else {
                message = "同步批次文件不完整，已停止自动上传。"
                isError = true
                return
            }
            webSyncing = true
            busy = true
            stravaPanel?.startUpload(items, onProgress: { [weak self] message in self?.message = message }, onVerified: { [weak self] item, stravaID, duplicate, completion in
                self?.recordWebSuccess(item, stravaID: stravaID, duplicate: duplicate, batchDirectory: directory, completion: completion)
            }, onFinished: { [weak self] error in
                guard let self = self else { return }
                self.webSyncing = false
                self.busy = self.process != nil
                self.isError = error != nil
                self.message = error ?? "同步完成：所选跑步已保存到 Strava，重复活动已跳过。"
            })
            return
        }
        let files = batchDirectory.flatMap {
            try? FileManager.default.contentsOfDirectory(at: URL(fileURLWithPath: $0), includingPropertiesForKeys: nil)
        }?.filter { $0.pathExtension.lowercased() == "fit" }.sorted { $0.lastPathComponent < $1.lastPathComponent } ?? []
        stravaPanel?.show(files: files, onConfirm: ids.isEmpty ? nil : { [weak self] in self?.confirmArchive(ids: ids) })
    }
    private func recordWebSuccess(_ item: StravaUploadItem, stravaID: Int, duplicate: Bool, batchDirectory: String, completion: @escaping (Bool) -> Void) {
        let folder = URL(fileURLWithPath: batchDirectory)
        let receipt = folder.appendingPathComponent("strava-confirmed.json")
        var records: [[String: Any]] = []
        if let old = try? Data(contentsOf: receipt), let value = try? JSONSerialization.jsonObject(with: old) as? [String: Any] {
            records = value["records"] as? [[String: Any]] ?? []
        }
        records.removeAll { $0["id"] as? String == item.id }
        records.append(["id": item.id, "file": item.file.lastPathComponent, "strava_id": stravaID, "result": duplicate ? "duplicate" : "uploaded"])
        do {
            let data = try JSONSerialization.data(withJSONObject: ["version": 1, "batch": folder.lastPathComponent, "records": records])
            try data.write(to: receipt, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: receipt.path)
            let handle = try FileHandle(forWritingTo: receipt)
            try handle.synchronize()
            try handle.close()
        } catch { completion(false); return }
        guard process == nil else { completion(false); return }
        webArchiveCompletion = completion
        run("complete-web", arguments: ["--batch-dir", batchDirectory, "--ids", item.id])
    }
    func confirmArchive(ids: [String]? = nil) {
        let selectedIDs = ids ?? chosen.map(\.id)
        guard !selectedIDs.isEmpty && !busy else { return }
        let alert = NSAlert()
        alert.messageText = "选中的跑步已在 Strava 保存成功吗？"
        alert.informativeText = "将标记本次选择的 \(selectedIDs.count) 条跑步为已上传，并保留文件到“已上传”文件夹。以后会自动跳过这些跑步。请确认每条活动已保存成功；只完成部分时，请取消并在活动列表勾选已成功的活动。"
        alert.addButton(withTitle: "已保存，标记已上传")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            run("archive", arguments: ["--ids"] + selectedIDs)
        }
    }
    func importRecords() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择 Strava 导出的 activities.csv"
        panel.allowedContentTypes = [.commaSeparatedText, .text]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let alert = NSAlert()
        alert.messageText = "导入 Strava 活动，检查重复跑步"
        alert.informativeText = "仅导入跑步类型，骑行不会参与匹配。按开始时间 ±6 秒对比。CSV 时间含时区时会使用原时区；不含时区时使用下面的选择。导入记录只覆盖此次文件的内容，请使用最新数据。"
        let timezone = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 330, height: 28))
        timezone.addItems(withTitles: ["UTC（Strava 导出的 CSV）", "北京时间 UTC+8（自己整理的 CSV）"])
        alert.accessoryView = timezone
        alert.addButton(withTitle: "导入并查重")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            run("import-strava", arguments: ["--csv", url.path, "--csv-timezone", timezone.indexOfSelectedItem == 0 ? "0" : "8"])
        }
    }

    func run(_ command: String, arguments: [String] = []) {
        guard !busy || (command == "complete-web" && webSyncing && process == nil) else { return }
        guard let resources = Bundle.main.resourceURL else { return }
        let executable = resources.appendingPathComponent("backend/xiaomi-sync-backend")
        let task = Process()
        let pipe = Pipe()
        task.executableURL = executable
        task.arguments = [command] + arguments
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        task.standardInput = FileHandle.nullDevice
        task.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        process = task
        busy = true
        isError = false
        receivedResult = false
        receivedError = false
        activeCommand = command
        if command != "status" { message = "正在处理…" }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                try task.run()
                var buffer = Data()
                while true {
                    let chunk = pipe.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                    while let end = buffer.firstIndex(of: 10) {
                        let line = Data(buffer[..<end])
                        buffer.removeSubrange(...end)
                        guard let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                        DispatchQueue.main.async { self?.receive(value) }
                    }
                }
                task.waitUntilExit()
                DispatchQueue.main.async { self?.finished(exitCode: task.terminationStatus) }
            } catch {
                DispatchQueue.main.async {
                    self?.message = "无法启动同步服务，请重新打开应用。"
                    self?.isError = true
                    self?.finished(exitCode: 1)
                }
            }
        }
    }
    private func receive(_ event: [String: Any]) {
        switch event["event"] as? String {
        case "progress": message = event["message"] as? String ?? "正在处理…"
        case "qr":
            if let encoded = event["image"] as? String, let data = Data(base64Encoded: encoded) {
                qrImage = NSImage(data: data)
                message = "请用小米手机扫码，并在手机确认登录。"
            }
        case "error":
            receivedError = true
            isError = true
            message = event["message"] as? String ?? "操作未完成，请重试。"
            if event["login_needed"] as? Bool == true { loggedIn = false }
        case "result":
            receivedResult = true
            guard let value = event["data"] as? [String: Any] else { return }
            activities = (value["activities"] as? [[String: Any]] ?? []).map(ActivityRow.init)
            loadedDays = value["days"] as? Int ?? days
            loggedIn = value["logged_in"] as? Bool ?? false
            apiConnected = value["api_connected"] as? Bool ?? false
            outputDirectory = value["output_dir"] as? String ?? ""
            since = value["since_local"] as? String ?? ""
            range = value["range"] as? String ?? ""
            lastCheck = value["last_check"] as? String ?? "尚未刷新"
            duplicateText = value["duplicate_text"] as? String ?? ""
            retainVisibleSelection()
            if let notice = value["message"] as? String {
                message = notice
            } else if activeCommand == "refresh" {
                let runs = activities.filter(\.running).count
                message = "刷新完成：\(activities.count) 条活动，\(runs) 条跑步。"
            } else if activeCommand == "archive" {
                message = "已记录上传完成，下次将跳过这些跑步。"
                stravaPanel?.finishBatch()
            } else if activeCommand == "import-strava" {
                message = "Strava 活动已导入，已匹配的重复跑步不可再次选择。"
            } else if activeCommand == "login" {
                message = "小米扫码登录已完成，点击“刷新活动”读取云端记录。"
                loginVisible = false
                qrImage = nil
            } else {
                message = loggedIn ? "小米已连接，点击“刷新活动”查看记录。" : "请先扫码登录小米账号。"
                if let cachedDays = value["days"] as? Int { days = cachedDays }
            }
            if let folder = value["batch_dir"] as? String {
                showStrava(batchDirectory: folder, ids: value["batch_ids"] as? [String] ?? [], entries: value["batch_items"] as? [[String: Any]] ?? [])
            }
        default: break
        }
    }
    private func finished(exitCode: Int32) {
        busy = webSyncing
        process = nil
        if activeCommand == "complete-web", let completion = webArchiveCompletion {
            webArchiveCompletion = nil
            completion(exitCode == 0 && receivedResult && !receivedError)
        }
        if !receivedResult && !receivedError && exitCode != 0 {
            isError = true
            message = loginVisible ? "登录已结束，可重新获取二维码。" : "本次操作未完成，登录和文件已保留。请重试。"
        }
    }
}

struct FilledButton: ButtonStyle {
    @Environment(\.isEnabled) var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 15, weight: .semibold))
            .foregroundStyle(enabled ? ink : .secondary)
            .padding(.horizontal, 22).padding(.vertical, 12)
            .background(enabled ? orange.opacity(configuration.isPressed ? 0.75 : 1) : Color.black.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 13))
    }
}

struct MainView: View {
    @ObservedObject var model: SyncModel
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 14) {
                Image(systemName: "figure.run").font(.system(size: 27, weight: .semibold))
                    .foregroundStyle(ink).frame(width: 54, height: 54).background(orange)
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                VStack(alignment: .leading, spacing: 4) {
                    Text("活动").font(.system(size: 30, weight: .bold))
                    Text("小米跑步同步  ·  国行 Watch S4  ·  中国区").font(.system(size: 13)).opacity(0.75)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 8) {
                    Label(model.loggedIn ? "小米已连接" : "小米未连接", systemImage: model.loggedIn ? "checkmark.circle.fill" : "person.crop.circle.badge.questionmark")
                        .foregroundStyle(model.loggedIn ? Color.green.opacity(0.85) : .white).font(.system(size: 13, weight: .medium))
                    Text(model.apiConnected ? "Strava API 自动上传" : "免费 · Strava 自动同步").font(.system(size: 12)).opacity(0.75)
                }
                Menu {
                    Button(model.loggedIn ? "重新扫码登录小米" : "扫码登录小米") { model.login() }
                    Button("导入 Strava 查重记录…") { model.importRecords() }
                    Button("手动上传后标记选中项已完成…") { model.confirmArchive() }
                        .disabled(!model.archiveAvailable || model.apiConnected)
                    Button("如何下载 Strava 活动记录") {
                        NSWorkspace.shared.open(URL(string: "https://support.strava.com/en-us/articles/15401919-how-do-i-export-my-strava-data")!)
                    }
                } label: { Image(systemName: "gearshape").font(.system(size: 20)) }
                .menuStyle(.borderlessButton).frame(width: 32).disabled(model.busy)
            }.foregroundStyle(.white)

            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 24) {
                    Picker("时间范围", selection: $model.days) {
                        Text("今天").tag(1)
                        Text("近三天").tag(3)
                        Text("近一周").tag(7)
                        Text("近一月").tag(30)
                    }.pickerStyle(.segmented).labelsHidden().frame(width: 385).disabled(model.busy)
                    Spacer()
                    Text(model.dayRange).font(.system(size: 14, weight: .medium)).monospacedDigit()
                }
                HStack(spacing: 24) {
                    Toggle("隐藏骑行", isOn: $model.hideRides).toggleStyle(.checkbox)
                    Toggle("仅显示跑步", isOn: $model.onlyRuns).toggleStyle(.checkbox)
                    Spacer()
                    if !model.loggedIn {
                        Button("扫码登录小米") { model.login() }.buttonStyle(FilledButton()).disabled(model.busy)
                    }
                    Button { model.refresh() } label: { Label("刷新活动", systemImage: "arrow.clockwise") }
                        .buttonStyle(FilledButton()).disabled(model.busy || !model.loggedIn)
                }
                Text("仅允许同步 \(model.since.isEmpty ? "配置完成后" : model.since + " 之后") 的跑步；骑行可查看，不能同步。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }.padding(22).card()

            VStack(spacing: 0) {
                HStack {
                    Text("\(model.visible.count) 个活动  ·  已选 \(model.chosen.count)").font(.system(size: 15, weight: .semibold))
                    if model.days == model.loadedDays && model.hideRides && model.activities.contains(where: \.ride) {
                        Text("已隐藏 \(model.activities.filter(\.ride).count) 条骑行").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("全选可同步跑步") { model.selectAll() }.buttonStyle(.plain).foregroundStyle(teal)
                        .disabled(model.busy || !model.visible.contains(where: \.selectable))
                }.padding(.horizontal, 22).padding(.vertical, 18)
                Divider()
                HStack(spacing: 14) {
                    Text("").frame(width: 28)
                    Text("开始时间").frame(width: 155, alignment: .leading)
                    Text("运动类型").frame(width: 100, alignment: .leading)
                    Text("距离").frame(width: 90, alignment: .trailing)
                    Text("时长").frame(width: 85, alignment: .trailing)
                    Spacer()
                    Text("同步状态").frame(width: 150, alignment: .leading)
                }.font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.vertical, 12)
                Divider()
                if model.visible.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "figure.run.circle").font(.system(size: 40)).foregroundStyle(teal.opacity(0.65))
                        Text(model.days != model.loadedDays ? "点击刷新读取所选时间范围" : model.activities.isEmpty ? "所选范围暂无活动" : "筛选后没有活动")
                            .font(.system(size: 17, weight: .semibold))
                        Text(model.days != model.loadedDays || model.activities.isEmpty ? "先将手表同步到手机小米运动健康，再点击刷新活动。" : "可取消“隐藏骑行”查看骑行记录；这些记录不会参与同步。")
                            .font(.system(size: 13)).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(26)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 0) {
                            ForEach(model.visible) { row in
                                HStack(spacing: 14) {
                                    Button { model.toggle(row) } label: {
                                        Image(systemName: model.selected.contains(row.id) ? "checkmark.circle.fill" : "circle")
                                            .font(.system(size: 23)).foregroundStyle(row.selectable ? teal : Color.gray.opacity(0.3))
                                    }.buttonStyle(.plain).frame(width: 28).disabled(!row.selectable || model.busy)
                                    Text(row.date).frame(width: 155, alignment: .leading).monospacedDigit()
                                    Text(row.sport).frame(width: 100, alignment: .leading).lineLimit(1)
                                    Text(row.distance).fontWeight(.semibold).frame(width: 90, alignment: .trailing).monospacedDigit()
                                    Text(row.duration).frame(width: 85, alignment: .trailing).monospacedDigit().foregroundStyle(.secondary)
                                    Spacer()
                                    Text(row.state).font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(row.selectable ? teal : .secondary).frame(width: 150, alignment: .leading)
                                }.font(.system(size: 13)).padding(.horizontal, 22).padding(.vertical, 18)
                                    .background(model.selected.contains(row.id) ? teal.opacity(0.06) : Color.clear)
                                    .contentShape(Rectangle()).onTapGesture { model.toggle(row) }
                                Divider()
                            }
                        }
                    }
                }
                Divider()
                HStack {
                    Text("最近刷新：\(model.lastCheck)")
                    Spacer()
                    if !model.range.isEmpty { Text("已读取 \(model.range)") }
                }.font(.system(size: 11)).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.vertical, 11)
            }.card().frame(maxHeight: .infinity)

            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("已选择 \(model.chosen.count) 条跑步").font(.system(size: 17, weight: .semibold))
                        Text(model.apiConnected ? "同步前再次检查 Strava 上的重复记录。" : "自动上传并保存，成功后自动记录；重复活动会跳过。")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("开始同步到 Strava") { model.sync() }.buttonStyle(FilledButton()).disabled(model.busy || model.chosen.isEmpty)
                }
                Text(model.duplicateText).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 22) {
                    Button { model.openFolder() } label: { Label("待上传文件夹", systemImage: "folder") }
                    Button { model.importRecords() } label: { Label("导入 Strava 查重记录", systemImage: "doc.badge.arrow.up") }.disabled(model.busy)
                    Button { model.openStrava() } label: { Label("打开 Strava", systemImage: "arrow.up.right.square") }
                        .accessibilityIdentifier("open-strava").accessibilityLabel("打开 Strava")
                    Spacer()
                }.buttonStyle(.plain).foregroundStyle(teal).font(.system(size: 12))
            }.padding(22).card()

            HStack(spacing: 10) {
                if model.busy { ProgressView().controlSize(.small).tint(.white) }
                Image(systemName: model.isError ? "exclamationmark.circle" : "info.circle").opacity(0.8)
                Text(model.message).font(.system(size: 12)).lineLimit(3).textSelection(.enabled)
                Spacer()
            }.foregroundStyle(model.isError ? Color.orange : .white.opacity(0.88)).frame(minHeight: 28)
        }
        .padding(26).foregroundStyle(ink).tint(teal)
        .background(LinearGradient(colors: [Color(red: 0.09, green: 0.13, blue: 0.20), Color(red: 0.04, green: 0.35, blue: 0.45)], startPoint: .topLeading, endPoint: .bottomTrailing))
        .preferredColorScheme(.light)
        .onChange(of: model.hideRides) { model.retainVisibleSelection() }
        .onChange(of: model.onlyRuns) { model.retainVisibleSelection() }
        .onChange(of: model.days) {
            model.selected = []
            if !model.busy { model.message = "时间范围已更改，点击刷新活动读取。" }
        }
        .sheet(isPresented: $model.loginVisible) {
            VStack(spacing: 20) {
                Text("小米账号扫码登录").font(.system(size: 22, weight: .bold))
                Text("用小米手机扫一扫，并在手机上确认登录。\n使用与小米运动健康、手表绑定的同一个账号。")
                    .multilineTextAlignment(.center).foregroundStyle(.secondary)
                if let image = model.qrImage {
                    Image(nsImage: image).resizable().interpolation(.none).scaledToFit().frame(width: 280, height: 280)
                } else { ProgressView().frame(width: 280, height: 280) }
                Text(model.message).font(.system(size: 12)).multilineTextAlignment(.center)
                Button("取消登录") { model.cancelLogin() }
            }.padding(32).frame(width: 400)
        }
    }
}

extension View {
    func card() -> some View {
        self.background(Color(red: 0.97, green: 0.98, blue: 1.0)).clipShape(RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(Color.white.opacity(0.2), lineWidth: 1))
            .shadow(color: .black.opacity(0.10), radius: 12, y: 6)
    }
}

final class StravaWebPanel: NSObject, WKUIDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private let caption = NSTextField(labelWithString: "")
    private let confirmation = NSButton(title: "已保存，标记本次已上传", target: nil, action: nil)
    private let stopAutomatic = NSButton(title: "停止同步", target: nil, action: nil)
    private let reload = NSButton(title: "刷新页面", target: nil, action: nil)
    private var preparedFiles: [URL] = []
    private var onConfirm: (() -> Void)?
    private let browserSession = StravaSession()
    private var uploadReady = false
    private var popupWindows: [NSWindow] = []
    private let browserInstance = UUID().uuidString
    private var uploader: StravaUpload?

    override init() {
        super.init()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = browserSession.dataStore
        webView = WKWebView(frame: .zero, configuration: configuration)
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
        if let file = Bundle.main.resourceURL?.appendingPathComponent("strava-upload.js") {
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
            if !self.uploadReady { self.loadUploadPage() }
            self.recordSessionStatus()
        }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func startUpload(_ items: [StravaUploadItem], onProgress: @escaping (String) -> Void,
                     onVerified: @escaping (StravaUploadItem, Int, Bool, @escaping (Bool) -> Void) -> Void,
                     onFinished: @escaping (String?) -> Void) {
        guard let uploader = uploader, !uploader.active else {
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
            self?.window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
        uploader.onVerified = onVerified
        uploader.onFinished = { [weak self] error in
            self?.stopAutomatic.isEnabled = false
            self?.reload.isEnabled = true
            self?.caption.stringValue = error ?? "所选跑步已处理，Strava 返回的结果已自动记录。"
            if error != nil { self?.window.makeKeyAndOrderFront(nil) }
            else { self?.preparedFiles = []; self?.window.orderOut(nil) }
            onFinished(error)
        }
        caption.stringValue = "正在连接 Strava，准备自动同步…"
        window.orderBack(nil)
        browserSession.restore { uploader.start(items) }
    }
    func cancelUpload() { uploader?.cancel() }
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
        let alert = NSAlert()
        alert.messageText = "Strava 页面提示"
        alert.informativeText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Strava 页面确认"
        alert.informativeText = message
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = SyncModel()
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let appMenu = NSMenu()
        let root = NSMenuItem()
        appMenu.addItem(root)
        let menu = NSMenu()
        menu.addItem(withTitle: "关于小米跑步同步", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "刷新活动", action: #selector(refresh), keyEquivalent: "r").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出小米跑步同步", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        root.submenu = menu
        NSApplication.shared.mainMenu = appMenu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 810), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "小米跑步同步"
        window.minSize = NSSize(width: 940, height: 760)
        window.contentView = NSHostingView(rootView: MainView(model: model))
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        model.run("status")
        if CommandLine.arguments.contains("--inspect-strava") {
            DispatchQueue.main.async { self.model.openStrava() }
        }
    }
    @objc func refresh() { if model.loggedIn { model.refresh() } }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { model.stop() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        model.stop()
        model.saveBrowserSession { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
