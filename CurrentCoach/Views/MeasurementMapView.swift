import SwiftUI
import MapKit

enum MapDisplayStyle: String, CaseIterable {
    case standard = "Standard"
    case satellite = "Satellite"
    case charts = "Charts"
}

enum LookbackPeriod: String, CaseIterable, Identifiable {
    case tenMin = "10m"
    case thirtyMin = "30m"
    case oneHour = "1h"
    case twoHour = "2h"
    case fiveHour = "5h"

    var id: String { rawValue }

    var seconds: TimeInterval {
        switch self {
        case .tenMin: return 600
        case .thirtyMin: return 1800
        case .oneHour: return 3600
        case .twoHour: return 7200
        case .fiveHour: return 18000
        }
    }
}

struct MeasurementMapView: View {
    let store: MeasurementStore
    let locationService: LocationService

    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var mapStyle: MapDisplayStyle = .standard
    @State private var nauticalRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 39.5, longitude: 2.7),
        span: MKCoordinateSpan(latitudeDelta: 0.1, longitudeDelta: 0.1)
    )

    // Time scrubber: position in last 24h (0 = now, 86400 = 24h ago)
    @State private var scrubberSecondsAgo: Double = 0
    @State private var lookback: LookbackPeriod = .oneHour
    @State private var showLookbackMenu = false

    private var referenceTime: Date {
        Date().addingTimeInterval(-scrubberSecondsAgo)
    }

    private var filteredMeasurements: [TideMeasurement] {
        let endTime = referenceTime
        let startTime = endTime.addingTimeInterval(-lookback.seconds)
        return store.measurements.filter { $0.timestamp >= startTime && $0.timestamp <= endTime }
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                if mapStyle == .charts {
                    NauticalMapView(
                        measurements: filteredMeasurements,
                        showChartOverlay: true,
                        region: $nauticalRegion
                    )
                } else {
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

                    // Time scrubber for last 24h
                    VStack(spacing: 4) {
                        HStack {
                            Text("Now")
                                .font(.caption2)
                                .foregroundStyle(NT.textDim)
                            Spacer()
                            Text(scrubberLabel)
                                .font(.caption.weight(.bold))
                                .foregroundStyle(NT.accentTeal)
                            Spacer()
                            Text("24h ago")
                                .font(.caption2)
                                .foregroundStyle(NT.textDim)
                        }
                        Slider(value: $scrubberSecondsAgo, in: 0...86400, step: 60)
                            .tint(NT.accentTeal)
                    }
                }
                .padding()
                .background(NT.bgCard)
            }

            // Lookback period selector (top-left circle)
            VStack(spacing: 0) {
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        showLookbackMenu.toggle()
                    }
                } label: {
                    Text(lookback.rawValue)
                        .font(.subheadline.weight(.bold))
                        .foregroundStyle(NT.accentTeal)
                        .frame(width: 50, height: 50)
                        .background(
                            Circle()
                                .fill(NT.bgCard.opacity(0.95))
                                .overlay(Circle().stroke(NT.accentTeal.opacity(0.4), lineWidth: 1.5))
                        )
                }

                if showLookbackMenu {
                    VStack(spacing: 6) {
                        ForEach(LookbackPeriod.allCases) { period in
                            Button {
                                lookback = period
                                withAnimation(.easeInOut(duration: 0.2)) {
                                    showLookbackMenu = false
                                }
                            } label: {
                                Text(period.rawValue)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(period == lookback ? NT.accentAmber : NT.textSecondary)
                                    .frame(width: 44, height: 32)
                                    .background(
                                        RoundedRectangle(cornerRadius: 8)
                                            .fill(NT.bgCard.opacity(0.95))
                                            .overlay(
                                                RoundedRectangle(cornerRadius: 8)
                                                    .stroke(period == lookback ? NT.accentAmber.opacity(0.5) : NT.textDim.opacity(0.3), lineWidth: 1)
                                            )
                                    )
                            }
                        }
                    }
                    .padding(.top, 6)
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .padding(.leading, 12)
            .padding(.top, 12)
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

    private var scrubberLabel: String {
        if scrubberSecondsAgo < 60 {
            return "Now • showing last \(lookback.rawValue)"
        }
        let mins = Int(scrubberSecondsAgo / 60)
        if mins < 60 {
            return "\(mins)m ago • last \(lookback.rawValue)"
        }
        let hrs = Double(mins) / 60.0
        return String(format: "%.1fh ago • last %@", hrs, lookback.rawValue)
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
