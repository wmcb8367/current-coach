import Foundation

/// A current: metres per second going east (`u`) and going north (`v`).
struct CurrentVector: Sendable, Equatable {
    static let knotsPerMetrePerSecond = 3600.0 / 1852.0

    let u: Double
    let v: Double

    var speed: Double { (u * u + v * v).squareRoot() }
    var speedKnots: Double { speed * Self.knotsPerMetrePerSecond }

    /// The direction the water flows toward, degrees true, 0 to 360.
    var setDegrees: Double {
        let degrees = atan2(u, v) * 180.0 / Double.pi
        return degrees < 0 ? degrees + 360.0 : degrees
    }

    /// The direction the water comes from, as the app records readings.
    var fromDegrees: Double {
        let from = setDegrees + 180.0
        return from >= 360.0 ? from - 360.0 : from
    }
}

enum CurrentFieldError: Error, Equatable {
    case notACurrentMap
    case wrongSize
    case compressedWithoutInflater
}

/// One model's current for one UTC day, as published.
///
/// The file (little-endian):
///
///     bytes 0-3   "CCM1"
///     4-5, 6-7    rows (south to north), columns (west to east)
///     8-9         frames, hourly from 00:00 UTC
///     10-11       flags; bit 0: the rest is raw DEFLATE
///     12-15       metres per second per unit (Float32)
///     16-         per frame: east-going plane, then north-going plane,
///                 Int16, -32768 where there is no water or no answer
struct CurrentDayField: Sendable, Equatable {
    static let missing: Int16 = -32768
    static let headerBytes = 16

    let rows: Int
    let cols: Int
    let frames: Int
    /// Metres per second per unit.
    let scale: Double
    /// frames x 2 planes x rows x cols.
    let units: [Int16]

    init(rows: Int, cols: Int, frames: Int, scale: Double, units: [Int16]) {
        self.rows = rows
        self.cols = cols
        self.frames = frames
        self.scale = scale
        self.units = units
    }

    /// Reads a published file. `inflate` undoes raw DEFLATE; it is needed
    /// only for compressed files, which is how the server sends them.
    init(data: Data, inflate: ((Data) throws -> Data)? = nil) throws {
        let bytes = [UInt8](data)
        guard bytes.count >= Self.headerBytes, Array(bytes[0..<4]) == Array("CCM1".utf8) else {
            throw CurrentFieldError.notACurrentMap
        }
        func word(_ offset: Int) -> Int { Int(bytes[offset]) | Int(bytes[offset + 1]) << 8 }
        let rows = word(4)
        let cols = word(6)
        let frames = word(8)
        let flags = word(10)
        let scaleBits = UInt32(bytes[12]) | UInt32(bytes[13]) << 8 | UInt32(bytes[14]) << 16 | UInt32(bytes[15]) << 24
        var body = [UInt8](bytes[Self.headerBytes...])
        if flags & 1 == 1 {
            guard let inflate else { throw CurrentFieldError.compressedWithoutInflater }
            body = [UInt8](try inflate(Data(body)))
        }
        let count = frames * 2 * rows * cols
        guard rows >= 2, cols >= 2, frames >= 1, body.count == count * 2 else {
            throw CurrentFieldError.wrongSize
        }
        var units = [Int16](repeating: 0, count: count)
        for index in 0..<count {
            units[index] = Int16(bitPattern: UInt16(body[2 * index]) | UInt16(body[2 * index + 1]) << 8)
        }
        self.init(rows: rows, cols: cols, frames: frames, scale: Double(Float(bitPattern: scaleBits)), units: units)
    }

    /// The whole map at a time of the day, weighted between the hours either
    /// side. `hours` counts from 00:00 UTC; nil outside the file's hours.
    func frame(hours: Double) -> CurrentFrame? {
        guard hours >= 0, hours <= Double(frames - 1) else { return nil }
        let first = min(Int(hours), max(frames - 2, 0))
        let share = frames > 1 ? Float(hours - Double(first)) : 0
        let plane = rows * cols
        var u = [Float](repeating: .nan, count: plane)
        var v = [Float](repeating: .nan, count: plane)
        let scale = Float(self.scale)
        let a = first * 2 * plane
        let b = (frames > 1 ? first + 1 : first) * 2 * plane
        for index in 0..<plane {
            let u0 = units[a + index]
            let v0 = units[a + plane + index]
            let u1 = units[b + index]
            let v1 = units[b + plane + index]
            if u0 == Self.missing || v0 == Self.missing || u1 == Self.missing || v1 == Self.missing { continue }
            u[index] = ((1 - share) * Float(u0) + share * Float(u1)) * scale
            v[index] = ((1 - share) * Float(v0) + share * Float(v1)) * scale
        }
        return CurrentFrame(rows: rows, cols: cols, u: u, v: v)
    }

    /// The current at a place and a time of the day, or nil on land, off the
    /// map or outside the file's hours.
    func velocity(grid: CurrentMapGrid, latitude: Double, longitude: Double, hours: Double) -> CurrentVector? {
        frame(hours: hours)?.velocity(x: grid.x(longitude: longitude), y: grid.y(latitude: latitude))
    }
}

/// The current over the whole map at one moment. Row 0 is the south edge;
/// values are NaN where there is no water or no answer.
struct CurrentFrame: Sendable, Equatable {
    let rows: Int
    let cols: Int
    let u: [Float]
    let v: [Float]

    /// The current at fractional grid coordinates (`x` columns from the west
    /// edge, `y` rows from the south edge), weighted by distance over the
    /// neighbouring points that have water. Nil where less than half of that
    /// weight is on water.
    func velocity(x: Double, y: Double) -> CurrentVector? {
        guard x >= 0, y >= 0, x <= Double(cols - 1), y <= Double(rows - 1) else { return nil }
        let col = min(Int(x), cols - 2)
        let row = min(Int(y), rows - 2)
        let fx = x - Double(col)
        let fy = y - Double(row)
        let base = row * cols + col
        let corners: [(Int, Double)] = [
            (base, (1 - fy) * (1 - fx)),
            (base + 1, (1 - fy) * fx),
            (base + cols, fy * (1 - fx)),
            (base + cols + 1, fy * fx),
        ]
        var sumU = 0.0
        var sumV = 0.0
        var total = 0.0
        for (index, weight) in corners where weight > 0 {
            let a = u[index]
            let b = v[index]
            if a.isNaN || b.isNaN { continue }
            sumU += weight * Double(a)
            sumV += weight * Double(b)
            total += weight
        }
        guard total >= 0.5 else { return nil }
        return CurrentVector(u: sumU / total, v: sumV / total)
    }

    func velocity(grid: CurrentMapGrid, latitude: Double, longitude: Double) -> CurrentVector? {
        velocity(x: grid.x(longitude: longitude), y: grid.y(latitude: latitude))
    }

    /// The fastest current on the map, in knots.
    var maxKnots: Double {
        var fastest: Float = 0
        for index in u.indices where !u[index].isNaN && !v[index].isNaN {
            fastest = max(fastest, u[index] * u[index] + v[index] * v[index])
        }
        return Double(fastest).squareRoot() * CurrentVector.knotsPerMetrePerSecond
    }
}
