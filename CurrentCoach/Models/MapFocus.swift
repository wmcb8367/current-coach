import Foundation
import CoreLocation

struct MapFocus: Equatable, Sendable {
    let measurementId: UUID
    let latitude: Double
    let longitude: Double
    let timestamp: Date

    static let halfWindow: TimeInterval = 12 * 3600

    var startTime: Date { timestamp.addingTimeInterval(-Self.halfWindow) }
    var endTime: Date { timestamp.addingTimeInterval(Self.halfWindow) }

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    }

    init(measurement: TideMeasurement) {
        self.measurementId = measurement.id
        self.latitude = measurement.latitude
        self.longitude = measurement.longitude
        self.timestamp = measurement.timestamp
    }
}
