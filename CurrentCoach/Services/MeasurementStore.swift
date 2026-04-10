import Foundation

@MainActor
@Observable
final class MeasurementStore {
    private(set) var measurements: [TideMeasurement] = []

    private let fileURL: URL = {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appending(path: "measurements.json")
    }()

    init() {
        load()
    }

    func add(_ measurement: TideMeasurement) {
        measurements.insert(measurement, at: 0)
        save()
    }

    func delete(_ ids: Set<UUID>) {
        measurements.removeAll { ids.contains($0.id) }
        save()
    }

    func delete(at offsets: IndexSet, in section: [TideMeasurement]) {
        let idsToDelete = offsets.map { section[$0].id }
        measurements.removeAll { idsToDelete.contains($0.id) }
        save()
    }

    /// Measurements grouped by date, sorted newest first
    var groupedByDate: [(date: String, measurements: [TideMeasurement])] {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .none

        let grouped = Dictionary(grouping: measurements) { m in
            formatter.string(from: m.timestamp)
        }

        return grouped
            .map { (date: $0.key, measurements: $0.value.sorted { $0.timestamp > $1.timestamp }) }
            .sorted { first, second in
                guard let d1 = first.measurements.first?.timestamp,
                      let d2 = second.measurements.first?.timestamp else { return false }
                return d1 > d2
            }
    }

    func measurements(for filter: MapTimeFilter, date: Date? = nil) -> [TideMeasurement] {
        let now = Date()
        switch filter {
        case .lastDay:
            let cutoff = now.addingTimeInterval(-86400)
            return measurements.filter { $0.timestamp >= cutoff }
        case .lastHour:
            let cutoff = now.addingTimeInterval(-3600)
            return measurements.filter { $0.timestamp >= cutoff }
        case .specificDate:
            guard let date else { return [] }
            let calendar = Calendar.current
            return measurements.filter { calendar.isDate($0.timestamp, inSameDayAs: date) }
        }
    }

    // MARK: - Persistence

    private func save() {
        do {
            let data = try JSONEncoder().encode(measurements)
            try data.write(to: fileURL, options: .atomic)
        } catch {
            print("Failed to save measurements: \(error)")
        }
    }

    private func load() {
        guard FileManager.default.fileExists(atPath: fileURL.path()) else { return }
        do {
            let data = try Data(contentsOf: fileURL)
            measurements = try JSONDecoder().decode([TideMeasurement].self, from: data)
        } catch {
            print("Failed to load measurements: \(error)")
        }
    }
}

enum MapTimeFilter: Sendable {
    case lastDay
    case lastHour
    case specificDate
}
