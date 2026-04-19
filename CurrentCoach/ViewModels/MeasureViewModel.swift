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

    private var startLocation: CLLocation?
    private var startTime: Date?
    private var timer: Timer?
    private var accuracyReadings: [Double] = []
    private var startAccuracy: Double = -1

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
        startLocation = location
        startAccuracy = location.horizontalAccuracy >= 0 ? location.horizontalAccuracy : 30
        currentDistance = 0
        elapsedTime = 0
        currentSpeed = 0
        currentDirection = 0
        recentSpeeds = []
        confidence = 0
        accuracyReadings = [startAccuracy]

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

        let endAccuracyRaw = locationService.currentLocation?.horizontalAccuracy ?? -1
        let endAccuracy = endAccuracyRaw >= 0 ? endAccuracyRaw : (accuracyReadings.last ?? 30)

        let finalConfidence = Self.computeFinalConfidence(
            elapsedTime: duration,
            accuracyReadings: accuracyReadings,
            startAccuracy: startAccuracy,
            endAccuracy: endAccuracy,
            finalDistance: currentDistance
        )

        let anchor = startLocation ?? locationService.currentLocation
        let measurement = TideMeasurement(
            id: UUID(),
            timestamp: startTime,
            duration: duration,
            speedMetersPerMinute: currentSpeed,
            fromDirection: currentDirection,
            latitude: anchor?.coordinate.latitude ?? 0,
            longitude: anchor?.coordinate.longitude ?? 0,
            isValid: isValid,
            confidence: finalConfidence
        )

        store.add(measurement)
        self.startTime = nil
        self.startLocation = nil
    }

    private func update() {
        guard let startTime,
              let startLocation,
              let currentLocation = locationService.currentLocation else { return }

        // Displacement from the first ping to the current location.
        // Not aggregate path length — if the user returns to the start, this goes to zero.
        currentDistance = currentLocation.distance(from: startLocation)

        elapsedTime = Date().timeIntervalSince(startTime)

        if elapsedTime > 0 {
            currentSpeed = (currentDistance / elapsedTime) * 60.0
        }

        // Bearing: flow direction from first to current location
        if currentDistance > 1 {
            let flowBearing = Self.bearing(from: startLocation, to: currentLocation)
            currentDirection = Self.normalizeAngle(flowBearing + 180.0)
        }

        // Track recent speeds (last 10 seconds)
        recentSpeeds.append(currentSpeed)
        if recentSpeeds.count > 10 {
            recentSpeeds.removeFirst()
        }

        // Confidence calculation — live score reflects only time + GPS quality.
        // Boat motion during the recording is independent of drifter motion, so we
        // can't know the true start→end displacement until Stop is pressed.
        let acc = currentLocation.horizontalAccuracy
        if acc >= 0 { accuracyReadings.append(acc) }
        confidence = Self.computeLiveConfidence(
            elapsedTime: elapsedTime,
            accuracyReadings: accuracyReadings
        )
    }

    /// Pessimistic GPS accuracy in meters: mean of the worst third of readings.
    /// A single bad ping should still count, so we don't use the plain mean.
    static func pessimisticAccuracy(_ readings: [Double]) -> Double {
        let valid = readings.filter { $0 >= 0 }
        guard !valid.isEmpty else { return 30 }
        let sorted = valid.sorted()
        let tailStart = (sorted.count * 2) / 3
        let tail = sorted[tailStart..<sorted.count]
        return tail.reduce(0, +) / Double(tail.count)
    }

    /// 0-100 factor: 1.0 at ≤2m, ~0.5 at 10m, ~0 at ≥30m.
    static func accuracyFactor(meters: Double) -> Double {
        max(0, 1.0 - pow(meters / 25.0, 1.5))
    }

    /// 0-100 factor: asymptotic ramp, 24% at 10s, 55% at 30s, 78% at 60s, 95% at 120s.
    /// Captures how many independent GPS samples have been integrated.
    static func timeFactor(seconds: TimeInterval) -> Double {
        1.0 - exp(-seconds / 50.0)
    }

    /// Signal-to-noise factor: how large is the drifter's displacement vs. GPS noise?
    /// Need ≥ 2× GPS accuracy for half credit; 4× for full credit; floored at 5m so
    /// a pristine 1m-accuracy drift still needs meaningful motion to count.
    static func snrFactor(distance: Double, accuracy: Double) -> Double {
        let noiseFloor = max(5.0, 2.0 * accuracy)
        return max(0, min(1.0, (distance - noiseFloor) / (2.0 * noiseFloor)))
    }

    /// Live 0-100 confidence shown during recording. Only time and GPS quality —
    /// distance is intentionally excluded because boat motion is independent of
    /// the drifter and the true start→end displacement isn't known until Stop.
    static func computeLiveConfidence(
        elapsedTime: TimeInterval,
        accuracyReadings: [Double]
    ) -> Double {
        let t = timeFactor(seconds: elapsedTime)
        let a = accuracyFactor(meters: pessimisticAccuracy(accuracyReadings))
        // Weight time and accuracy roughly equally; both must be decent for a
        // high live score. Geometric mean penalizes either being weak.
        let combined = sqrt(t * a)
        return min(100, max(0, combined * 100))
    }

    /// Final 0-100 confidence computed at Stop, using the actual start→end
    /// displacement that the tidal-current speed/direction were derived from.
    /// Displacement uncertainty is dominated by the worst endpoint, so the SNR
    /// denominator uses max(startAccuracy, endAccuracy). A separate start-ping
    /// factor throttles the whole score when the anchor ping was weak — the
    /// entire measurement pivots on that first fix.
    static func computeFinalConfidence(
        elapsedTime: TimeInterval,
        accuracyReadings: [Double],
        startAccuracy: Double,
        endAccuracy: Double,
        finalDistance: Double
    ) -> Double {
        let t = timeFactor(seconds: elapsedTime)
        let aOverall = accuracyFactor(meters: pessimisticAccuracy(accuracyReadings))
        let endpointNoise = max(startAccuracy, endAccuracy)
        let s = snrFactor(distance: finalDistance, accuracy: endpointNoise)
        let startFactor = accuracyFactor(meters: startAccuracy)
        // Geometric mean of all four — any weak link drags the score.
        let combined = pow(t * aOverall * s * startFactor, 1.0 / 4.0)
        return min(100, max(0, combined * 100))
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
