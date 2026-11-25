import Foundation

/// Speed of the current to a colour, the way current maps usually read:
/// deep blue for slack water through cyan, green and yellow to red and
/// magenta for a strong stream. The steps are close together at the weak
/// end, where most sailing venues live.
struct CurrentColorScale: Sendable, Equatable {
    struct Stop: Sendable, Equatable {
        let knots: Double
        let red: Double
        let green: Double
        let blue: Double
    }

    struct RGB: Sendable, Equatable {
        let red: Double
        let green: Double
        let blue: Double
    }

    let stops: [Stop]

    static let standard = CurrentColorScale(stops: [
        Stop(knots: 0.00, red: 0.071, green: 0.118, blue: 0.290),
        Stop(knots: 0.10, red: 0.098, green: 0.220, blue: 0.494),
        Stop(knots: 0.20, red: 0.086, green: 0.400, blue: 0.702),
        Stop(knots: 0.30, red: 0.063, green: 0.600, blue: 0.741),
        Stop(knots: 0.50, red: 0.141, green: 0.745, blue: 0.541),
        Stop(knots: 0.75, red: 0.522, green: 0.820, blue: 0.290),
        Stop(knots: 1.00, red: 0.933, green: 0.843, blue: 0.220),
        Stop(knots: 1.50, red: 0.965, green: 0.576, blue: 0.161),
        Stop(knots: 2.00, red: 0.910, green: 0.298, blue: 0.188),
        Stop(knots: 3.00, red: 0.784, green: 0.149, blue: 0.482),
        Stop(knots: 4.00, red: 0.580, green: 0.200, blue: 0.722),
    ])

    var maxKnots: Double { stops.last?.knots ?? 0 }

    func color(knots: Double) -> RGB {
        guard let first = stops.first, let last = stops.last else { return RGB(red: 0, green: 0, blue: 0) }
        if !(knots > first.knots) { return RGB(red: first.red, green: first.green, blue: first.blue) }
        if knots >= last.knots { return RGB(red: last.red, green: last.green, blue: last.blue) }
        var upper = 1
        while stops[upper].knots < knots { upper += 1 }
        let a = stops[upper - 1]
        let b = stops[upper]
        let share = (knots - a.knots) / (b.knots - a.knots)
        return RGB(
            red: a.red + share * (b.red - a.red),
            green: a.green + share * (b.green - a.green),
            blue: a.blue + share * (b.blue - a.blue)
        )
    }

    /// 256 colours from 0 to `maxKnots`, as bytes, for fast drawing.
    func table(steps: Int = 256) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: steps * 3)
        for step in 0..<steps {
            let rgb = color(knots: maxKnots * Double(step) / Double(steps - 1))
            bytes[3 * step] = UInt8((rgb.red * 255).rounded())
            bytes[3 * step + 1] = UInt8((rgb.green * 255).rounded())
            bytes[3 * step + 2] = UInt8((rgb.blue * 255).rounded())
        }
        return bytes
    }
}

/// The speed map as a picture: one RGBA pixel per sample, top row north,
/// colour premultiplied by alpha, clear where there is no water.
struct CurrentRaster: Sendable, Equatable {
    let width: Int
    let height: Int
    let pixels: [UInt8]

    /// Paints `frame` with `detail` pixels per grid cell.
    ///
    /// With a coastline (`mask`, at the same detail) the colour runs to the
    /// real shore and stops there. Without one, the edge of the water fades
    /// out over the last half cell rather than ending in steps.
    static func make(
        frame: CurrentFrame,
        scale: CurrentColorScale = .standard,
        detail: Int = 3,
        opacity: Double = 0.82,
        mask: CurrentWaterMask? = nil
    ) -> CurrentRaster {
        let detail = max(1, detail)
        var mask = mask
        if let given = mask, given.detail != detail || given.cols != (frame.cols - 1) * detail + 1 || given.rows != (frame.rows - 1) * detail + 1 {
            mask = nil
        }
        let frame = mask == nil ? frame : frame.filled(passes: 3)
        let width = (frame.cols - 1) * detail + 1
        let height = (frame.rows - 1) * detail + 1
        let table = scale.table()
        let steps = table.count / 3
        let perKnot = Double(steps - 1) / max(scale.maxKnots, 0.001)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let cols = frame.cols
        let u = frame.u
        let v = frame.v
        for py in 0..<height {
            let y = Double(height - 1 - py) / Double(detail)
            let row = min(Int(y), frame.rows - 2)
            let fy = y - Double(row)
            for px in 0..<width {
                let x = Double(px) / Double(detail)
                let col = min(Int(x), cols - 2)
                let fx = x - Double(col)
                let base = row * cols + col
                var sumU = 0.0
                var sumV = 0.0
                var total = 0.0
                // The four grid points around the pixel, each counted only if it has water.
                var index = base
                var weight = (1 - fy) * (1 - fx)
                for corner in 0..<4 {
                    switch corner {
                    case 1: index = base + 1; weight = (1 - fy) * fx
                    case 2: index = base + cols; weight = fy * (1 - fx)
                    case 3: index = base + cols + 1; weight = fy * fx
                    default: break
                    }
                    let a = u[index]
                    let b = v[index]
                    if weight <= 0 || a.isNaN || b.isNaN { continue }
                    sumU += weight * Double(a)
                    sumV += weight * Double(b)
                    total += weight
                }
                let alpha: Double
                if let mask {
                    // The coastline decides where the colour is; the model only has to have a value nearby.
                    if total <= 0 || !mask.isWater(row: height - 1 - py, col: px) { continue }
                    alpha = opacity
                } else {
                    // Full strength from half the weight on water; fading to nothing at a quarter.
                    if total < 0.25 { continue }
                    alpha = opacity * min(1.0, (total - 0.25) / 0.25)
                }
                let knots = (sumU * sumU + sumV * sumV).squareRoot() / total * CurrentVector.knotsPerMetrePerSecond
                let step = min(steps - 1, max(0, Int((knots * perKnot).rounded())))
                let offset = (py * width + px) * 4
                pixels[offset] = UInt8((Double(table[3 * step]) * alpha).rounded())
                pixels[offset + 1] = UInt8((Double(table[3 * step + 1]) * alpha).rounded())
                pixels[offset + 2] = UInt8((Double(table[3 * step + 2]) * alpha).rounded())
                pixels[offset + 3] = UInt8((255 * alpha).rounded())
            }
        }
        return CurrentRaster(width: width, height: height, pixels: pixels)
    }
}
