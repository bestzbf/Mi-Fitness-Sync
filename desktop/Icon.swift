import AppKit

let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)

func drawIcon(size: Int, filename: String) throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let context = NSGraphicsContext(bitmapImageRep: bitmap)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let scale = CGFloat(size) / 1024
    let transform = AffineTransform(scale: scale)
    (transform as NSAffineTransform).concat()
    let background = NSBezierPath(roundedRect: NSRect(x: 50, y: 50, width: 924, height: 924), xRadius: 204, yRadius: 204)
    NSGradient(starting: NSColor(calibratedRed: 0.04, green: 0.38, blue: 0.48, alpha: 1), ending: NSColor(calibratedRed: 0.09, green: 0.15, blue: 0.23, alpha: 1))!.draw(in: background, angle: 75)
    NSColor(calibratedRed: 1, green: 0.59, blue: 0.28, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 225, y: 240, width: 574, height: 574), xRadius: 185, yRadius: 185).fill()
    let config = NSImage.SymbolConfiguration(pointSize: 400, weight: .semibold).applying(NSImage.SymbolConfiguration(paletteColors: [NSColor(calibratedRed: 0.09, green: 0.15, blue: 0.23, alpha: 1)]))
    NSImage(systemSymbolName: "figure.run", accessibilityDescription: "跑步")!.withSymbolConfiguration(config)!.draw(in: NSRect(x: 310, y: 326, width: 400, height: 430))
    let bridge = NSBezierPath()
    bridge.move(to: NSPoint(x: 250, y: 200))
    bridge.curve(to: NSPoint(x: 774, y: 200), controlPoint1: NSPoint(x: 430, y: 100), controlPoint2: NSPoint(x: 594, y: 100))
    bridge.lineWidth = 25
    bridge.lineCapStyle = .round
    NSColor.white.withAlphaComponent(0.75).setStroke()
    bridge.stroke()
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: folder.appendingPathComponent(filename))
}

for base in [16, 32, 128, 256, 512] {
    try drawIcon(size: base, filename: "icon_\(base)x\(base).png")
    try drawIcon(size: base * 2, filename: "icon_\(base)x\(base)@2x.png")
}
