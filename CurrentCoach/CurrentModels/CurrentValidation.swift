import Foundation

/// How well each model matched the readings taken on the water, as the
/// server works it out from the maps published before each day began.
struct CurrentValidationReport: Codable, Sendable, Equatable {
    struct Score: Codable, Sendable, Equatable {
        /// Readings the model had an answer for.
        let count: Int
        /// Typical size of the difference between model and reading, in knots.
        let errorKn: Double?
        /// Model speed minus measured speed on average, in knots.
        let speedBiasKn: Double?
        /// Average angle between model and reading, in degrees.
        let directionDeg: Double?
        /// The error "no current at all" would have had on the same readings.
        let noCurrentKn: Double?
    }

    struct Scope: Codable, Sendable, Equatable {
        let readings: Int
        let boats: Int
        let models: [String: Score]

        /// The model with the smallest error among those that answered at
        /// least `minimum` readings, if any did.
        func best(minimum: Int = 3) -> String? {
            models
                .filter { $0.value.count >= minimum && $0.value.errorKn != nil }
                .min { ($0.value.errorKn ?? .infinity, $0.key) < ($1.value.errorKn ?? .infinity, $1.key) }?
                .key
        }
    }

    struct Window: Codable, Sendable, Equatable {
        let id: String
        let hours: Int
        let mine: Scope
        let all: Scope
    }

    let venue: String
    let generated: String
    let windows: [Window]

    func scope(window: CurrentValidationWindow, readings: CurrentValidationReadings) -> Scope? {
        guard let match = windows.first(where: { $0.id == window.rawValue }) else { return nil }
        return readings == .mine ? match.mine : match.all
    }
}

/// The setting: judge the models on the last day or on the last week.
enum CurrentValidationWindow: String, CaseIterable, Identifiable, Sendable {
    case day = "24h"
    case week = "7d"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .day: return "Last 24 hours"
        case .week: return "Last 7 days"
        }
    }
}

/// Whose readings count: this account's, or every boat's that has synced.
enum CurrentValidationReadings: String, CaseIterable, Identifiable, Sendable {
    case mine
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .mine: return "My readings"
        case .all: return "All boats"
        }
    }
}

extension CurrentValidationReport.Score {
    /// "0.19 kn", or a dash when the model answered no readings.
    var errorText: String {
        guard let errorKn else { return "–" }
        return String(format: "%.2f kn", errorKn)
    }

    /// How much of the "no current" error the model removes, 0 to 1; below
    /// zero when the model is worse than assuming slack water.
    var gain: Double? {
        guard let errorKn, let noCurrentKn, noCurrentKn > 0 else { return nil }
        return 1 - errorKn / noCurrentKn
    }

    var detailText: String {
        guard count > 0 else { return "No readings to compare" }
        var parts = ["\(count) reading\(count == 1 ? "" : "s")"]
        if let speedBiasKn {
            let size = String(format: "%.2f kn", abs(speedBiasKn))
            parts.append(abs(speedBiasKn) < 0.005 ? "speed spot on" : "\(size) \(speedBiasKn > 0 ? "fast" : "slow")")
        }
        if let directionDeg {
            parts.append(String(format: "%.0f° off", directionDeg))
        }
        return parts.joined(separator: " · ")
    }
}
