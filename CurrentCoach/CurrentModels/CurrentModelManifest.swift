import Foundation

/// What the server publishes for a venue: which current models exist and
/// for which days each has a map. Written by `currentmodel.publish` in
/// `model/`, which also describes the format.
struct CurrentModelManifest: Codable, Sendable, Equatable {
    struct Venue: Codable, Sendable, Equatable, Identifiable {
        let id: String
        let name: String
        let timeZone: String?
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
