import SwiftUI

struct MeasurementListView: View {
    let store: MeasurementStore
    var onSelectMeasurement: ((TideMeasurement) -> Void)? = nil
    @State private var selection = Set<UUID>()

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                // Header
                HStack(spacing: 12) {
                    M2XLogo(height: 22)
                    Rectangle()
                        .fill(NT.borderSubtle)
                        .frame(width: 1, height: 18)
                    Text("Measurements")
                        .eyebrow(NT.textSecondary)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 12)

                // Toolbar
                HStack(spacing: 12) {
                    Button(selection.count == store.measurements.count ? "Deselect All" : "Select All") {
                        if selection.count == store.measurements.count {
                            selection.removeAll()
                        } else {
                            selection = Set(store.measurements.map(\.id))
                        }
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(NT.accentTeal)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(
                        Capsule().fill(NT.accentTeal.opacity(0.15))
                    )
                    .overlay(
                        Capsule().strokeBorder(NT.accentTeal.opacity(0.35), lineWidth: 1)
                    )

                    Spacer()

                    ShareLink(items: exportText()) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.title3)
                            .foregroundStyle(NT.accentTeal)
                            .padding(10)
                            .background(Circle().fill(NT.bgCard))
                            .overlay(Circle().strokeBorder(NT.borderSubtle, lineWidth: 1))
                    }
                    .disabled(selection.isEmpty && store.measurements.isEmpty)

                    Button(role: .destructive) {
                        guard !selection.isEmpty else { return }
                        store.delete(selection)
                        selection.removeAll()
                    } label: {
                        Image(systemName: "trash")
                            .font(.title3)
                            .foregroundStyle(NT.accentCoral)
                            .padding(10)
                            .background(Circle().fill(NT.bgCard))
                            .overlay(Circle().strokeBorder(NT.borderSubtle, lineWidth: 1))
                    }
                    .disabled(selection.isEmpty)
                }
                .padding(.horizontal)
                .padding(.bottom, 8)

                // List
                if store.measurements.isEmpty {
                    Spacer()
                    VStack(spacing: 14) {
                        Image(systemName: "water.waves")
                            .font(.system(size: 60))
                            .foregroundStyle(NT.textFaint)
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
                                        onToggleSelect: {
                                            if selection.contains(measurement.id) {
                                                selection.remove(measurement.id)
                                            } else {
                                                selection.insert(measurement.id)
                                            }
                                        },
                                        onOpenInMap: { onSelectMeasurement?(measurement) }
                                    )
                                    .listRowBackground(
                                        RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                                            .fill(selection.contains(measurement.id) ? NT.accentTeal.opacity(0.12) : NT.bgCard)
                                            .overlay(
                                                RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                                                    .strokeBorder(
                                                        selection.contains(measurement.id) ? NT.accentTeal.opacity(0.4) : NT.borderSubtle,
                                                        lineWidth: 1
                                                    )
                                            )
                                            .padding(.vertical, 3)
                                    )
                                    .listRowSeparator(.hidden)
                                }
                            } header: {
                                Text(section.date)
                                    .eyebrow(NT.accentTealSoft)
                                    .padding(.top, 6)
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
    let onToggleSelect: () -> Void
    let onOpenInMap: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Button(action: onToggleSelect) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.title2)
                    .foregroundStyle(isSelected ? NT.accentTeal : NT.textFaint)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)

            Button(action: onOpenInMap) {
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(measurement.timeFormatted)
                                .font(.title2.weight(.bold).monospacedDigit())
                                .foregroundStyle(NT.textPrimary)
                            Text(measurement.durationFormatted)
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(NT.textDim)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Capsule().fill(NT.bgSurface))
                        }
                        HStack(spacing: 10) {
                            Text(String(format: "%.1f m/min", measurement.speedMetersPerMinute))
                                .font(.headline)
                                .foregroundStyle(NT.accentTeal)
                            Text(String(format: "%.0f°", measurement.fromDirection))
                                .font(.headline)
                                .foregroundStyle(NT.textSecondary)
                            Circle()
                                .fill(measurement.isValid ? NT.gpsGreat : NT.accentCoral)
                                .frame(width: 8, height: 8)
                            if measurement.syncedAt != nil {
                                Image(systemName: "icloud.fill")
                                    .font(.caption)
                                    .foregroundStyle(NT.accentTealSoft)
                            }
                        }
                    }

                    Spacer()

                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(NT.textFaint)
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 8)
    }
}
