import Foundation
import CoreLocation

struct VectorFieldSample: Identifiable, Sendable {
    let id: Int
    let latitude: Double
    let longitude: Double
    let speedMetersPerMinute: Double
    /// Direction the current flows TOWARD, degrees clockwise from north.
    let flowDirectionDegrees: Double
    /// 0...1 — low when no measurements are close to this grid cell.
    let confidence: Double

    /// Speed in knots (1 knot = 1852 m/hr = 30.87 m/min)
    var speedKnots: Double { speedMetersPerMinute * 60.0 / 1852.0 }
}

struct VectorFieldResult: Sendable {
    let samples: [VectorFieldSample]
    let minLat: Double
    let maxLat: Double
    let minLon: Double
    let maxLon: Double
}

enum VectorFieldInterpolator {
    private static let minSupportRadiusMeters = 400.0
    private static let maxSupportRadiusMeters = 5_000.0

    /// IDW over a lat/lon grid. Direction is averaged as unit vectors to avoid
    /// the 0°/360° wrap problem. Confidence falls off when the nearest
    /// contributing measurement is farther than `supportRadiusMeters`.
    static func compute(
        measurements: [TideMeasurement],
        gridSize: Int = 18,
        paddingFraction: Double = 0.12,
        power: Double = 2.0,
        supportRadiusMeters: Double? = nil
    ) -> VectorFieldResult? {
        let valid = measurements.filter { $0.isValid }
        guard valid.count >= 2 else { return nil }
        let resolvedSupportRadiusMeters = supportRadiusMeters ?? adaptiveSupportRadiusMeters(for: valid)

        let lats = valid.map(\.latitude)
        let lons = valid.map(\.longitude)
        guard let rawMinLat = lats.min(), let rawMaxLat = lats.max(),
              let rawMinLon = lons.min(), let rawMaxLon = lons.max() else { return nil }

        let latSpan = max(rawMaxLat - rawMinLat, 0.002)
        let lonSpan = max(rawMaxLon - rawMinLon, 0.002)
        let padLat = latSpan * paddingFraction
        let padLon = lonSpan * paddingFraction
        let minLat = rawMinLat - padLat
        let maxLat = rawMaxLat + padLat
        let minLon = rawMinLon - padLon
        let maxLon = rawMaxLon + padLon

        let steps = max(gridSize, 2)
        let latStep = (maxLat - minLat) / Double(steps - 1)
        let lonStep = (maxLon - minLon) / Double(steps - 1)

        var samples: [VectorFieldSample] = []
        samples.reserveCapacity(steps * steps)

        for j in 0..<steps {
            for i in 0..<steps {
                let lat = minLat + latStep * Double(j)
                let lon = minLon + lonStep * Double(i)
                let gridLocation = CLLocation(latitude: lat, longitude: lon)

                var weightSum = 0.0
                var speedWeighted = 0.0
                var uWeighted = 0.0
                var vWeighted = 0.0
                var nearest = Double.infinity

                for m in valid {
                    let mLoc = CLLocation(latitude: m.latitude, longitude: m.longitude)
                    let distanceMeters = max(gridLocation.distance(from: mLoc), 1.0)
                    if distanceMeters < nearest { nearest = distanceMeters }
                    let weight = 1.0 / pow(distanceMeters, power)
                    weightSum += weight
                    speedWeighted += weight * m.speedMetersPerMinute
                    let radians = m.flowDirection * .pi / 180.0
                    uWeighted += weight * sin(radians) // east component
                    vWeighted += weight * cos(radians) // north component
                }

                guard weightSum > 0 else { continue }
                let speed = speedWeighted / weightSum
                let u = uWeighted / weightSum
                let v = vWeighted / weightSum
                var direction = atan2(u, v) * 180.0 / .pi
                if direction < 0 { direction += 360 }

                let falloff = min(nearest / resolvedSupportRadiusMeters, 1.0)
                let confidence = max(0.0, 1.0 - falloff)
                if confidence < 0.05 { continue }

                samples.append(VectorFieldSample(
                    id: j * steps + i,
                    latitude: lat,
                    longitude: lon,
                    speedMetersPerMinute: speed,
                    flowDirectionDegrees: direction,
                    confidence: confidence
                ))
            }
        }

        return VectorFieldResult(
            samples: samples,
            minLat: minLat, maxLat: maxLat, minLon: minLon, maxLon: maxLon
        )
    }

    /// Uses the visible measurement spacing to keep dense sessions local while
    /// still bridging wider sparse-measurement gaps across the venue.
    private static func adaptiveSupportRadiusMeters(for measurements: [TideMeasurement]) -> Double {
        guard measurements.count >= 2 else { return minSupportRadiusMeters }

        let nearestNeighborDistances = measurements.enumerated().compactMap { index, measurement -> Double? in
            let location = CLLocation(latitude: measurement.latitude, longitude: measurement.longitude)
            var nearest = Double.infinity
            for (candidateIndex, candidate) in measurements.enumerated() where candidateIndex != index {
                let candidateLocation = CLLocation(latitude: candidate.latitude, longitude: candidate.longitude)
                nearest = min(nearest, location.distance(from: candidateLocation))
            }
            return nearest.isFinite ? nearest : nil
        }.sorted()

        guard !nearestNeighborDistances.isEmpty else { return minSupportRadiusMeters }
        let medianSpacing = nearestNeighborDistances[nearestNeighborDistances.count / 2]
        return min(maxSupportRadiusMeters, max(minSupportRadiusMeters, medianSpacing * 2.5))
    }
}
