import Foundation
import CoreLocation

struct TideMeasurement: Identifiable, Codable, Hashable, Sendable {
    let id: UUID
    let timestamp: Date
    let duration: TimeInterval
    let speedMetersPerMinute: Double
    let fromDirection: Double
    let latitude: Double
    let longitude: Double
    let isValid: Bool
    var confidence: Double?
    var syncedAt: Date?

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    }

    // MARK: - Speed Conversions

    var speedKnots: Double {
        speedMetersPerMinute * 60.0 / 1852.0
    }

    var speedCmPerSecond: Double {
        speedMetersPerMinute * 100.0 / 60.0
    }

    var speedMetersPerSecond: Double {
        speedMetersPerMinute / 60.0
    }

    // MARK: - Formatting

    var durationFormatted: String {
        let total = Int(duration)
        let minutes = total / 60
        let seconds = total % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }

    var timeFormatted: String {
        Self.timeFormatter.string(from: timestamp)
    }

    /// Direction the current flows toward (opposite of fromDirection)
    var flowDirection: Double {
        let flow = fromDirection + 180.0
        return flow >= 360.0 ? flow - 360.0 : flow
    }

    // MARK: - Hashable (CLLocationCoordinate2D isn't Hashable)

    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }

    static func == (lhs: TideMeasurement, rhs: TideMeasurement) -> Bool {
        lhs.id == rhs.id
    }

    // MARK: - Private

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()
}
