// Draws the app icon (1024x1024 PNG): a rising line chart on a blue rounded tile.
// Usage: swift macos/make_icon.swift out.png
import AppKit

let size: CGFloat = 1024
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

// Tile: macOS icon grid leaves ~100px margin; corner radius ~ 22.5% of the tile.
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let path = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: NSColor.black.withAlphaComponent(0.28).cgColor)
ctx.addPath(path)
ctx.setFillColor(NSColor(srgbRed: 0.10, green: 0.36, blue: 0.67, alpha: 1).cgColor)
ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(path)
ctx.clip()
let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                          colors: [NSColor(srgbRed: 0.24, green: 0.55, blue: 0.92, alpha: 1).cgColor,
                                   NSColor(srgbRed: 0.09, green: 0.33, blue: 0.64, alpha: 1).cgColor] as CFArray,
                          locations: [0, 1])!
ctx.drawLinearGradient(gradient, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])

// Faint grid
ctx.setStrokeColor(NSColor.white.withAlphaComponent(0.16).cgColor)
ctx.setLineWidth(6)
for y in stride(from: 300.0, through: 720.0, by: 140.0) {
    ctx.move(to: CGPoint(x: 220, y: y)); ctx.addLine(to: CGPoint(x: 804, y: y))
}
ctx.strokePath()

// Rising line with a soft wash under it
let points = [CGPoint(x: 230, y: 330), CGPoint(x: 400, y: 450), CGPoint(x: 540, y: 400), CGPoint(x: 780, y: 680)]
let wash = CGMutablePath()
wash.move(to: CGPoint(x: points[0].x, y: 230))
points.forEach { wash.addLine(to: $0) }
wash.addLine(to: CGPoint(x: points.last!.x, y: 230))
wash.closeSubpath()
ctx.addPath(wash)
ctx.setFillColor(NSColor.white.withAlphaComponent(0.14).cgColor)
ctx.fillPath()

ctx.setStrokeColor(NSColor.white.cgColor)
ctx.setLineWidth(44)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
ctx.addLines(between: points)
ctx.strokePath()

// End dot with a ring
let end = points.last!
ctx.setFillColor(NSColor(srgbRed: 0.10, green: 0.36, blue: 0.67, alpha: 1).cgColor)
ctx.fillEllipse(in: CGRect(x: end.x - 58, y: end.y - 58, width: 116, height: 116))
ctx.setFillColor(NSColor.white.cgColor)
ctx.fillEllipse(in: CGRect(x: end.x - 38, y: end.y - 38, width: 76, height: 76))
ctx.restoreGState()
image.unlockFocus()

let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
