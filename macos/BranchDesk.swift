import AppKit
import WebKit

struct DesktopConfig: Decodable {
    let application: String
    let default_repo: String
    let python: String
    let path: String

    func validate() throws {
        let paths = [application, default_repo, python]
        guard paths.allSatisfy({ $0.hasPrefix("/") && !$0.contains("\0") }),
              !path.isEmpty,
              path.split(separator: ":", omittingEmptySubsequences: false)
                .allSatisfy({ $0.hasPrefix("/") && !$0.contains("\0") }) else {
            throw DesktopError.invalidConfig
        }
    }
}

enum DesktopError: Error {
    case invalidConfig
}

enum NavigationPolicy {
    static func localOrigin(_ url: URL) -> String? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "http", parts.percentEncodedHost == "127.0.0.1",
              parts.user == nil, parts.password == nil,
              let port = parts.port, (1...65535).contains(port) else { return nil }
        return "http://127.0.0.1:\(port)"
    }

    static func runtimeURL(_ text: String) -> URL? {
        guard let url = URL(string: text), localOrigin(url) != nil,
              let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.path == "/", parts.query == nil,
              let token = parts.fragment,
              token.range(of: "^[A-Za-z0-9_-]{32,128}$", options: .regularExpression) != nil else { return nil }
        return url
    }

    static func externalURL(_ url: URL) -> URL? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.user == nil, parts.password == nil else { return nil }
        if parts.scheme == "https", let host = parts.host, !host.isEmpty {
            return parts.url
        }
        if parts.scheme == "codex", parts.host == "threads", parts.port == nil,
           parts.query == nil, parts.fragment == nil,
           parts.percentEncodedPath.range(of: "^/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$", options: .regularExpression) != nil {
            return parts.url
        }
        return nil
    }
}

// Bridge output is private: never send it to logs or show a returned URL in the UI.
struct BridgeResult {
    let status: String
    let url: URL?

    static func read(_ data: Data, succeeded: Bool) -> BridgeResult {
        guard let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let status = value["status"] as? String else { return BridgeResult(status: "error", url: nil) }
        if succeeded, ["running", "started"].contains(status),
           let text = value["url"] as? String, let url = NavigationPolicy.runtimeURL(text) {
            return BridgeResult(status: status, url: url)
        }
        return BridgeResult(status: succeeded && status == "not_running" ? "not_running" : "error", url: nil)
    }
}

func selfTest() {
    let token = String(repeating: "a", count: 32)
    let local = "http://127.0.0.1:8765/"
    precondition(NavigationPolicy.runtimeURL(local + "#" + token) != nil)
    for bad in [local, "http://localhost:8765/#" + token, "http://user@127.0.0.1:8765/#" + token,
                "http://127.0.0.1:0/#" + token, "http://127.0.0.1:65536/#" + token,
                local + "?token=abc#" + token, local + "api/status#" + token,
                "https://127.0.0.1:8765/#" + token, "http://127.0.0.1.evil:8765/#" + token] {
        precondition(NavigationPolicy.runtimeURL(bad) == nil)
    }
    precondition(NavigationPolicy.localOrigin(URL(string: local + "api/status")!) == "http://127.0.0.1:8765")
    precondition(NavigationPolicy.localOrigin(URL(string: "http://%31%32%37.0.0.1:8765/")!) == nil)
    precondition(NavigationPolicy.externalURL(URL(string: "https://example.com/item#section")!)?.absoluteString == "https://example.com/item#section")
    precondition(NavigationPolicy.externalURL(URL(string: "codex://threads/019cfed1-7171-7001-9001-bcdef0123456")!) != nil)
    for bad in ["file:///etc/passwd", "javascript:alert(1)", "http://example.com/", "codex://threads/hello",
                "codex://threads/019cfed1-7171-7001-9001-bcdef0123456?x=1", "https://user:secret@example.com/"] {
        precondition(NavigationPolicy.externalURL(URL(string: bad)!) == nil)
    }
    let good = DesktopConfig(application: "/tmp/app", default_repo: "/tmp/repo", python: "/usr/bin/python3", path: "/usr/bin:/bin")
    precondition((try? good.validate()) != nil)
    let bad = DesktopConfig(application: "relative", default_repo: "/tmp", python: "/usr/bin/python3", path: "/usr/bin")
    precondition((try? bad.validate()) == nil)
    let blankPath = DesktopConfig(application: "/tmp/app", default_repo: "/tmp", python: "/usr/bin/python3", path: "/usr/bin:")
    precondition((try? blankPath.validate()) == nil)
    let running = Data("{\"status\":\"running\",\"url\":\"\(local)#\(token)\"}".utf8)
    precondition(BridgeResult.read(running, succeeded: true).url != nil)
    precondition(BridgeResult.read(running, succeeded: false).status == "error")
    precondition(BridgeResult.read(Data("{\"status\":\"not_running\"}".utf8), succeeded: true).status == "not_running")
    precondition(BridgeResult.read(Data("{}".utf8), succeeded: true).status == "error")
    precondition(NSImage(systemSymbolName: "arrow.triangle.branch", accessibilityDescription: "Branch Desk") != nil)
    print("Branch Desk native policy self-test passed (no UI, network or service).")
}

final class BridgeTask: @unchecked Sendable {
    private let lock = NSLock()
    private var output = Data()
    private var overflow = false

    func append(_ data: Data, process: Process) {
        lock.lock()
        if output.count + data.count <= 1_048_576 { output.append(data) } else { overflow = true }
        let stop = overflow
        lock.unlock()
        if stop, process.isRunning { process.terminate() }
    }

    func finish() -> (Data, Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (output, overflow)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply {
    private var window: NSWindow!
    private var statusItem: NSStatusItem?
    private var statusMenu: NSMenu?
    private var webView: WKWebView!
    private var overlay: NSView!
    private var statusLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var spinner: NSProgressIndicator!
    private var startButton: NSButton!
    private var reconnectButton: NSButton!
    private var returnButton: NSButton!
    private var config: DesktopConfig?
    private var configURL: URL?
    private var selectedProjectID: String?
    private var currentOrigin: String?
    private var loadedWorkbench = false
    private var connecting = false
    private var leaving = false
    private var popups: [ObjectIdentifier: WKWebView] = [:]
    private var popupBlocker: WKContentRuleList?

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let bundle = Bundle.main.bundleIdentifier,
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
                .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            existing.activate(options: [.activateAllWindows])
            NSApp.terminate(nil)
            return
        }
        makeMenu()
        makeWindow()
        makeStatusItem()
        compilePopupBlocker()
        do {
            guard let url = Bundle.main.url(forResource: "workbench", withExtension: "json") else { throw DesktopError.invalidConfig }
            let value = try JSONDecoder().decode(DesktopConfig.self, from: Data(contentsOf: url))
            try value.validate()
            config = value
            configURL = url
            connect(start: false)
        } catch {
            showStatus("应用配置不可用", detail: "请重新构建 Branch Desk 应用，保留原工作台和业务仓库。", busy: false, allowStart: false)
            reconnectButton.isEnabled = false
        }
        showWorkbench()
    }

    private func makeStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item
        if let button = item.button {
            let image = NSImage(systemSymbolName: "arrow.triangle.branch", accessibilityDescription: "Branch Desk")
            image?.isTemplate = true
            image?.size = NSSize(width: 18, height: 18)
            button.image = image
            button.toolTip = "Branch Desk · 点击打开，右键显示菜单"
            button.setAccessibilityLabel("Branch Desk")
            button.target = self
            button.action = #selector(statusItemClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    @objc private func statusItemClicked(_ sender: NSStatusBarButton) {
        guard NSApp.currentEvent?.type == .rightMouseUp else { showWorkbench(); return }
        let menu = NSMenu()
        let show = menu.addItem(withTitle: "显示工作台", action: #selector(showWorkbench), keyEquivalent: "")
        show.target = self
        let hide = menu.addItem(withTitle: "隐藏工作台", action: #selector(hideWorkbench), keyEquivalent: "")
        hide.target = self
        menu.autoenablesItems = false
        hide.isEnabled = window.isVisible && !window.isMiniaturized && !NSApp.isHidden
        menu.addItem(.separator())
        let quit = menu.addItem(withTitle: "退出 Branch Desk", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.minimumWidth = 200
        menu.delegate = self
        statusMenu = menu
        statusItem?.menu = menu
        sender.performClick(nil)
    }

    func menuDidClose(_ menu: NSMenu) {
        guard menu === statusMenu else { return }
        statusItem?.menu = nil
        statusMenu = nil
    }

    @objc private func showWorkbench() {
        NSApp.unhide(nil)
        if window.isMiniaturized { window.deminiaturize(nil) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func hideWorkbench() {
        window.orderOut(nil)
    }

    private func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于 Branch Desk", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 Branch Desk", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 Branch Desk", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let fileItem = NSMenuItem()
        menu.addItem(fileItem)
        let file = NSMenu(title: "窗口")
        fileItem.submenu = file
        file.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        file.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        let refresh = file.addItem(withTitle: "刷新页面", action: #selector(reloadPage), keyEquivalent: "r")
        refresh.target = self
        let reconnect = file.addItem(withTitle: "重新连接工作台", action: #selector(reconnect), keyEquivalent: "")
        reconnect.target = self
        let editItem = NSMenuItem()
        menu.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        editItem.submenu = edit
        for (title, selector, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        let redo = NSMenuItem(title: "重做", action: NSSelectorFromString("redo:"), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.insertItem(redo, at: 1)
        NSApp.mainMenu = menu
        NSApp.windowsMenu = file
    }

    private func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Branch Desk"
        window.minSize = NSSize(width: 860, height: 600)
        window.setFrameAutosaveName("BranchDeskMainWindow")
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        configuration.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "copyText")
        configuration.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "chooseProjectDirectory")
        configuration.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "connectProject")
        configuration.userContentController.addUserScript(WKUserScript(
            source: """
            window.branchDeskDesktop = {
                copyText: text => window.webkit.messageHandlers.copyText.postMessage(text),
                chooseProjectDirectory: () => window.webkit.messageHandlers.chooseProjectDirectory.postMessage(''),
                connectProject: id => window.webkit.messageHandlers.connectProject.postMessage(id)
            };
            (() => {
                const mark = () => {
                    if (!document.documentElement) return false;
                    document.documentElement.classList.add('branch-desk-desktop');
                    return true;
                };
                if (!mark()) {
                    const observer = new MutationObserver(() => { if (mark()) observer.disconnect(); });
                    observer.observe(document, {childList: true});
                }
            })();
            """,
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        let content = NSView()
        window.contentView = content
        content.addSubview(webView)
        webView.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                                     webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                                     webView.topAnchor.constraint(equalTo: content.topAnchor),
                                     webView.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        overlay = NSVisualEffectView()
        (overlay as? NSVisualEffectView)?.material = .windowBackground
        (overlay as? NSVisualEffectView)?.blendingMode = .withinWindow
        content.addSubview(overlay)
        overlay.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([overlay.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                                     overlay.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                                     overlay.topAnchor.constraint(equalTo: content.topAnchor),
                                     overlay.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        statusLabel = NSTextField(labelWithString: "正在连接工作台…")
        statusLabel.font = .systemFont(ofSize: 24, weight: .semibold)
        detailLabel = NSTextField(wrappingLabelWithString: "")
        detailLabel.alignment = .center
        detailLabel.textColor = .secondaryLabelColor
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        startButton = NSButton(title: "启动工作台", target: self, action: #selector(startService))
        startButton.bezelStyle = .rounded
        reconnectButton = NSButton(title: "重新连接", target: self, action: #selector(reconnect))
        reconnectButton.bezelStyle = .rounded
        returnButton = NSButton(title: "返回当前页面", target: self, action: #selector(returnToPage))
        returnButton.bezelStyle = .rounded
        let buttons = NSStackView(views: [returnButton, reconnectButton, startButton])
        buttons.spacing = 12
        let stack = NSStackView(views: [spinner, statusLabel, detailLabel, buttons])
        stack.orientation = .vertical
        stack.spacing = 18
        stack.alignment = .centerX
        overlay.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
                                     stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
                                     stack.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
                                     stack.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor, constant: 30)])
    }

    private func showStatus(_ title: String, detail: String, busy: Bool, allowStart: Bool) {
        overlay.isHidden = false
        statusLabel.stringValue = title
        detailLabel.stringValue = detail
        spinner.isHidden = !busy
        if busy { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        reconnectButton.isEnabled = !busy && config != nil
        startButton.isHidden = !allowStart
        startButton.isEnabled = !busy
        returnButton.isHidden = busy || !loadedWorkbench
    }

    @objc private func returnToPage() {
        if loadedWorkbench && !connecting { overlay.isHidden = true }
    }

    @objc private func startService() {
        guard !connecting else { return }
        withLeavePermission(action: "连接新的工作台页面", detail: "连接后会重新载入页面，请先保存当前修改。") { [weak self] allowed in
            if allowed { self?.connect(start: true) }
        }
    }

    @objc private func reconnect() {
        guard !connecting else { return }
        withLeavePermission(action: "重新连接工作台", detail: "重新连接会重新载入页面，请先保存当前修改。") { [weak self] allowed in
            if allowed { self?.connect(start: false) }
        }
    }

    @objc private func reloadPage() {
        guard !connecting else { return }
        guard loadedWorkbench else { connect(start: false); return }
        withLeavePermission(action: "刷新工作台", detail: "刷新会重新载入页面，请先保存当前修改。") { [weak self] allowed in
            guard let self, allowed else { return }
            self.showStatus("正在刷新工作台…", detail: "后台任务会继续执行。", busy: true, allowStart: false)
            self.webView.reload()
        }
    }

    private func withLeavePermission(action: String, detail: String, completion: @escaping (Bool) -> Void) {
        guard !leaving else { completion(false); return }
        guard loadedWorkbench else { completion(true); return }
        leaving = true
        var evaluated = false
        let checked: (Any?) -> Void = { [weak self] value in
            guard let self, !evaluated else { return }
            evaluated = true
            if value as? Bool == true { self.leaving = false; completion(true); return }
            let alert = NSAlert()
            alert.messageText = "\(action)？"
            alert.informativeText = "\(detail) 未保存的内容或正在进行的操作可能仍需要处理。"
            alert.addButton(withTitle: "取消")
            alert.addButton(withTitle: "继续")
            self.showWorkbench()
            alert.beginSheetModal(for: self.window) { result in
                self.leaving = false
                completion(result == .alertSecondButtonReturn)
            }
        }
        webView.evaluateJavaScript("window.branchDeskDesktop?.canLeave?.() ?? false") { value, _ in checked(value) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { checked(nil) }
    }

    private func connect(start: Bool) {
        guard !connecting, let config, let configURL,
              let bridge = Bundle.main.url(forResource: "desktop_bridge", withExtension: "py") else { return }
        connecting = true
        showStatus(start ? "正在启动工作台…" : "正在连接工作台…",
                   detail: start ? "只在没有现有服务时启动，不会重启或关闭已有后台。" : "正在检查本机现有服务，不会自动启动或重启。", busy: true, allowStart: false)
        let executable = config.python
        let searchPath = config.path
        let args = [bridge.path, "--config", configURL.path] + (selectedProjectID.map { ["--project-id", $0] } ?? []) + (start ? ["--start"] : [])
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: executable)
            task.arguments = args
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = searchPath
            task.environment = environment
            task.standardInput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            let pipe = Pipe()
            task.standardOutput = pipe
            let capture = BridgeTask()
            let finishedReading = DispatchSemaphore(value: 0)
            pipe.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; finishedReading.signal() }
                else { capture.append(data, process: task) }
            }
            var result = BridgeResult(status: "error", url: nil)
            do {
                try task.run()
                let deadline = Date().addingTimeInterval(25)
                while task.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
                if task.isRunning {
                    task.terminate()
                    let grace = Date().addingTimeInterval(1)
                    while task.isRunning && Date() < grace { Thread.sleep(forTimeInterval: 0.05) }
                    if task.isRunning { kill(task.processIdentifier, SIGKILL) }
                }
                task.waitUntilExit()
                let complete = finishedReading.wait(timeout: .now() + 2) == .success
                pipe.fileHandleForReading.readabilityHandler = nil
                let (data, overflow) = capture.finish()
                result = BridgeResult.read(data, succeeded: complete && !overflow && task.terminationStatus == 0 && Date() < deadline)
            } catch {
                pipe.fileHandleForReading.readabilityHandler = nil
            }
            DispatchQueue.main.async { [weak self] in self?.connected(result) }
        }
    }

    private func connected(_ result: BridgeResult) {
        connecting = false
        if let url = result.url, let origin = NavigationPolicy.localOrigin(url) {
            currentOrigin = origin
            loadedWorkbench = false
            webView.load(URLRequest(url: url))
        } else if result.status == "not_running" {
            showStatus("工作台尚未运行", detail: "点击“启动工作台”启动本机后台。关闭此窗口后，后台和当前页面仍会保留。", busy: false, allowStart: true)
        } else {
            showStatus("暂时无法连接工作台", detail: "已有服务可能正在忙碌、配置已变化或仍是旧版本。稍后重新连接；不会重复启动或自动关闭服务。", busy: false, allowStart: false)
        }
    }

    private func compilePopupBlocker() {
        let rules = "[{\"trigger\":{\"url-filter\":\".*\"},\"action\":{\"type\":\"block\"}}]"
        WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "BranchDeskPopupNoNetwork", encodedContentRuleList: rules) { [weak self] rules, _ in
            self?.popupBlocker = rules
        }
    }

    private func closePopup(_ popup: WKWebView) {
        popup.stopLoading()
        popup.navigationDelegate = nil
        popup.uiDelegate = nil
        popups.removeValue(forKey: ObjectIdentifier(popup))
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if webView !== self.webView {
            if url.absoluteString == "about:blank" { decisionHandler(.allow); return }
            decisionHandler(.cancel)
            if let external = NavigationPolicy.externalURL(url) { NSWorkspace.shared.open(external) }
            closePopup(webView)
            return
        }
        if let origin = NavigationPolicy.localOrigin(url), origin == currentOrigin {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if navigationAction.targetFrame?.isMainFrame != false, let external = NavigationPolicy.externalURL(url) {
                NSWorkspace.shared.open(external)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping @MainActor @Sendable (WKNavigationResponsePolicy) -> Void) {
        if webView !== self.webView {
            decisionHandler(navigationResponse.response.url?.absoluteString == "about:blank" ? .allow : .cancel)
            return
        }
        guard webView === self.webView,
              let url = navigationResponse.response.url,
              NavigationPolicy.localOrigin(url) == currentOrigin else { decisionHandler(.cancel); return }
        if let response = navigationResponse.response as? HTTPURLResponse, response.statusCode >= 400 {
            decisionHandler(.cancel)
            connectionFailed()
        } else { decisionHandler(.allow) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView === self.webView else { return }
        loadedWorkbench = true
        overlay.isHidden = true
        webView.evaluateJavaScript("window.dispatchEvent(new Event('branch-desk:activate'))", completionHandler: nil)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard webView === self.webView, (error as NSError).code != NSURLErrorCancelled else { return }
        connectionFailed()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard webView === self.webView, (error as NSError).code != NSURLErrorCancelled else { return }
        connectionFailed()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if webView === self.webView { connectionFailed() } else { closePopup(webView) }
    }

    private func connectionFailed() {
        showStatus("工作台连接已中断", detail: "当前窗口保留。请重新连接确认后台状态；页面中的本地草稿仍按原规则保存。", busy: false, allowStart: false)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard webView === self.webView, let url = navigationAction.request.url else { return nil }
        if let external = NavigationPolicy.externalURL(url) { NSWorkspace.shared.open(external); return nil }
        guard url.absoluteString == "about:blank", let popupBlocker else { return nil }
        // Related WebViews must keep the supplied data store or WebKit raises an exception.
        configuration.userContentController = WKUserContentController()
        configuration.userContentController.add(popupBlocker)
        let popup = WKWebView(frame: .zero, configuration: configuration)
        popup.navigationDelegate = self
        popup.uiDelegate = self
        popups[ObjectIdentifier(popup)] = popup
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self, weak popup] in
            if let popup { self?.closePopup(popup) }
        }
        return popup
    }

    func webViewDidClose(_ webView: WKWebView) {
        if webView !== self.webView { closePopup(webView) }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage, replyHandler: @escaping @MainActor @Sendable (Any?, String?) -> Void) {
        let origin = message.frameInfo.securityOrigin
        guard message.webView === webView, message.frameInfo.isMainFrame, loadedWorkbench,
              origin.protocol == "http", origin.host == "127.0.0.1",
              currentOrigin == "http://127.0.0.1:\(origin.port)",
              let text = message.body as? String, text.utf8.count <= 1_048_576 else {
            replyHandler(nil, "工作台请求无效。")
            return
        }
        if message.name == "chooseProjectDirectory" {
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.allowsMultipleSelection = false
            panel.beginSheetModal(for: window) { result in
                replyHandler(result == .OK ? panel.url?.path : nil, nil)
            }
            return
        }
        if message.name == "connectProject" {
            guard text.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil, !connecting else {
                replyHandler(nil, "项目标识无效或正在连接。")
                return
            }
            withLeavePermission(action: "切换项目", detail: "切换会重新载入页面，请先保存当前修改。") { [weak self] allowed in
                guard let self, allowed else { replyHandler(false, nil); return }
                self.selectedProjectID = text
                self.connect(start: false)
                replyHandler(true, nil)
            }
            return
        }
        guard message.name == "copyText" else { replyHandler(nil, "未知工作台请求。") ; return }
        NSPasteboard.general.clearContents()
        if NSPasteboard.general.setString(text, forType: .string) { replyHandler(true, nil) }
        else { replyHandler(nil, "无法写入剪贴板，请重试。") }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable () -> Void) {
        guard webView === self.webView else { completionHandler(); return }
        let alert = NSAlert()
        alert.messageText = "Branch Desk"
        alert.informativeText = String(message.prefix(4000))
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (Bool) -> Void) {
        guard webView === self.webView else { completionHandler(false); return }
        let alert = NSAlert()
        alert.messageText = "Branch Desk"
        alert.informativeText = String(message.prefix(4000))
        alert.addButton(withTitle: "取消")
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertSecondButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (String?) -> Void) {
        guard webView === self.webView else { completionHandler(nil); return }
        let alert = NSAlert()
        alert.messageText = "Branch Desk"
        alert.informativeText = String(prompt.prefix(4000))
        let input = NSTextField(string: defaultText ?? "")
        input.frame = NSRect(x: 0, y: 0, width: 360, height: 24)
        alert.accessoryView = input
        alert.addButton(withTitle: "取消")
        alert.addButton(withTitle: "确定")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertSecondButtonReturn ? input.stringValue : nil) }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWorkbench()
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard loadedWorkbench else { return }
        webView.evaluateJavaScript("window.dispatchEvent(new Event('branch-desk:activate'))", completionHandler: nil)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard loadedWorkbench else { return .terminateNow }
        guard !leaving else { return .terminateCancel }
        withLeavePermission(action: "退出 Branch Desk", detail: "后台服务和任务会继续执行；退出会关闭当前页面。") { allowed in
            sender.reply(toApplicationShouldTerminate: allowed)
        }
        return .terminateLater
    }
}

if CommandLine.arguments.contains("--self-test") {
    selfTest()
} else {
    MainActor.assumeIsolated {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
