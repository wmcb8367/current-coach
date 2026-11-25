import Foundation

/// What the server publishes for a venue: which current models exist and
/// for which days each has a map. Written by `currentmodel.publish` in
/// `model/`, which also describes the format.
struct CurrentModelManifest: Codable, Sendable, Equatable {
    /// The venue's coastline, finer than the map: see `CurrentWaterMask`.
    struct Mask: Codable, Sendable, Equatable {
        let detail: Int
        let rows: Int
        let cols: Int
        /// The bits, raw DEFLATE, base64.
        let data: String
    }

    struct Venue: Codable, Sendable, Equatable, Identifiable {
        let id: String
        let name: String
        let timeZone: String?
        let mask: Mask?
        let south: Double
        let north: Double
        let west: Double
        let east: Double
        let rows: Int
        let cols: Int

        var grid: CurrentMapGrid {
            CurrentMapGrid(south: south, north: north, west: west, east: east, rows: rows, cols: cols)
        }

        var centerLatitude: Double { 0.5 * (south + north) }
        var centerLongitude: Double { 0.5 * (west + east) }
    }

    struct Day: Codable, Sendable, Equatable {
        /// UTC day, YYYY-MM-DD.
        let day: String
        /// "forecast": built before the day began. "analysis": built during it.
        let kind: String
        /// When it was built, seconds since 1970.
        let issued: Double
        let bytes: Int
    }

    struct Model: Codable, Sendable, Equatable, Identifiable {
        let id: String
        let name: String
        let summary: String
        let factors: [String]
        let days: [Day]

        func entry(for day: String) -> Day? {
            days.first { $0.day == day }
        }
    }

    let version: Int
    let venue: Venue
    let frameSeconds: Int
    let framesPerDay: Int
    let updated: Double
    let defaultModel: String
    let models: [Model]
    let attribution: [String]

    enum CodingKeys: String, CodingKey {
        case version, venue, frameSeconds, framesPerDay, updated, models, attribution
        case defaultModel = "default"
    }

    func model(_ id: String) -> Model? {
        models.first { $0.id == id }
    }

    /// Every day that at least one model has a map for, oldest first.
    var days: [String] {
        Array(Set(models.flatMap { $0.days.map(\.day) })).sorted()
    }
}

/// The list of venues that have been published.
struct CurrentModelIndex: Codable, Sendable, Equatable {
    let version: Int
    let venues: [CurrentModelManifest.Venue]
}

/// A regular grid of latitude and longitude. Point (row 0, column 0) is the
/// south-west corner; the last row and column lie on the north and east edges.
struct CurrentMapGrid: Sendable, Equatable {
    let south: Double
    let north: Double
    let west: Double
    let east: Double
    let rows: Int
    let cols: Int

    func contains(latitude: Double, longitude: Double) -> Bool {
        latitude >= south && latitude <= north && longitude >= west && longitude <= east
    }

    /// Fractional column for a longitude: 0 on the west edge, `cols - 1` on the east.
    func x(longitude: Double) -> Double {
        (longitude - west) / (east - west) * Double(cols - 1)
    }

    /// Fractional row for a latitude: 0 on the south edge, `rows - 1` on the north.
    func y(latitude: Double) -> Double {
        (latitude - south) / (north - south) * Double(rows - 1)
    }

    func longitude(x: Double) -> Double {
        west + x / Double(cols - 1) * (east - west)
    }

    func latitude(y: Double) -> Double {
        south + y / Double(rows - 1) * (north - south)
    }

    /// Metres between neighbouring columns and between neighbouring rows.
    var metresPerCell: (x: Double, y: Double) {
        let metresPerDegree = 111_320.0
        let middle = 0.5 * (south + north) * Double.pi / 180.0
        return (
            (east - west) / Double(cols - 1) * metresPerDegree * cos(middle),
            (north - south) / Double(rows - 1) * metresPerDegree
        )
    }
}

/// Days are UTC days, named YYYY-MM-DD, because that is how the maps are filed.
enum CurrentDay {
    static let seconds: TimeInterval = 86_400

    private static var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }

    static func name(for date: Date) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        let month = parts.month ?? 1
        let day = parts.day ?? 1
        return "\(parts.year ?? 1970)-\(month < 10 ? "0" : "")\(month)-\(day < 10 ? "0" : "")\(day)"
    }

    /// 00:00 UTC on the named day, or nil if the name is not a date.
    static func start(of name: String) -> Date? {
        let parts = name.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
    }

    /// The day a moment falls in and the hours into that day.
    static func split(_ date: Date) -> (day: String, hours: Double) {
        let day = name(for: date)
        let start = self.start(of: day) ?? date
        return (day, date.timeIntervalSince(start) / 3600.0)
    }
}

/// Where the sea is, several times finer than the map, so that the colour
/// can end at the shore and not at the edge of a map cell. One bit per
/// point; rows run from the south, each padded to a whole byte, with the
/// most significant bit first.
struct CurrentWaterMask: Sendable, Equatable {
    /// Points per map cell, each way.
    let detail: Int
    let rows: Int
    let cols: Int
    let bits: [UInt8]

    private var bytesPerRow: Int { (cols + 7) / 8 }

    init?(detail: Int, rows: Int, cols: Int, bits: [UInt8]) {
        guard detail >= 1, rows >= 2, cols >= 2, bits.count == rows * ((cols + 7) / 8) else { return nil }
        self.detail = detail
        self.rows = rows
        self.cols = cols
        self.bits = bits
    }

    /// From the manifest. `inflate` undoes raw DEFLATE.
    init?(entry: CurrentModelManifest.Mask, inflate: (Data) throws -> Data) {
        guard let packed = Data(base64Encoded: entry.data), let bits = try? inflate(packed) else { return nil }
        self.init(detail: entry.detail, rows: entry.rows, cols: entry.cols, bits: [UInt8](bits))
    }

    /// Whether the point (row from the south, column from the west) is water.
    func isWater(row: Int, col: Int) -> Bool {
        guard row >= 0, row < rows, col >= 0, col < cols else { return false }
        return bits[row * bytesPerRow + col / 8] & (0x80 >> UInt8(col % 8)) != 0
    }

    /// Whether a place given in the map's grid coordinates (columns from
    /// the west edge, rows from the south edge) is water: the nearest
    /// point of the coastline decides.
    func isWater(x: Double, y: Double) -> Bool {
        let col = (x * Double(detail)).rounded()
        let row = (y * Double(detail)).rounded()
        guard col >= 0, row >= 0, col < Double(cols), row < Double(rows) else { return false }
        return isWater(row: Int(row), col: Int(col))
    }

    /// Whether this coastline belongs to a map of this many rows and columns.
    func fits(rows mapRows: Int, cols mapCols: Int) -> Bool {
        rows == (mapRows - 1) * detail + 1 && cols == (mapCols - 1) * detail + 1
    }
}
