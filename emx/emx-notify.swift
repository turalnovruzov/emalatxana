// emx-notify: the small app that shows emx's notifications as emx's own (ADR 0006).
//
// emx/notify.py builds it with swiftc into an app bundle and runs it with one verb:
//   post              stdin: one JSON object {title, body, id, open}; shows it, no sound
//   status            reports the notification permission
//   authorize <file>  asks for the permission once (the normal prompt, not provisional)
//                     and writes its report into <file>
//   icon <dir>        writes the app's .iconset into <dir>; never touches Notification Center
//   click <file>      runs what a click on a banner runs, for the `open` object in <file>:
//                     `emx open <sid>`, then brings forward the terminal whose bundle id it printed
// post and status run as the executable directly. authorize must be launched through
// LaunchServices (`open -W -a <app> --args authorize <file>`): macOS refuses the request
// from anything but a running NSApplication, and stdout does not survive `open`.
// post, status and authorize report one JSON object:
//   {"auth", "alerts", "posted", "shown", "delivered"}, plus "error" when the system refused
// and exit 0 when they did their job, 3 when not authorized, 4 when the system refused the
// request, 1 on an error.
// With no verb it is the app macOS launches when he clicks a banner.

import AppKit
import UserNotifications

// Reached only through this, so `icon` (run while building) never registers the app with Notification Center.
var center: UNUserNotificationCenter { UNUserNotificationCenter.current() }

func wait(seconds: Double, until done: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while !done() && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    return done()
}

func authName(_ status: UNAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .notDetermined: return "notDetermined"
    case .provisional: return "provisional"
    @unknown default: return "unknown"
    }
}

func alertsName(_ setting: UNNotificationSetting) -> String {
    switch setting {
    case .enabled: return "enabled"
    case .disabled: return "disabled"
    case .notSupported: return "notSupported"
    @unknown default: return "unknown"
    }
}

func settings() -> UNNotificationSettings? {
    var found: UNNotificationSettings?
    center.getNotificationSettings { found = $0 }
    _ = wait(seconds: 10) { found != nil }
    return found
}

func delivered() -> [UNNotification] {
    var found: [UNNotification]?
    center.getDeliveredNotifications { found = $0 }
    _ = wait(seconds: 10) { found != nil }
    return found ?? []
}

let refusedCode: Int32 = 4

func reportLine(_ s: UNNotificationSettings, posted: Bool = false, shown: Bool = false, error: Error? = nil) -> (Data, Int32) {
    var line: [String: Any] = [
        "auth": authName(s.authorizationStatus),
        "alerts": alertsName(s.alertSetting),
        "posted": posted,
        "shown": shown,
        "delivered": delivered().count,
    ]
    if let error = error {
        line["error"] = "\(error)"
    }
    let data = try! JSONSerialization.data(withJSONObject: line, options: [.sortedKeys])
    if error != nil {
        return (data, refusedCode)
    }
    let allowed: Set<UNAuthorizationStatus> = [.authorized, .provisional]
    return (data, allowed.contains(s.authorizationStatus) && s.alertSetting != .disabled ? 0 : 3)
}

func report(posted: Bool = false, shown: Bool = false) -> Int32 {
    guard let s = settings() else {
        FileHandle.standardError.write("emx-notify: no notification settings\n".data(using: .utf8)!)
        return 1
    }
    let (data, code) = reportLine(s, posted: posted, shown: shown)
    print(String(data: data, encoding: .utf8)!)
    return code
}

func post() -> Int32 {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let request = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
          let title = request["title"] as? String,
          let body = request["body"] as? String,
          let ident = request["id"] as? String else {
        FileHandle.standardError.write("emx-notify: post needs {title, body, id} on stdin\n".data(using: .utf8)!)
        return 1
    }
    let content = UNMutableNotificationContent()
    content.title = title
    content.body = body
    content.sound = nil
    if let open = request["open"] as? [String: Any] {
        content.userInfo = ["open": open]
    }
    var added: Error??
    center.add(UNNotificationRequest(identifier: ident, content: content, trigger: nil)) { added = .some($0) }
    _ = wait(seconds: 10) { added != nil }
    let posted = added != nil && added! == nil
    // add's completion only says the request was accepted; a listed delivery is the evidence it showed.
    let shown = posted && wait(seconds: 10) { delivered().contains { $0.request.identifier == ident } }
    let code = report(posted: posted, shown: shown)
    return code == 0 && !posted ? 1 : code
}

class Authorizing: NSObject, NSApplicationDelegate {
    let path: String
    var finished = false

    init(path: String) {
        self.path = path
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        center.requestAuthorization(options: [.alert]) { _, error in
            center.getNotificationSettings { s in
                DispatchQueue.main.async { self.finish(s, error: error) }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 300) {
            center.getNotificationSettings { s in
                DispatchQueue.main.async { self.finish(s, error: nil) }
            }
        }
    }

    func finish(_ s: UNNotificationSettings, error: Error?) {
        guard !finished else { return }
        finished = true
        let (data, code) = reportLine(s, error: error)
        // Written in place, never replaced, so the 0600 file emx made keeps its mode.
        guard let handle = FileHandle(forWritingAtPath: path) else { exit(1) }
        handle.truncateFile(atOffset: 0)
        handle.write(data)
        handle.closeFile()
        // exit, not NSApp.terminate, which would always exit 0.
        exit(code)
    }
}

func authorize(_ path: String) -> Never {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let asking = Authorizing(path: path)
    app.delegate = asking
    app.run()
    exit(1)
}

// The same pattern as core._BUNDLE_ID.
let bundlePattern = "^[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$"
let openSeconds: Double = 5

// Runs `emx open <sid>` and waits for it, bounded; returns its exit status and the bundle id it printed, if valid.
func openSession(_ open: [String: Any]) -> (Int32, String?) {
    guard let python = open["python"] as? String, let emx = open["emx"] as? String,
          let sid = open["sid"] as? String else { return (1, nil) }
    let process = Process()
    let out = Pipe()
    process.executableURL = URL(fileURLWithPath: python)
    process.arguments = [emx, "open", sid]
    process.environment = (open["env"] as? [String: String]) ?? [:]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = out
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        return (1, nil)
    }
    if !wait(seconds: openSeconds, until: { !process.isRunning }) {
        process.terminate()
        return (1, nil)
    }
    let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    let line = text.split(separator: "\n", maxSplits: 1).first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
    let valid = line.count <= 255 && line.range(of: bundlePattern, options: .regularExpression) != nil
    return (process.terminationStatus, valid ? line : nil)
}

// A background app may not activate another one; the click made this helper active, so it yields to the terminal.
func bringForward(_ bundle: String) {
    if #available(macOS 14, *) {
        NSApp?.yieldActivation(toApplicationWithBundleIdentifier: bundle)
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle) else { return }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        NSWorkspace.shared.openApplication(at: url, configuration: configuration)
    } else {
        NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first?.activate(options: [.activateIgnoringOtherApps])
    }
}

func clickOpen(_ open: [String: Any]) -> Int32 {
    let (status, bundle) = openSession(open)
    if let bundle = bundle {
        bringForward(bundle)
    }
    return status == 0 ? 0 : 1
}

func click(_ path: String) -> Int32 {
    guard let data = FileManager.default.contents(atPath: path),
          let open = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return 1 }
    return clickOpen(open)
}

let inkShare: CGFloat = 0.6

func glyphLine(fontSize: CGFloat) -> CTLine {
    // U+FE0E asks for the text form of ⚒, never the emoji; Core Text picks the font that has the glyph.
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: fontSize),
        .foregroundColor: NSColor(srgbRed: 0xD7 / 255, green: 0x87 / 255, blue: 0x5F / 255, alpha: 1),
    ]
    return CTLineCreateWithAttributedString(NSAttributedString(string: "\u{2692}\u{FE0E}", attributes: attributes))
}

// The font size, per pixel of side, at which the glyph's ink spans `inkShare` of the square.
let glyphScale: CGFloat = {
    let ink = CTLineGetBoundsWithOptions(glyphLine(fontSize: 100), .useGlyphPathBounds)
    return inkShare * 100 / max(ink.width, ink.height, 1)
}()

func drawIcon(side: Int) -> Data? {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8, samplesPerPixel: 4,
        hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ), let context = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
    let size = CGFloat(side)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let radius = size * 0.225
    NSColor(srgbRed: 0x1C / 255, green: 0x1C / 255, blue: 0x1C / 255, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: size, height: size), xRadius: radius, yRadius: radius).fill()
    let line = glyphLine(fontSize: size * glyphScale)
    let ink = CTLineGetBoundsWithOptions(line, .useGlyphPathBounds)
    let cg = context.cgContext
    cg.textPosition = CGPoint(x: (size - ink.width) / 2 - ink.minX, y: (size - ink.height) / 2 - ink.minY)
    CTLineDraw(line, cg)
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

func icon(_ dir: String) -> Int32 {
    do {
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        for points in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let name = scale == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
                guard let png = drawIcon(side: points * scale) else { return 1 }
                try png.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name))
            }
        }
        return 0
    } catch {
        return 1
    }
}

class Clicked: NSObject, UNUserNotificationCenterDelegate, NSApplicationDelegate {
    var handled = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
            if !self.handled { NSApp.terminate(nil) }
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        handled = true
        if let open = response.notification.request.content.userInfo["open"] as? [String: Any] {
            _ = clickOpen(open)
        }
        completionHandler()
        // A click can also land while `post` waits; that run has no NSApplication and exits by itself.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { NSApp?.terminate(nil) }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }
}

let args = CommandLine.arguments
let verb = args.count > 1 ? args[1] : ""
let clicked = Clicked()
switch verb {
case "post":
    center.delegate = clicked
    exit(post())
case "status":
    exit(report())
case "authorize" where args.count == 3:
    authorize(args[2])
case "icon" where args.count == 3:
    exit(icon(args[2]))
case "click" where args.count == 3:
    exit(click(args[2]))
case "":
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.delegate = clicked
    app.run()
default:
    FileHandle.standardError.write("usage: emx-notify post|status|authorize <file>|icon <dir>|click <file>\n".data(using: .utf8)!)
    exit(1)
}
