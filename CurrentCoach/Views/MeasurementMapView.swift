import SwiftUI
import MapKit

enum MapDisplayStyle: String, CaseIterable {
    case standard = "Standard"
    case satellite = "Satellite"
    case charts = "Charts"
}

struct MeasurementMapView: View {
    let store: MeasurementStore
    let locationService: LocationService

    @State private var filter: MapTimeFilter = .lastDay
    @State private var selectedDate: Date = Date()
    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var mapStyle: MapDisplayStyle = .standard
    @State private var nauticalRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 39.5, longitude: 2.7),
        span: MKCoordinateSpan(latitudeDelta: 0.1, longitudeDelta: 0.1)
    )

    private var filteredMeasurements: [TideMeasurement] {
        store.measurements(for: filter, date: selectedDate)
    }

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                if mapStyle == .charts {
                    // Nautical chart mode with OpenSeaMap overlay
                    NauticalMapView(
                        measurements: filteredMeasurements,
                        showChartOverlay: true,
                        region: $nauticalRegion
                    )
                } else {
                    // Standard SwiftUI Map
                    Map(position: $cameraPosition) {
                        UserAnnotation()

                        ForEach(filteredMeasurements) { measurement in
                            Annotation("", coordinate: measurement.coordinate, anchor: .center) {
                                CurrentArrowView(measurement: measurement)
                            }
                        }
                    }
                    .mapStyle(mapStyle == .satellite ? .imagery(elevation: .flat) : .standard)
                    .mapControls {
                        MapUserLocationButton()
                        MapCompass()
                        MapScaleView()
                    }
                }

                // Controls bar
                VStack(spacing: 12) {
                    // Map style picker
                    Picker("Map Style", selection: $mapStyle) {
                        ForEach(MapDisplayStyle.allCases, id: \.self) { style in
                            Text(style.rawValue).tag(style)
                        }
                    }
                    .pickerStyle(.segmented)

                    // Time filter
                    HStack(spacing: 0) {
                        FilterButton(title: "Last Day", isSelected: filter == .lastDay) {
                            filter = .lastDay
                        }

                        FilterButton(title: "Last\nhour", isSelected: filter == .lastHour) {
                            filter = .lastHour
                        }

                        DatePicker("", selection: $selectedDate, displayedComponents: .date)
                            .labelsHidden()
                            .tint(NT.accentTeal)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .stroke(filter == .specificDate ? NT.accentAmber : .clear, lineWidth: 1)
                            )
                            .onChange(of: selectedDate) {
                                filter = .specificDate
                            }
                    }
                }
                .padding()
                .background(NT.bgCard)
            }
        }
        .onChange(of: filteredMeasurements) {
            updateCamera()
        }
        .onChange(of: mapStyle) {
            if mapStyle == .charts {
                syncToNauticalRegion()
            }
        }
        .onAppear {
            updateCamera()
        }
    }

    private func syncToNauticalRegion() {
        let measurements = filteredMeasurements
        if measurements.isEmpty {
            if let loc = locationService.currentLocation {
                nauticalRegion = MKCoordinateRegion(
                    center: loc.coordinate,
                    span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
                )
            }
            return
        }
        nauticalRegion = regionForMeasurements(measurements)
    }

    private func updateCamera() {
        let measurements = filteredMeasurements
        if measurements.isEmpty {
            if let loc = locationService.currentLocation {
                cameraPosition = .region(MKCoordinateRegion(
                    center: loc.coordinate,
                    span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
                ))
            }
            return
        }
        cameraPosition = .region(regionForMeasurements(measurements))
    }

    private func regionForMeasurements(_ measurements: [TideMeasurement]) -> MKCoordinateRegion {
        let lats = measurements.map(\.latitude)
        let lons = measurements.map(\.longitude)
        guard let minLat = lats.min(), let maxLat = lats.max(),
              let minLon = lons.min(), let maxLon = lons.max() else {
            return MKCoordinateRegion()
        }
        return MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: (minLat + maxLat) / 2, longitude: (minLon + maxLon) / 2),
            span: MKCoordinateSpan(latitudeDelta: max(0.01, (maxLat - minLat) * 1.5), longitudeDelta: max(0.01, (maxLon - minLon) * 1.5))
        )
    }
}

// MARK: - Arrow Annotation (for SwiftUI Map modes)

private struct CurrentArrowView: View {
    let measurement: TideMeasurement

    private var arrowColor: Color {
        let speed = measurement.speedMetersPerMinute
        if speed < 3 { return NT.accentTeal }
        if speed < 6 { return NT.accentAmber }
        return NT.accentCoral
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Image(systemName: "location.north.fill")
                .font(.title)
                .fontWeight(.bold)
                .foregroundStyle(arrowColor)
                .rotationEffect(.degrees(measurement.flowDirection))

            VStack(alignment: .leading, spacing: 0) {
                Text(measurement.timeFormatted)
                    .font(.caption2)
                    .fontWeight(.bold)
                    .foregroundStyle(NT.accentAmber)
                Text(String(format: "%.1fm/min", measurement.speedMetersPerMinute))
                    .font(.caption2)
                    .foregroundStyle(NT.textPrimary)
                Text(String(format: "From:%.0f°", measurement.fromDirection))
                    .font(.caption2)
                    .foregroundStyle(NT.textPrimary)
            }
            .padding(4)
            .background(
                RoundedRectangle(cornerRadius: 4)
                    .fill(NT.bgCard.opacity(0.9))
                    .overlay(
                        RoundedRectangle(cornerRadius: 4)
                            .stroke(NT.accentTeal.opacity(0.3), lineWidth: 1)
                    )
            )
        }
    }
}

// MARK: - Filter Button

private struct FilterButton: View {
    let title: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(isSelected ? NT.accentTeal : NT.textSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
        }
    }
}
