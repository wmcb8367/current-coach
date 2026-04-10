import SwiftUI

struct MeasurementListView: View {
    let store: MeasurementStore
    @State private var selection = Set<UUID>()

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                // Toolbar
                HStack(spacing: 16) {
                    Button(selection.count == store.measurements.count ? "Deselect All" : "Select All") {
                        if selection.count == store.measurements.count {
                            selection.removeAll()
                        } else {
                            selection = Set(store.measurements.map(\.id))
                        }
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(NT.accentTeal)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(
                        Capsule()
                            .fill(NT.bgSurface)
                            .overlay(Capsule().stroke(NT.accentTeal.opacity(0.3), lineWidth: 1))
                    )

                    Spacer()

                    ShareLink(items: exportText()) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.title2)
                            .foregroundStyle(NT.accentTeal)
                    }
                    .disabled(selection.isEmpty && store.measurements.isEmpty)

                    Button(role: .destructive) {
                        guard !selection.isEmpty else { return }
                        store.delete(selection)
                        selection.removeAll()
                    } label: {
                        Image(systemName: "trash")
                            .font(.title2)
                            .foregroundStyle(NT.accentCoral)
                    }
                    .disabled(selection.isEmpty)
                }
                .padding(.horizontal)
                .padding(.vertical, 10)
                .background(NT.bgCard)

                // List
                if store.measurements.isEmpty {
                    Spacer()
                    VStack(spacing: 12) {
                        Image(systemName: "water.waves")
                            .font(.system(size: 60))
                            .foregroundStyle(NT.textDim)
                        Text("No Measurements")
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(NT.textSecondary)
                        Text("Start measuring tidal currents\non the Measure tab.")
                            .font(.subheadline)
                            .foregroundStyle(NT.textDim)
                            .multilineTextAlignment(.center)
                    }
                    Spacer()
                } else {
                    List {
                        ForEach(store.groupedByDate, id: \.date) { section in
                            Section {
                                ForEach(section.measurements) { measurement in
                                    MeasurementRow(
                                        measurement: measurement,
                                        isSelected: selection.contains(measurement.id),
                                        onToggle: {
                                            if selection.contains(measurement.id) {
                                                selection.remove(measurement.id)
                                            } else {
                                                selection.insert(measurement.id)
                                            }
                                        }
                                    )
                                    .listRowBackground(
                                        RoundedRectangle(cornerRadius: 12)
                                            .fill(selection.contains(measurement.id) ? NT.accentTeal.opacity(0.12) : NT.bgCard)
                                            .padding(.vertical, 2)
                                    )
                                    .listRowSeparator(.hidden)
                                }
                            } header: {
                                Text(section.date)
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(NT.accentAmber)
                            }
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
        }
    }

    private func exportText() -> [String] {
        let selected = selection.isEmpty ? store.measurements : store.measurements.filter { selection.contains($0.id) }
        let header = "time,duration_s,speed_m_min,from_deg,lat,lon,valid"
        let rows = selected.map { m in
            "\(m.timeFormatted),\(Int(m.duration)),\(String(format: "%.1f", m.speedMetersPerMinute)),\(Int(m.fromDirection)),\(m.latitude),\(m.longitude),\(m.isValid)"
        }
        return [([header] + rows).joined(separator: "\n")]
    }
}

// MARK: - Row

private struct MeasurementRow: View {
    let measurement: TideMeasurement
    let isSelected: Bool
    let onToggle: () -> Void

    var body: some View {
        Button(action: onToggle) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(measurement.timeFormatted)
                            .font(.title2.weight(.bold).monospacedDigit())
                            .foregroundStyle(NT.textPrimary)
                        Text("Dur:\(measurement.durationFormatted)")
                            .font(.caption)
                            .foregroundStyle(NT.textSecondary)
                    }
                    HStack(spacing: 8) {
                        Text(String(format: "%.1f m/min", measurement.speedMetersPerMinute))
                            .font(.headline)
                            .foregroundStyle(NT.accentTeal)
                        Text(String(format: "From: %.0f°", measurement.fromDirection))
                            .font(.headline)
                            .foregroundStyle(NT.textSecondary)
                        Circle()
                            .fill(measurement.isValid ? NT.gpsGreat : NT.accentCoral)
                            .frame(width: 10, height: 10)
                    }
                }

                Spacer()

                Image(systemName: "map.fill")
                    .font(.title2)
                    .foregroundStyle(NT.accentAmber)
            }
            .padding(.vertical, 4)
        }
    }
}
