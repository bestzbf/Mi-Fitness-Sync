import AppKit
import WebKit

final class UploadProbe: NSObject, WKNavigationDelegate {
    var finished = false
    var passed = false
    let source: String
    var view: WKWebView!

    init(source: String) { self.source = source }

    func start() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1000, height: 700), configuration: config)
        view.navigationDelegate = self
        view.loadHTMLString(#"""
        <!doctype html><html><body>
        <a href="/activities/999">Unrelated activity must not count as success</a>
        <div id="uploadFile"><form action="/upload/files"><input type="file" name="files[]" class="files"></form>
          <p class="server-error" style="display:none">Server failed</p></div>
        <div id="uploadProgress"><ul class="uploads"></ul></div>
        <footer id="uploadFooter"><button class="save-and-view">Save</button><p class="saving-error" style="display:none">Save failed</p></footer>
        <script>
        window.Strava = {Uploads:{FileUploadView:function(){}}};
        window.jQuery = element => ({data: key => element.dataset[key]});
        window.changeCount = 0;
        window.saveCount = 0;
        document.querySelector('input[type=file]').addEventListener('change',()=>window.changeCount++);
        document.querySelector('.save-and-view').addEventListener('click',()=>window.saveCount++);
        </script></body></html>
        """#, baseURL: URL(string: "https://www.strava.com/upload/select")!)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let tests = #"""
        const require = (condition,message) => {if(!condition) throw new Error(message)};
        const list = document.querySelector('.uploads');
        const button = document.querySelector('.save-and-view');
        const payload = {name:'run_20261012_120000_0123456789abcdef.fit',data:'AQID'};
        const reset = () => {list.innerHTML='';delete window.xiaomiRunUpload;eval(source);button.className='save-and-view';};
        const row = (classes,body) => {list.innerHTML='<li class="'+classes+'">'+body+'</li>'};
        reset();
        require(window.xiaomiRunUpload.inspect().status==='ready_for_file','upload page not recognized');
        require(window.xiaomiRunUpload.selectFile(payload).status==='selected','file selection failed');
        const file = document.querySelector('input[type=file]').files[0];
        require(file.name===payload.name && file.size===3,'file metadata changed');
        require(Array.from(new Uint8Array(await file.arrayBuffer())).join(',')==='1,2,3','FIT bytes changed');
        require(window.xiaomiRunUpload.selectFile(payload).status==='already_selected' && window.changeCount===1,'file submitted twice');
        row('in-progress','<div class="activity-link"><a href="/activities/12345">Activity</a></div>');
        require(window.xiaomiRunUpload.inspect().status==='processing','processing treated as saved');
        row('finished','<div class="activity-link"><a href="/activities/12345">Activity</a></div><div class="sport-type"><span class="selection" data-current="Run"></span></div>');
        button.classList.add('disabled');
        require(window.xiaomiRunUpload.inspect().status==='processing','disabled save button used');
        button.classList.remove('disabled');
        require(window.xiaomiRunUpload.inspect().activity_id===12345,'wrong activity ID');
        require(window.xiaomiRunUpload.save().status==='saving','save not invoked');
        require(window.xiaomiRunUpload.save().status==='already_saving' && window.saveCount===1,'saved twice');
        reset();window.xiaomiRunUpload.selectFile(payload);
        row('error','<p class="error-message">此记录与 <a href="/activities/67890">Existing</a> 一致</p>');
        const duplicate = window.xiaomiRunUpload.inspect();
        require(duplicate.status==='duplicate' && duplicate.activity_id===67890,'duplicate not recognized');
        row('error','<p class="error-message">Duplicate <a href="https://example.test/activities/67890">Invalid</a></p>');
        require(window.xiaomiRunUpload.inspect().status==='error','external duplicate link trusted');
        row('finished','<div class="activity-link"><a href="/activities/12345">Activity</a></div><div class="sport-type"><span class="selection" data-current="Ride"></span></div>');
        require(window.xiaomiRunUpload.inspect().reason==='not-running','cycling accepted');
        row('finished','<div class="sport-type"><span class="selection" data-current="Run"></span></div>');
        require(window.xiaomiRunUpload.inspect().reason==='missing-activity-id','unrelated page link accepted');
        reset();row('finished','<div class="activity-link"><a href="/activities/12345">Old activity</a></div>');
        require(window.xiaomiRunUpload.inspect().reason==='previous-upload-unfinished','another upload overwritten');
        require(window.saveCount===1,'unexpected save');
        return {passed:true,checks:'exact FIT bytes, one selection, one save, duplicate ID, cycling rejection, unrelated links, unfinished uploads'};
        """#
        webView.callAsyncJavaScript(tests, arguments: ["source": source], in: nil, in: .page) { result in
            switch result {
            case .success(let value):
                if let value = value as? [String: Any], value["passed"] as? Bool == true {
                    self.passed = true
                    print("WebKit upload checks passed: \(value["checks"] ?? "")")
                }
            case .failure(let error): print("WebKit upload checks failed: \(error.localizedDescription)")
            }
            self.finished = true
        }
    }

    // Fixture tests never navigate to Strava or upload any real activity.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(navigationAction.navigationType == .other ? .allow : .cancel)
    }
}

_ = NSApplication.shared
let source = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
let probe = UploadProbe(source: source)
probe.start()
let deadline = Date().addingTimeInterval(15)
while !probe.finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
if !probe.passed { exit(1) }
