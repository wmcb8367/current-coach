import Foundation
import CoreLocation

@MainActor
@Observable
final class MeasureViewModel {
    let locationService: LocationService
    let store: MeasurementStore

    var isMeasuring = false
    var currentDistance: Double = 0
    var elapsedTime: TimeInterval = 0
    var currentSpeed: Double = 0
    var currentDirection: Double = 0
    var recentSpeeds: [Double] = []
    var confidence: Double = 0

    private var locations: [CLLocation] = []
    private var startTime: Date?
    private var timer: Timer?
    private var accuracyReadings: [Double] = []

    init(locationService: LocationService, store: MeasurementStore) {
        self.locationService = locationService
        self.store = store
    }

    var isGPSReady: Bool {
        locationService.isAuthorized && locationService.accuracyIsGood
    }

    var statusLabel: String {
        if !locationService.isAuthorized { return "No GPS" }
        if !locationService.accuracyIsGood { return "Acquiring GPS" }
        return "GPS ready"
    }

    var speedKnots: Double { currentSpeed * 60.0 / 1852.0 }
    var speedCmPerSecond: Double { currentSpeed * 100.0 / 60.0 }
    var speedMetersPerSecond: Double { currentSpeed / 60.0 }

    var elapsedFormatted: String {
        let total = Int(elapsedTime)
        let minutes = total / 60
        let seconds = total % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }

    var distanceFormatted: String {
        if currentDistance < 1 { return "0m" }
        return String(format: "%.0fm", currentDistance)
    }

    func start() {
        guard let location = locationService.currentLocation else { return }
        locationService.enableBackgroundUpdates()
        isMeasuring = true
        startTime = Date()
        locations = [location]
        currentDistance = 0
        elapsedTime = 0
        currentSpeed = 0
        currentDirection = 0
        recentSpeeds = []
        confidence = 0
        accuracyReadings = []

        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.update()
            }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        isMeasuring = false
        locationService.disableBackgroundUpdates()

        guard let startTime else { return }
        let duration = Date().timeIntervalSince(startTime)
        let isValid = currentDistance > 1 && duration > 3

        let startLocation = locations.first ?? locationService.currentLocation
        let measurement = TideMeasurement(
            id: UUID(),
            timestamp: startTime,
            duration: duration,
            speedMetersPerMinute: currentSpeed,
            fromDirection: currentDirection,
            latitude: startLocation?.coordinate.latitude ?? 0,
            longitude: startLocation?.coordinate.longitude ?? 0,
            isValid: isValid
        )

        store.add(measurement)
        self.startTime = nil
    }

    private func update() {
        guard let startTime,
              let currentLocation = locationService.currentLocation else { return }

        locations.append(currentLocation)

        // Total distance along GPS track
        var totalDistance: Double = 0
        for i in 1..<locations.count {
            totalDistance += locations[i].distance(from: locations[i - 1])
        }
        currentDistance = totalDistance

        // Elapsed time
        elapsedTime = Date().timeIntervalSince(startTime)

        // Speed in m/min
        if elapsedTime > 0 {
            currentSpeed = (currentDistance / elapsedTime) * 60.0
        }

        // Bearing: flow direction from first to last location
        if let first = locations.first, currentLocation.distance(from: first) > 1 {
            let flowBearing = Self.bearing(from: first, to: currentLocation)
            currentDirection = Self.normalizeAngle(flowBearing + 180.0)
        }

        // Track recent speeds (last 10 seconds)
        recentSpeeds.append(currentSpeed)
        if recentSpeeds.count > 10 {
            recentSpeeds.removeFirst()
        }

        // Confidence calculation
        let acc = currentLocation.horizontalAccuracy
        if acc >= 0 { accuracyReadings.append(acc) }
        confidence = Self.computeConfidence(
            elapsedTime: elapsedTime,
            accuracyReadings: accuracyReadings,
            recentSpeeds: recentSpeeds,
            distance: currentDistance
        )
    }

    /// Confidence 0-100 based on time, GPS accuracy, and measurement stability.
    /// More data dilutes GPS noise → higher confidence.
    static func computeConfidence(
        elapsedTime: TimeInterval,
        accuracyReadings: [Double],
        recentSpeeds: [Double],
        distance: Double
    ) -> Double {
        // Time factor: ramps from 0 → 100% over 120 seconds (asymptotic)
        // At 10s: 24%, 30s: 53%, 60s: 78%, 120s: 95%
        let timeFactor = 1.0 - exp(-elapsedTime / 50.0)

        // Accuracy factor: average GPS accuracy → confidence
        // <2m: 100%, 5m: 85%, 10m: 60%, 20m: 20%, >30m: 0%
        let avgAccuracy: Double
        if accuracyReadings.isEmpty {
            avgAccuracy = 30
        } else {
            avgAccuracy = accuracyReadings.reduce(0, +) / Double(accuracyReadings.count)
        }
        let accuracyFactor = max(0, 1.0 - pow(avgAccuracy / 25.0, 1.5))

        // Stability factor: low variance in recent speeds = higher confidence
        let stabilityFactor: Double
        if recentSpeeds.count >= 3 {
            let mean = recentSpeeds.reduce(0, +) / Double(recentSpeeds.count)
            let variance = recentSpeeds.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Double(recentSpeeds.count)
            let cv = mean > 0.1 ? sqrt(variance) / mean : 1.0
            stabilityFactor = max(0, 1.0 - min(cv, 1.0))
        } else {
            stabilityFactor = 0.2
        }

        // Distance factor: need meaningful displacement vs GPS noise floor
        let distanceFactor = min(1.0, distance / 10.0)

        // Weighted combination
        let raw = timeFactor * 0.35 + accuracyFactor * 0.30 + stabilityFactor * 0.20 + distanceFactor * 0.15
        return min(100, max(0, raw * 100))
    }

    // MARK: - Geodesy

    static func bearing(from: CLLocation, to: CLLocation) -> Double {
        let lat1 = from.coordinate.latitude * .pi / 180.0
        let lon1 = from.coordinate.longitude * .pi / 180.0
        let lat2 = to.coordinate.latitude * .pi / 180.0
        let lon2 = to.coordinate.longitude * .pi / 180.0

        let dLon = lon2 - lon1
        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)
        let bearing = atan2(y, x) * 180.0 / .pi
        return normalizeAngle(bearing)
    }

    static func normalizeAngle(_ angle: Double) -> Double {
        var a = angle.truncatingRemainder(dividingBy: 360.0)
        if a < 0 { a += 360.0 }
        return a
    }
}
