import Foundation
import Testing
@testable import CurrentCoach

@Suite("TideMeasurement Tests")
struct TideMeasurementTests {

    @Test("Speed conversions from m/min")
    func speedConversions() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 180,
            speedMetersPerMinute: 6.6,
            fromDirection: 207,
            latitude: 39.5,
            longitude: 2.7,
            isValid: true
        )

        // 6.6 m/min = 396 m/hr = 396/1852 kts ≈ 0.2138
        #expect(abs(m.speedKnots - 0.2138) < 0.001)

        // 6.6 m/min = 660 cm / 60s = 11 cm/s
        #expect(abs(m.speedCmPerSecond - 11.0) < 0.001)

        // 6.6 m/min = 6.6/60 = 0.11 m/s
        #expect(abs(m.speedMetersPerSecond - 0.11) < 0.001)
    }

    @Test("Duration formatting")
    func durationFormatting() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 167,
            speedMetersPerMinute: 5.0,
            fromDirection: 180,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(m.durationFormatted == "02:47")
    }

    @Test("Flow direction is opposite of from")
    func flowDirection() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 60,
            speedMetersPerMinute: 1.0,
            fromDirection: 207,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(abs(m.flowDirection - 27.0) < 0.001)
    }

    @Test("Flow direction wraps around 360")
    func flowDirectionWrap() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 60,
            speedMetersPerMinute: 1.0,
            fromDirection: 10,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(abs(m.flowDirection - 190.0) < 0.001)
    }

    @Test("Invalid measurement with zero speed")
    func invalidMeasurement() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 3,
            speedMetersPerMinute: 0.0,
            fromDirection: 0,
            latitude: 0,
            longitude: 0,
            isValid: false
        )
        #expect(!m.isValid)
        #expect(m.speedKnots == 0)
    }
}

@Suite("Bearing Calculation Tests")
@MainActor
struct BearingCalculationTests {

    @Test("Normalize angle positive")
    func normalizePositive() {
        #expect(MeasureViewModel.normalizeAngle(370) == 10)
    }

    @Test("Normalize angle negative")
    func normalizeNegative() {
        #expect(MeasureViewModel.normalizeAngle(-10) == 350)
    }

    @Test("Normalize angle zero")
    func normalizeZero() {
        #expect(MeasureViewModel.normalizeAngle(0) == 0)
    }

    @Test("Normalize angle 360")
    func normalize360() {
        #expect(MeasureViewModel.normalizeAngle(360) == 0)
    }
}
