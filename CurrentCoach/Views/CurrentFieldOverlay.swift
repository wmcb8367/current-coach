import SwiftUI

/// Keeps the moving streaks between frames of the animation. The drawing
/// code asks it for the streaks at each tick; a lock keeps that safe from
/// wherever the canvas is drawn.
final class CurrentParticleEngine: @unchecked Sendable {
    private let lock = NSLock()
    private var system = CurrentParticleSystem(count: 0)
    private var lastTick: Date?

    func advance(
        to now: Date,
        frame: CurrentFrame,
        bounds: CurrentParticleSystem.Bounds,
        count: Int,
        cellsPerSecond: (x: Double, y: Double)
    ) -> [CurrentParticleSystem.Particle] {
        lock.lock()
        defer { lock.unlock() }
        if system.particles.count != count { system.resize(to: count) }
        // After a pause (the tab was away) carry on smoothly rather than jumping.
        let elapsed = lastTick.map { now.timeIntervalSince($0) } ?? 0
        lastTick = now
        if elapsed > 0 {
            system.step(frame: frame, bounds: bounds, dt: min(elapsed, 0.1), cellsPerSecond: cellsPerSecond)
        }
        return system.particles
    }

    func reset() {
        lock.lock()
        defer { lock.unlock() }
        system.reset()
        lastTick = nil
    }
}

extension CurrentRaster {
    /// The picture as an image the canvas can draw.
    var cgImage: CGImage? {
        guard let provider = CGDataProvider(data: Data(pixels) as CFData) else { return nil }
        return CGImage(
            width: width,
            height: height,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: true,
            intent: .defaultIntent
        )
    }
}

/// The current drawn over the map: the speed as colour, and streaks that
/// drift with the water. It sits on top of the map and lets every touch
/// through to it.
struct CurrentFieldOverlay: View {
    let frame: CurrentFrame?
    let image: CGImage?
    let grid: CurrentMapGrid?
    /// Where the grid's edges are on screen: left is west, top is north.
    let box: CGRect?
    let showStreaks: Bool
    let engine: CurrentParticleEngine

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: frame == nil || !showStreaks)) { timeline in
            Canvas { context, size in
                draw(in: &context, size: size, now: timeline.date)
            }
        }
        .allowsHitTesting(false)
    }

    private func draw(in context: inout GraphicsContext, size: CGSize, now: Date) {
        guard let box, let grid, box.width > 2, box.height > 2 else { return }
        if let image, image.width > 1, image.height > 1 {
            // Pixel centres sit on the grid's edges, so the picture overhangs by half a pixel.
            let halfX = box.width / CGFloat(image.width - 1) / 2
            let halfY = box.height / CGFloat(image.height - 1) / 2
            context.draw(
                Image(decorative: image, scale: 1).interpolation(.medium),
                in: box.insetBy(dx: -halfX, dy: -halfY)
            )
        }
        guard showStreaks, let frame else { return }
        let visible = box.intersection(CGRect(origin: .zero, size: size))
        guard !visible.isNull, visible.width > 2, visible.height > 2 else { return }

        let cols = Double(grid.cols - 1)
        let rows = Double(grid.rows - 1)
        let bounds = CurrentParticleSystem.Bounds(
            minX: Double((visible.minX - box.minX) / box.width) * cols,
            maxX: Double((visible.maxX - box.minX) / box.width) * cols,
            minY: Double((box.maxY - visible.maxY) / box.height) * rows,
            maxY: Double((box.maxY - visible.minY) / box.height) * rows
        )
        // Half a knot crosses a twentieth of the visible water each second,
        // whatever the zoom, so weak and strong streams both read at a glance.
        let metres = grid.metresPerCell
        let visibleMetres = (bounds.maxY - bounds.minY) * metres.y
        let timeScale = 0.05 * visibleMetres / (0.5 / CurrentVector.knotsPerMetrePerSecond)
        let count = min(1400, max(120, Int(visible.width * visible.height / 450)))
        let particles = engine.advance(
            to: now,
            frame: frame,
            bounds: bounds,
            count: count,
            cellsPerSecond: (x: timeScale / metres.x, y: timeScale / metres.y)
        )

        // Four shades from faint to bright: a streak fades in, then out.
        var paths = [Path](repeating: Path(), count: 4)
        for particle in particles where particle.trail.count > 1 {
            let shade = min(3, Int(particle.strength * 4))
            var started = false
            for point in particle.trail {
                let place = CGPoint(
                    x: box.minX + CGFloat(point.x / cols) * box.width,
                    y: box.maxY - CGFloat(point.y / rows) * box.height
                )
                if started {
                    paths[shade].addLine(to: place)
                } else {
                    paths[shade].move(to: place)
                    started = true
                }
            }
        }
        let style = StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round)
        for (shade, path) in paths.enumerated() {
            context.stroke(path, with: .color(.white.opacity(0.16 + 0.22 * Double(shade))), style: style)
        }
    }
}

/// The colour key: slack water on the left, a strong stream on the right.
struct CurrentLegendView: View {
    var scale: CurrentColorScale = .standard
    private let marks: [Double] = [0, 0.5, 1, 2, 3]

    private var gradient: LinearGradient {
        let top = scale.maxKnots
        let stops = scale.stops.map { stop in
            Gradient.Stop(
                color: Color(red: stop.red, green: stop.green, blue: stop.blue),
                location: CGFloat(stop.knots / top)
            )
        }
        return LinearGradient(stops: stops, startPoint: .leading, endPoint: .trailing)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            GeometryReader { geometry in
                ZStack(alignment: .topLeading) {
                    Capsule().fill(gradient).frame(height: 8)
                    ForEach(marks, id: \.self) { mark in
                        Text(mark == 0 ? "0" : String(format: mark < 1 ? "%.1f" : "%.0f", mark))
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(NT.textSecondary)
                            .fixedSize()
                            .position(
                                x: min(geometry.size.width - 6, max(4, geometry.size.width * CGFloat(mark / scale.maxKnots))),
                                y: 17
                            )
                    }
                }
            }
            .frame(height: 24)
            Text("knots")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(NT.textDim)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(width: 170)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(NT.bgPrimary.opacity(0.82))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(NT.borderSubtle, lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Colour key: dark blue is slack water, yellow one knot, red two knots and more")
    }
}
