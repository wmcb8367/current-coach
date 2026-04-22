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
    @Binding var focus: MapFocus?

    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var mapStyle: MapDisplayStyle = {
        // Screenshot/dev hook: CC_DEFAULT_MAP_STYLE=charts|satellite|standard
        let raw = ProcessInfo.processInfo.environment["CC_DEFAULT_MAP_STYLE"] ?? ""
        return MapDisplayStyle(rawValue: raw.capitalized) ?? .standard
    }()
    @State private var nauticalRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 39.5, longitude: 2.7),
        span: MKCoordinateSpan(latitudeDelta: 0.1, longitudeDelta: 0.1)
    )

    // Time scrubber: position in last 24h (0 = now, 86400 = 24h ago)
    @State private var scrubberSecondsAgo: Double = 0
    @State private var lookback: LookbackPeriod = .oneHour
    @State private var showLookbackMenu = false

    @State private var showHeatMap: Bool = false
    @State private var heatMapLookbackHours: Double = 3

    private var referenceTime: Date {
        Date().addingTimeInterval(-scrubberSecondsAgo)
    }

    private var filteredMeasurements: [TideMeasurement] {
        if let focus {
            return store.measurements.filter { $0.timestamp >= focus.startTime && $0.timestamp <= focus.endTime }
        }
        if showHeatMap {
            let cutoff = Date().addingTimeInterval(-heatMapLookbackHours * 3600)
            return store.measurements.filter { $0.timestamp >= cutoff }
        }
        let endTime = referenceTime
        let startTime = endTime.addingTimeInterval(-lookback.seconds)
        return store.measurements.filter { $0.timestamp >= startTime && $0.timestamp <= endTime }
    }

    private var vectorField: VectorFieldResult? {
        guard showHeatMap else { return nil }
        return VectorFieldInterpolator.compute(measurements: filteredMeasurements)
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

                        if let field = vectorField {
                            ForEach(field.samples) { sample in
                                Annotation("", coordinate: CLLocationCoordinate2D(latitude: sample.latitude, longitude: sample.longitude), anchor: .center) {
                                    FieldArrowView(sample: sample, speedFraction: sample.speedKnots / 1.5)
                                }
                                .annotationTitles(.hidden)
                            }
                        }

                        ForEach(filteredMeasurements) { measurement in
                            Annotation("", coordinate: measurement.coordinate, anchor: .center) {
                                CurrentArrowView(measurement: measurement, speedFraction: measurement.speedKnots / 1.5)
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

                    if let focus {
                        focusBanner(for: focus)
                    } else if showHeatMap {
                        VStack(spacing: 4) {
                            HStack {
                                Text("1h")
                                    .font(.caption2)
                                    .foregroundStyle(NT.textDim)
                                Spacer()
                                Text(heatMapLookbackLabel)
                                    .font(.caption.weight(.bold))
                                    .foregroundStyle(NT.accentTealSoft)
                                Spacer()
                                Text("24h")
                                    .font(.caption2)
                                    .foregroundStyle(NT.textDim)
                            }
                            Slider(value: $heatMapLookbackHours, in: 1...24, step: 0.5)
                                .tint(NT.accentTealSoft)
                        }
                    } else {
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
                }
                .padding()
                .background(NT.bgCard)
            }

            // Top-left column: lookback selector (when applicable) + heat map toggle
            VStack(spacing: 10) {
                if !showHeatMap && focus == nil {
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
                }

                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        if !showHeatMap && focus != nil { focus = nil }
                        showHeatMap.toggle()
                    }
                } label: {
                    Image(systemName: showHeatMap ? "square.grid.3x3.fill" : "square.grid.3x3")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(showHeatMap ? NT.bgPrimary : NT.accentTealSoft)
                        .frame(width: 50, height: 50)
                        .background(
                            Circle()
                                .fill(showHeatMap ? NT.accentTealSoft : NT.bgCard.opacity(0.95))
                                .overlay(Circle().stroke(NT.accentTealSoft.opacity(0.4), lineWidth: 1.5))
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
        .onChange(of: mapStyle) {
            if mapStyle == .charts {
                syncToNauticalRegion()
            }
        }
        .onChange(of: focus) { _, newFocus in
            if newFocus != nil {
                // Suppress free-form scrubber while focused.
                scrubberSecondsAgo = 0
                showLookbackMenu = false
                showHeatMap = false
            }
            updateCamera()
            if mapStyle == .charts { syncToNauticalRegion() }
        }
        .onChange(of: showHeatMap) { _, _ in
            updateCamera()
            if mapStyle == .charts { syncToNauticalRegion() }
        }
        .onAppear {
            updateCamera()
            if mapStyle == .charts {
                syncToNauticalRegion()
            }
        }
    }

    @ViewBuilder
    private func focusBanner(for focus: MapFocus) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "scope")
                .foregroundStyle(NT.accentTealSoft)
            VStack(alignment: .leading, spacing: 2) {
                Text("Focused on measurement")
                    .eyebrow(NT.accentTealSoft)
                Text(Self.focusDateFormatter.string(from: focus.timestamp))
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                    .foregroundStyle(NT.textPrimary)
                Text("±12h window")
                    .font(.caption)
                    .foregroundStyle(NT.textDim)
            }
            Spacer()
            Button {
                self.focus = nil
            } label: {
                Text("Live")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(NT.bgPrimary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Capsule().fill(Color.white))
            }
        }
    }

    private static let focusDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d, HH:mm"
        return f
    }()

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

    private var heatMapLookbackLabel: String {
        let hours = heatMapLookbackHours
        let pointCount = filteredMeasurements.filter(\.isValid).count
        let durationLabel: String = {
            if hours >= 1 && hours.truncatingRemainder(dividingBy: 1) == 0 {
                return String(format: "last %.0fh", hours)
            }
            return String(format: "last %.1fh", hours)
        }()
        return "Vector field • \(durationLabel) • \(pointCount) pts"
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
        let region: MKCoordinateRegion
        if showHeatMap, let field = vectorField {
            region = regionForField(field)
        } else if !filteredMeasurements.isEmpty {
            region = regionForMeasurements(filteredMeasurements)
        } else if let loc = locationService.currentLocation {
            region = MKCoordinateRegion(
                center: loc.coordinate,
                span: MKCoordinateSpan(latitudeDelta: 0.05, longitudeDelta: 0.05)
            )
        } else {
            return
        }
        nauticalRegion = region
        // Apple's top-down MapCamera shows ~2× the distance value in meters
        // vertically on a portrait phone. Heat-map mode frames the padded
        // field bbox (loose fit looks right); the list/scrubber views frame
        // just the measurements — zoom in tighter so individual arrows stand
        // out on the course.
        let latMeters = region.span.latitudeDelta * 111_320.0
        let lonMeters = region.span.longitudeDelta * 111_320.0 * cos(region.center.latitude * .pi / 180.0)
        let multiplier = showHeatMap ? 0.55 : 0.22
        let floor: Double = showHeatMap ? 200 : 80
        let distance = max(floor, max(lonMeters, latMeters) * multiplier)
        let camera = MapCamera(
            centerCoordinate: region.center,
            distance: distance,
            heading: 0,
            pitch: 0
        )
        withAnimation(.easeInOut(duration: 0.35)) {
            cameraPosition = .camera(camera)
        }
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
            span: MKCoordinateSpan(
                latitudeDelta: max(0.008, (maxLat - minLat) * 1.8),
                longitudeDelta: max(0.008, (maxLon - minLon) * 1.8)
            )
        )
    }

    private func regionForField(_ field: VectorFieldResult) -> MKCoordinateRegion {
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(
                latitude: (field.minLat + field.maxLat) / 2,
                longitude: (field.minLon + field.maxLon) / 2
            ),
            span: MKCoordinateSpan(
                latitudeDelta: max(0.003, field.maxLat - field.minLat),
                longitudeDelta: max(0.003, field.maxLon - field.minLon)
            )
        )
    }
}

// MARK: - Arrow Annotation (for SwiftUI Map modes)

private struct CurrentArrowView: View {
    let measurement: TideMeasurement
    var speedFraction: Double = 0.5

    private var arrowColor: Color {
        colorForSpeed(fraction: speedFraction)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Image(systemName: "location.north.fill")
                .font(.title)
                .fontWeight(.bold)
                .foregroundStyle(arrowColor)
                .rotationEffect(.degrees(measurement.flowDirection))
                .shadow(color: .black.opacity(0.55), radius: 3, y: 1)

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
                    .fill(NT.bgPrimary)
                    .overlay(
                        RoundedRectangle(cornerRadius: 4)
                            .stroke(arrowColor.opacity(0.6), lineWidth: 1)
                    )
            )
            .shadow(color: .black.opacity(0.55), radius: 4, y: 2)
        }
    }
}

// MARK: - Vector Field Arrow (heat map mode)

private struct FieldArrowView: View {
    let sample: VectorFieldSample
    var speedFraction: Double = 0.5

    private var color: Color {
        colorForSpeed(fraction: speedFraction)
    }

    var body: some View {
        Image(systemName: "arrow.up")
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(color.opacity(0.25 + 0.45 * sample.confidence))
            .rotationEffect(.degrees(sample.flowDirectionDegrees))
    }
}
