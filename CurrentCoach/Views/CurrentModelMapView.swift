import SwiftUI
import MapKit

/// The current over a venue as a moving map, from the model the sailor
/// picks. Colour is speed; the streaks drift the way the water goes. The
/// slider runs through the day, and a tap on the water reads the current there.
///
/// Only accounts the server allows see this tab (see `AuthStore.canSeeCurrentModels`).
struct CurrentModelMapView: View {
    let auth: AuthStore
    let measurements: MeasurementStore
    let models: CurrentModelStore

    @State private var camera: MapCameraPosition = .automatic
    @State private var time: Date = CurrentModelMapView.tenMinutes(Date())
    @State private var isPlaying = false
    @State private var box: CGRect?
    @State private var probe: ProbePoint?
    @State private var showSelector = false
    @State private var frame: CurrentFrame?
    @State private var image: CGImage?
    @State private var engine = CurrentParticleEngine()
    @State private var framedVenue: String?

    private struct ProbePoint: Equatable {
        let latitude: Double
        let longitude: Double
        var coordinate: CLLocationCoordinate2D { CLLocationCoordinate2D(latitude: latitude, longitude: longitude) }
    }

    /// Everything a redraw depends on: a change to any part starts one.
    private struct DrawKey: Equatable {
        let model: String
        let time: Date
        let loaded: Int
        let manifestUpdated: Double
    }

    private static let step: TimeInterval = 600

    private static func tenMinutes(_ date: Date) -> Date {
        Date(timeIntervalSince1970: (date.timeIntervalSince1970 / step).rounded() * step)
    }

    // MARK: - Body

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()
            switch models.phase {
            case .idle, .loading:
                ProgressView("Loading current models")
                    .tint(NT.accentTeal)
                    .foregroundStyle(NT.textSecondary)
            case .failed(let message):
                failure(message)
            case .ready:
                ready
            }
        }
        .task(id: auth.token) {
            await models.refresh(token: auth.token)
            await models.loadValidation(token: auth.token)
        }
        .sheet(isPresented: $showSelector) {
            CurrentModelSelectorView(models: models, onRefresh: {
                await models.refresh(token: auth.token)
                await models.loadValidation(token: auth.token)
            })
        }
    }

    private var ready: some View {
        VStack(spacing: 0) {
            map
            controls
        }
        .task(id: DrawKey(
            model: models.modelID,
            time: time,
            loaded: models.loadedRevision,
            manifestUpdated: models.manifest?.updated ?? 0
        )) {
            await redraw()
        }
        .task(id: isPlaying) {
            await play()
        }
        .onChange(of: models.venue?.id, initial: true) { _, venue in
            frameVenue(venue)
        }
        .onChange(of: models.modelID) { _, _ in
            clampTime()
        }
    }

    private func failure(_ message: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "water.waves.slash")
                .font(.system(size: 34))
                .foregroundStyle(NT.textDim)
            Text(message)
                .font(.subheadline)
                .multilineTextAlignment(.center)
                .foregroundStyle(NT.textSecondary)
            Button("Try again") {
                Task {
                    await models.refresh(token: auth.token)
                    await models.loadValidation(token: auth.token)
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(NT.accentTeal)
        }
        .padding(32)
    }

    // MARK: - Map

    private var map: some View {
        GeometryReader { geometry in
            MapReader { proxy in
                Map(position: $camera, interactionModes: [.pan, .zoom]) {
                    UserAnnotation()
                    ForEach(nearbyReadings) { reading in
                        Annotation("", coordinate: reading.coordinate, anchor: .center) {
                            MeasuredCurrentArrow(setDegrees: reading.flowDirection, knots: reading.speedKnots)
                        }
                        .annotationTitles(.hidden)
                    }
                    if let probe {
                        Annotation("", coordinate: probe.coordinate, anchor: .center) {
                            Circle()
                                .strokeBorder(.white, lineWidth: 2)
                                .background(Circle().fill(NT.accentTeal.opacity(0.5)))
                                .frame(width: 16, height: 16)
                        }
                        .annotationTitles(.hidden)
                    }
                }
                .mapStyle(.standard(elevation: .flat, emphasis: .muted))
                .onMapCameraChange(frequency: .continuous) { _ in
                    locateBox(proxy, size: geometry.size)
                }
                .onChange(of: geometry.size, initial: true) { _, size in
                    locateBox(proxy, size: size)
                }
                .onChange(of: models.venue?.id) { _, _ in
                    locateBox(proxy, size: geometry.size)
                }
                .onTapGesture { point in
                    if let place = proxy.convert(point, from: .local) {
                        probe = ProbePoint(latitude: place.latitude, longitude: place.longitude)
                    }
                }
                .overlay {
                    CurrentFieldOverlay(
                        frame: frame,
                        image: image,
                        grid: models.venue?.grid,
                        box: box,
                        showStreaks: true,
                        engine: engine
                    )
                }
                .overlay(alignment: .top) { topBar }
                .overlay(alignment: .bottomLeading) {
                    CurrentLegendView().padding(10)
                }
            }
        }
    }

    /// Work out where the model's grid lies on screen. The map is never
    /// turned or tilted here, so two corners of the view pin it down:
    /// longitude runs evenly across, latitude evenly in Mercator's scale.
    private func locateBox(_ proxy: MapProxy, size: CGSize) {
        guard size.width > 1, size.height > 1, let grid = models.venue?.grid,
              let topLeft = proxy.convert(CGPoint.zero, from: .local),
              let bottomRight = proxy.convert(CGPoint(x: size.width, y: size.height), from: .local),
              bottomRight.longitude != topLeft.longitude, bottomRight.latitude != topLeft.latitude
        else { return }
        func mercator(_ latitude: Double) -> Double {
            log(tan(Double.pi / 4 + latitude * Double.pi / 360))
        }
        func x(_ longitude: Double) -> CGFloat {
            CGFloat((longitude - topLeft.longitude) / (bottomRight.longitude - topLeft.longitude)) * size.width
        }
        func y(_ latitude: Double) -> CGFloat {
            CGFloat((mercator(topLeft.latitude) - mercator(latitude)) / (mercator(topLeft.latitude) - mercator(bottomRight.latitude))) * size.height
        }
        let west = x(grid.west)
        let east = x(grid.east)
        let north = y(grid.north)
        let south = y(grid.south)
        guard west.isFinite, east.isFinite, north.isFinite, south.isFinite else { return }
        box = CGRect(x: west, y: north, width: east - west, height: south - north)
    }

    private func frameVenue(_ id: String?) {
        guard let venue = models.venue, framedVenue != venue.id else { return }
        framedVenue = venue.id
        engine.reset()
        probe = nil
        camera = .region(MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: venue.centerLatitude, longitude: venue.centerLongitude),
            span: MKCoordinateSpan(latitudeDelta: venue.north - venue.south, longitudeDelta: venue.east - venue.west)
        ))
        clampTime()
    }

    /// The sailor's own readings taken within an hour and a half of the time shown.
    private var nearbyReadings: [TideMeasurement] {
        guard let grid = models.venue?.grid else { return [] }
        return measurements.measurements.filter { reading in
            reading.isValid
                && abs(reading.timestamp.addingTimeInterval(reading.duration / 2).timeIntervalSince(time)) <= 5400
                && grid.contains(latitude: reading.latitude, longitude: reading.longitude)
        }
    }

    // MARK: - Top bar

    private var topBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Button {
                    showSelector = true
                } label: {
                    modelChip
                }
                .buttonStyle(.plain)
                Spacer(minLength: 0)
            }
            if let probe {
                probeCard(probe)
            }
            if let notice = statusNotice {
                Text(notice)
                    .font(.caption)
                    .foregroundStyle(NT.textPrimary)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Capsule().fill(NT.bgPrimary.opacity(0.85)))
            }
        }
        .padding(10)
    }

    private var modelChip: some View {
        HStack(spacing: 10) {
            Image(systemName: "square.3.layers.3d")
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(NT.accentTeal)
            VStack(alignment: .leading, spacing: 1) {
                Text(models.model?.name ?? "Choose a model")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(NT.textPrimary)
                Text(chipDetail)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(NT.textDim)
            }
            Image(systemName: "chevron.down")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(NT.textDim)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(NT.bgPrimary.opacity(0.88))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(NT.borderStrong, lineWidth: 1)
        )
        .accessibilityLabel("Current model: \(models.model?.name ?? "none"). \(chipDetail)")
        .accessibilityHint("Shows all models and how each matched readings")
    }

    /// The chosen model's score over the chosen window, in a few words.
    private var chipDetail: String {
        guard let score = models.scores?.models[models.modelID], score.count > 0 else {
            return models.isLoadingValidation ? "Checking against readings" : "No readings to compare yet"
        }
        return "\(score.errorText) off over \(score.count) reading\(score.count == 1 ? "" : "s"), \(models.window.title.lowercased())"
    }

    private func probeCard(_ point: ProbePoint) -> some View {
        let current = models.venue.flatMap { venue in
            frame?.velocity(grid: venue.grid, latitude: point.latitude, longitude: point.longitude)
        }
        return HStack(spacing: 10) {
            if let current {
                Image(systemName: "location.north.fill")
                    .font(.system(size: 14))
                    .foregroundStyle(NT.accentTeal)
                    .rotationEffect(.degrees(current.setDegrees))
                Text(String(format: "%.2f kn", current.speedKnots))
                    .font(.system(size: 17, weight: .bold).monospacedDigit())
                    .foregroundStyle(NT.textPrimary)
                Text(String(format: "toward %03.0f°", current.setDegrees))
                    .font(.system(size: 13, weight: .medium).monospacedDigit())
                    .foregroundStyle(NT.textSecondary)
            } else {
                Text("No current here: land, or outside the model")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(NT.textSecondary)
            }
            Button {
                probe = nil
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 16))
                    .foregroundStyle(NT.textDim)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Clear the point")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(NT.bgPrimary.opacity(0.88))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(NT.borderSubtle, lineWidth: 1)
        )
    }

    /// What to tell the sailor when there is nothing to draw, or the maps are stale.
    private var statusNotice: String? {
        let day = CurrentDay.split(time).day
        let model = models.modelID
        if let error = models.downloadError(model: model, day: day) { return error }
        if models.isDownloading(model: model, day: day) { return "Downloading this day's map" }
        if !models.hasMap(model: model, day: day) { return "This model has no map for this time" }
        return models.notice
    }

    // MARK: - Time

    private var zone: TimeZone {
        models.venue?.timeZone.flatMap { TimeZone(identifier: $0) } ?? .current
    }

    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar
    }

    /// The span of time the chosen model has maps for.
    private var span: ClosedRange<Date>? {
        guard let days = models.model?.days,
              let first = days.first.flatMap({ CurrentDay.start(of: $0.day) }),
              let last = days.last.flatMap({ CurrentDay.start(of: $0.day) })
        else { return nil }
        return first...last.addingTimeInterval(CurrentDay.seconds)
    }

    /// The venue's days, by its own clock, that fall inside the span.
    private var localDays: [Date] {
        guard let span else { return [] }
        var days: [Date] = []
        var day = calendar.startOfDay(for: span.lowerBound)
        while day < span.upperBound, days.count < 40 {
            days.append(day)
            guard let next = calendar.date(byAdding: .day, value: 1, to: day) else { break }
            day = next
        }
        return days
    }

    private var dayStart: Date { calendar.startOfDay(for: time) }

    private func clampTime() {
        guard let span else { return }
        time = min(max(time, span.lowerBound), span.upperBound)
    }

    private var minutes: Binding<Double> {
        Binding(
            get: { time.timeIntervalSince(dayStart) / 60 },
            set: { value in
                isPlaying = false
                time = dayStart.addingTimeInterval(value * 60)
                clampTime()
            }
        )
    }

    private func label(_ date: Date, _ style: Date.FormatStyle) -> String {
        var style = style
        style.timeZone = zone
        return date.formatted(style)
    }

    private var controls: some View {
        VStack(spacing: 10) {
            ScrollViewReader { scroller in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(localDays, id: \.self) { day in
                            dayChip(day)
                                .id(day)
                        }
                    }
                    .padding(.horizontal, 2)
                }
                .onChange(of: dayStart, initial: true) { _, day in
                    withAnimation(.easeInOut(duration: 0.2)) { scroller.scrollTo(day, anchor: .center) }
                }
            }

            HStack(spacing: 12) {
                Button {
                    isPlaying.toggle()
                } label: {
                    Image(systemName: isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(NT.bgPrimary)
                        .frame(width: 40, height: 40)
                        .background(Circle().fill(NT.accentTeal))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(isPlaying ? "Pause" : "Play through the day")

                VStack(spacing: 2) {
                    HStack {
                        Text(label(time, .dateTime.weekday(.abbreviated).day().month(.abbreviated)))
                            .font(.caption)
                            .foregroundStyle(NT.textDim)
                        Spacer()
                        Text(label(time, .dateTime.hour().minute()))
                            .font(.system(size: 17, weight: .bold).monospacedDigit())
                            .foregroundStyle(NT.accentTeal)
                        Spacer()
                        Text(issuedText)
                            .font(.caption)
                            .foregroundStyle(NT.textDim)
                            .lineLimit(1)
                    }
                    Slider(value: minutes, in: 0...1440, step: 10)
                        .tint(NT.accentTeal)
                        .accessibilityLabel("Time of day")
                        .accessibilityValue(label(time, .dateTime.hour().minute()))
                }

                Button("Now") {
                    isPlaying = false
                    time = Self.tenMinutes(Date())
                    clampTime()
                }
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(NT.accentTeal)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .background(NT.bgCard)
    }

    private func dayChip(_ day: Date) -> some View {
        let selected = day == dayStart
        let hasReadings = measurements.measurements.contains { calendar.startOfDay(for: $0.timestamp) == day }
        return Button {
            isPlaying = false
            time = day.addingTimeInterval(time.timeIntervalSince(dayStart))
            clampTime()
        } label: {
            VStack(spacing: 2) {
                Text(calendar.isDateInToday(day) ? "Today" : label(day, .dateTime.weekday(.abbreviated)))
                    .font(.system(size: 11, weight: .semibold))
                Text(label(day, .dateTime.day()))
                    .font(.system(size: 15, weight: .bold).monospacedDigit())
                Circle()
                    .fill(hasReadings ? NT.accentAmber : Color.clear)
                    .frame(width: 4, height: 4)
            }
            .foregroundStyle(selected ? NT.bgPrimary : NT.textSecondary)
            .frame(width: 52, height: 48)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(selected ? NT.accentTeal : Color.white.opacity(0.06))
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label(day, .dateTime.weekday(.wide).day().month(.wide)) + (hasReadings ? ", has readings" : ""))
    }

    /// When the map on screen was made, so a forecast is never mistaken for a measurement.
    private var issuedText: String {
        guard let entry = models.model?.entry(for: CurrentDay.split(time).day) else { return "" }
        let made = label(Date(timeIntervalSince1970: entry.issued), .dateTime.weekday(.abbreviated).hour().minute())
        return entry.kind == "forecast" ? "Forecast, \(made)" : "Built \(made)"
    }

    // MARK: - Work

    /// Bring the map for the chosen model and time on screen, downloading its day if need be.
    private func redraw() async {
        let model = models.modelID
        let moment = time
        await models.load(model: model, day: CurrentDay.split(moment).day, token: auth.token)
        guard !Task.isCancelled else { return }
        guard let next = models.frame(model: model, at: moment) else {
            frame = nil
            image = nil
            return
        }
        frame = next
        let raster = await Task.detached(priority: .userInitiated) {
            CurrentRaster.make(frame: next)
        }.value
        guard !Task.isCancelled else { return }
        image = raster.cgImage
    }

    /// Run the clock through the day shown, ten minutes at a time, and round again.
    private func play() async {
        while isPlaying, !Task.isCancelled {
            try? await Task.sleep(for: .milliseconds(160))
            guard isPlaying, !Task.isCancelled else { return }
            let start = dayStart
            var next = time.addingTimeInterval(Self.step)
            if next.timeIntervalSince(start) > CurrentDay.seconds { next = start }
            if let span, next > span.upperBound { next = max(start, span.lowerBound) }
            time = next
        }
    }
}

/// A reading taken on the water, drawn as an arrow pointing the way the
/// water went, so it can be compared with the model underneath.
private struct MeasuredCurrentArrow: View {
    let setDegrees: Double
    let knots: Double

    var body: some View {
        VStack(spacing: 1) {
            Image(systemName: "location.north.fill")
                .font(.system(size: 20, weight: .bold))
                .foregroundStyle(NT.accentAmber)
                .shadow(color: .black.opacity(0.7), radius: 1.5)
                .rotationEffect(.degrees(setDegrees))
            Text(String(format: "%.2f", knots))
                .font(.system(size: 10, weight: .bold).monospacedDigit())
                .foregroundStyle(.white)
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(Capsule().fill(.black.opacity(0.6)))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(String(format: "Measured %.2f knots toward %.0f degrees", knots, setDegrees))
    }
}
