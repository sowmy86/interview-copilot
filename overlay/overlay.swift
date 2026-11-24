// overlay — a floating, always-on-top, translucent window that shows the copilot UI.
//
//   overlay http://127.0.0.1:8765/
//
// Global hotkeys (no Accessibility permission needed):
//   ⌃⌥A  answer now      ⌃⌥H  hide / show      ⌃⌥C  clear      ⌃⌥S  pause / resume
//   ⌃⌥=  / ⌃⌥-  background more / less opaque (text stays crisp)
// Drag the window by its header (or the strip above it); resize from any edge.
// Header buttons: – hides (⌃⌥H brings it back), × quits the whole copilot.

import AppKit
import Carbon.HIToolbox
import WebKit

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }   // allow typing in the question box
    override var canBecomeMain: Bool { false }
}

/// Strip at the top used to drag the borderless window around.
final class DragBar: NSView {
    override var mouseDownCanMoveWindow: Bool { false }  // we move it ourselves (beginDrag)
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }  // drag even when not focused
    override func mouseDown(with event: NSEvent) { (NSApp.delegate as? AppDelegate)?.beginDrag() }
    override func draw(_ dirtyRect: NSRect) {
        let pill = NSRect(x: bounds.midX - 26, y: bounds.midY - 2.5, width: 52, height: 5)
        NSColor.white.withAlphaComponent(0.55).setFill()
        NSBezierPath(roundedRect: pill, xRadius: 2, yRadius: 2).fill()
    }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
}

/// Web view that reacts to the first click even when the panel isn't focused.
final class WebView: WKWebView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var panel: Panel!
    var web: WKWebView!
    var dragMonitor: Any?
    var dragStartMouse = NSPoint.zero
    var dragStartOrigin = NSPoint.zero
    let url: URL

    init(url: URL) { self.url = url }

    func applicationDidFinishLaunching(_ note: Notification) {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w: CGFloat = 460, h: CGFloat = 520
        let frame = NSRect(x: screen.maxX - w - 24, y: screen.maxY - h - 24, width: w, height: h)

        panel = Panel(
            contentRect: frame,
            styleMask: [.borderless, .resizable, .nonactivatingPanel, .fullSizeContentView],
            backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.alphaValue = 1.0
        panel.minSize = NSSize(width: 300, height: 220)
        panel.setFrameAutosaveName("InterviewCopilotOverlay")

        let root = NSView(frame: NSRect(origin: .zero, size: frame.size))
        root.wantsLayer = true
        let barH: CGFloat = 14
        let bar = DragBar(frame: NSRect(x: 0, y: frame.height - barH, width: frame.width, height: barH))
        bar.autoresizingMask = [.width, .minYMargin]
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "win")
        web = WebView(frame: NSRect(x: 0, y: 0, width: frame.width, height: frame.height - barH), configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        web.navigationDelegate = self
        root.addSubview(web)
        root.addSubview(bar)
        panel.contentView = root
        load()

        panel.orderFrontRegardless()
        registerHotkeys()
    }

    func load() { web.load(URLRequest(url: url)) }

    // Messages from the page: "drag" (mousedown on the header), "hide", "quit".
    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        switch message.body as? String {
        case "drag": beginDrag()
        case "hide": panel.orderOut(nil)
        case "quit":  // ask the server to shut down (it stops listener + whisper), then leave
            panel.orderOut(nil)
            var req = URLRequest(url: url.appendingPathComponent("quit"))
            req.httpMethod = "POST"
            URLSession.shared.dataTask(with: req) { _, _, _ in exit(0) }.resume()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { exit(0) }
        default: break
        }
    }

    /// Move the window with the mouse until the button is released.
    func beginDrag() {
        guard NSEvent.pressedMouseButtons & 1 == 1 else { return }  // already released
        dragStartMouse = NSEvent.mouseLocation
        dragStartOrigin = panel.frame.origin
        if let m = dragMonitor { NSEvent.removeMonitor(m) }
        dragMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDragged, .leftMouseUp]) { [weak self] e in
            guard let self else { return e }
            if e.type == .leftMouseUp {
                if let m = self.dragMonitor { NSEvent.removeMonitor(m) }
                self.dragMonitor = nil
                return e
            }
            let p = NSEvent.mouseLocation
            self.panel.setFrameOrigin(NSPoint(x: self.dragStartOrigin.x + p.x - self.dragStartMouse.x,
                                              y: self.dragStartOrigin.y + p.y - self.dragStartMouse.y))
            return nil  // swallow so the page doesn't start a text selection
        }
    }

    // Server may not be up yet — retry.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { self.load() }
    }

    func toggle() {
        if panel.isVisible { panel.orderOut(nil) } else { panel.orderFrontRegardless(); panel.makeKey() }
    }

    func js(_ s: String) { web.evaluateJavaScript(s, completionHandler: nil) }

    // MARK: hotkeys

    func registerHotkeys() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async { (NSApp.delegate as? AppDelegate)?.hotkey(Int(id.id)) }
            return noErr
        }, 1, &spec, nil, nil)

        let mods = UInt32(controlKey | optionKey)
        let keys: [(Int, Int)] = [(kVK_ANSI_A, 1), (kVK_ANSI_H, 2), (kVK_ANSI_C, 3), (kVK_ANSI_Equal, 4), (kVK_ANSI_Minus, 5), (kVK_ANSI_S, 6)]
        for (code, id) in keys {
            var ref: EventHotKeyRef?
            let hk = EventHotKeyID(signature: OSType(0x4350_4C54), id: UInt32(id))  // 'CPLT'
            let st = RegisterEventHotKey(UInt32(code), mods, hk, GetApplicationEventTarget(), 0, &ref)
            if st != noErr {
                FileHandle.standardError.write("[overlay] hotkey \(id) failed to register (\(st)) — use the buttons instead\n".data(using: .utf8)!)
            }
        }
    }

    func hotkey(_ id: Int) {
        switch id {
        case 1: js("window.copilot && window.copilot.answer()")
        case 2: toggle()
        case 3: js("window.copilot && window.copilot.clear()")
        case 4: js("window.copilot && window.copilot.bg(10)")
        case 5: js("window.copilot && window.copilot.bg(-10)")
        case 6: js("window.copilot && window.copilot.togglePause()")
        default: break
        }
    }
}

let arg = CommandLine.arguments.dropFirst().first ?? "http://127.0.0.1:8765/"
let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon
let delegate = AppDelegate(url: URL(string: arg)!)
app.delegate = delegate
// quit when the parent server dies
signal(SIGTERM) { _ in exit(0) }
Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
    if getppid() == 1 { exit(0) }
}
app.run()
