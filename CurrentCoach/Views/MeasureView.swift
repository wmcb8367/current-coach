import SwiftUI
import Charts

struct MeasureView: View {
    @Bindable var viewModel: MeasureViewModel

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                // Brand header
                HStack(spacing: 12) {
                    M2XLogo(height: 26)
                    Rectangle()
                        .fill(NT.borderSubtle)
                        .frame(width: 1, height: 22)
                    Text("Current Coach")
                        .eyebrow(NT.textSecondary)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 12)

                // Top stats card
                HStack {
                    StatLabel(title: "ACC", value: viewModel.locationService.accuracyLabel,
                              valueColor: accuracyColor)
                    Spacer()
                    StatLabel(title: "DIST", value: viewModel.distanceFormatted)
                    Spacer()
                    StatLabel(title: "TIME", value: viewModel.elapsedFormatted)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .m2xCard()
                .padding(.horizontal)

                // Speed unit row
                HStack(spacing: 0) {
                    UnitLabel(title: "KTS", value: String(format: "%.2f", viewModel.speedKnots))
                    Divider().frame(height: 30).overlay(NT.borderSubtle)
                    ConfidenceLabel(confidence: viewModel.confidence)
                    Divider().frame(height: 30).overlay(NT.borderSubtle)
                    UnitLabel(title: "M/S", value: String(format: "%.2f", viewModel.speedMetersPerSecond))
                }
                .padding(.vertical, 12)
                .m2xCard()
                .padding(.horizontal)
                .padding(.top, 12)

                Spacer()

                // Big speed display
                HStack(alignment: .lastTextBaseline, spacing: 0) {
                    Text(speedWhole)
                        .font(.system(size: 130, weight: .bold, design: .monospaced))
                    Text(".")
                        .font(.system(size: 100, weight: .bold, design: .monospaced))
                    Text(speedDecimal)
                        .font(.system(size: 130, weight: .bold, design: .monospaced))
                    Text(speedHundredths)
                        .font(.system(size: 130, weight: .bold, design: .monospaced))
                }
                .foregroundStyle(NT.textPrimary)
                .shadow(color: NT.accentTeal.opacity(0.35), radius: 24)
                .minimumScaleFactor(0.5)
                .lineLimit(1)

                Text("m/min")
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(NT.accentTeal)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.trailing, 32)

                Spacer()

                // Direction display
                HStack(alignment: .lastTextBaseline) {
                    VStack(alignment: .leading, spacing: 6) {
                        Image(systemName: "safari")
                            .font(.title3)
                            .foregroundStyle(NT.accentTeal)
                            .rotationEffect(.degrees(viewModel.currentDirection))
                        Text("From")
                            .eyebrow(NT.accentTealSoft)
                    }
                    Spacer()
                    Text(String(format: "%.0f", viewModel.currentDirection))
                        .font(.system(size: 120, weight: .bold, design: .monospaced))
                        .foregroundStyle(NT.textPrimary)
                    Text("°")
                        .font(.system(size: 50, weight: .bold, design: .monospaced))
                        .foregroundStyle(NT.textPrimary)
                        .offset(y: -40)
                }
                .padding(.horizontal)
                .minimumScaleFactor(0.5)

                Spacer()

                // Bottom: GPS scope / sparkline + start/stop
                HStack(spacing: 16) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(viewModel.isMeasuring ? "Last 10s" : "GPS Signal")
                            .eyebrow()
                        if viewModel.isMeasuring, !viewModel.recentSpeeds.isEmpty {
                            Chart {
                                ForEach(Array(viewModel.recentSpeeds.enumerated()), id: \.offset) { index, speed in
                                    AreaMark(
                                        x: .value("Time", index),
                                        y: .value("Speed", speed)
                                    )
                                    .foregroundStyle(NT.accentTeal.opacity(0.15))

                                    LineMark(
                                        x: .value("Time", index),
                                        y: .value("Speed", speed)
                                    )
                                    .foregroundStyle(NT.accentTeal)
                                }
                            }
                            .chartXAxis(.hidden)
                            .chartYAxis(.hidden)
                            .frame(height: 50)
                        } else {
                            GPSSignalScope(
                                accuracyMeters: viewModel.locationService.currentLocation?.horizontalAccuracy,
                                isAuthorized: viewModel.locationService.isAuthorized
                            )
                            .frame(height: 50)
                        }
                    }
                    .padding(14)
                    .m2xCard()
                    .frame(maxWidth: 150)

                    // Start/Stop button
                    Button(action: {
                        if viewModel.isMeasuring {
                            viewModel.stop()
                        } else {
                            viewModel.start()
                        }
                    }) {
                        VStack(spacing: 4) {
                            Text(viewModel.isMeasuring ? "Stop" : "Start")
                                .font(.system(size: 40, weight: .bold))
                            Text(viewModel.isMeasuring ? "Recording…" : viewModel.statusLabel)
                                .font(.subheadline)
                        }
                        .foregroundStyle(viewModel.isMeasuring ? .white : NT.bgPrimary)
                        .frame(maxWidth: .infinity)
                        .frame(height: 100)
                        .background(
                            RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                                .fill(viewModel.isMeasuring ? NT.stopGradient : NT.startGradient)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                                .strokeBorder(
                                    viewModel.isMeasuring ? NT.accentCoral.opacity(0.5) : NT.accentTeal.opacity(0.5),
                                    lineWidth: 1
                                )
                        )
                    }
                    .disabled(!viewModel.isMeasuring && !viewModel.isGPSReady)
                    .opacity(!viewModel.isMeasuring && !viewModel.isGPSReady ? 0.4 : 1.0)
                }
                .padding(.horizontal)

                // Version footer
                Text("v1.1.0 · M2X")
                    .font(.caption)
                    .foregroundStyle(NT.textFaint)
                    .padding(.top, 8)
                    .padding(.bottom, 4)
            }
        }
        .onAppear {
            viewModel.locationService.requestAuthorization()
            viewModel.locationService.startUpdating()
        }
    }

    // MARK: - Speed Formatting

    private var speedWhole: String {
        "\(Int(viewModel.currentSpeed))"
    }

    private var speedDecimal: String {
        "\(Int((viewModel.currentSpeed * 10).truncatingRemainder(dividingBy: 10)))"
    }

    private var speedHundredths: String {
        "\(Int((viewModel.currentSpeed * 100).truncatingRemainder(dividingBy: 10)))"
    }

    private var accuracyColor: Color {
        switch viewModel.locationService.accuracyLabel {
        case "Great": return NT.gpsGreat
        case "Good": return NT.gpsGood
        case "Fair": return NT.gpsFair
        default: return NT.gpsPoor
        }
    }
}

// MARK: - Subviews

private struct StatLabel: View {
    let title: String
    let value: String
    var valueColor: Color = NT.textPrimary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).eyebrow()
            Text(value)
                .font(.title2.weight(.bold))
                .foregroundStyle(valueColor)
        }
    }
}

private struct UnitLabel: View {
    let title: String
    let value: String

    var body: some View {
        VStack(spacing: 4) {
            Text(title).eyebrow(NT.accentTealSoft)
            Text(value)
                .font(.system(size: 22, weight: .bold, design: .monospaced))
                .foregroundStyle(NT.textPrimary)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct GPSSignalScope: View {
    let accuracyMeters: Double?
    let isAuthorized: Bool

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: false)) { context in
            Canvas { ctx, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                let maxRadius = min(size.width, size.height) / 2 - 2
                let now = context.date.timeIntervalSinceReferenceDate

                // Animated sweep rings. Period (seconds) + intensity scale with signal.
                let period = pulsePeriod
                let ringCount = 3
                for i in 0..<ringCount {
                    let phase = ((now / period) + Double(i) / Double(ringCount)).truncatingRemainder(dividingBy: 1.0)
                    let r = maxRadius * CGFloat(phase)
                    let alpha = max(0.0, 1.0 - phase) * pulseIntensity
                    let path = Path(ellipseIn: CGRect(x: center.x - r, y: center.y - r, width: r * 2, height: r * 2))
                    ctx.stroke(path, with: .color(ringColor.opacity(alpha * 0.6)), lineWidth: 1.5)
                }

                // Center dot.
                let dotRadius: CGFloat = 3
                let dot = Path(ellipseIn: CGRect(x: center.x - dotRadius, y: center.y - dotRadius, width: dotRadius * 2, height: dotRadius * 2))
                ctx.fill(dot, with: .color(ringColor))
            }
            .overlay(alignment: .bottomTrailing) {
                Text(labelText)
                    .font(.caption2.weight(.bold).monospacedDigit())
                    .foregroundStyle(ringColor)
            }
        }
    }

    private var labelText: String {
        guard isAuthorized else { return "off" }
        guard let acc = accuracyMeters, acc >= 0 else { return "—" }
        return String(format: "%.0fm", acc)
    }

    private var ringColor: Color {
        guard isAuthorized, let acc = accuracyMeters, acc >= 0 else { return NT.textDim }
        if acc < 5 { return NT.gpsGreat }
        if acc < 10 { return NT.gpsGood }
        if acc < 20 { return NT.gpsFair }
        return NT.gpsPoor
    }

    private var pulsePeriod: Double {
        guard isAuthorized, let acc = accuracyMeters, acc >= 0 else { return 3.0 }
        if acc < 5 { return 0.8 }
        if acc < 10 { return 1.3 }
        if acc < 20 { return 2.0 }
        return 2.8
    }

    private var pulseIntensity: Double {
        guard isAuthorized, let acc = accuracyMeters, acc >= 0 else { return 0.25 }
        if acc < 5 { return 1.0 }
        if acc < 10 { return 0.85 }
        if acc < 20 { return 0.6 }
        return 0.35
    }
}

private struct ConfidenceLabel: View {
    let confidence: Double

    private var confidenceColor: Color {
        if confidence < 70 { return NT.accentCoral }
        if confidence < 80 { return NT.accentAmber }
        if confidence < 90 { return NT.accentTealSoft }
        return NT.accentEmerald
    }

    var body: some View {
        VStack(spacing: 4) {
            Text("Conf").eyebrow(NT.accentTealSoft)
            Text(String(format: "%.0f%%", confidence))
                .font(.system(size: 22, weight: .bold, design: .monospaced))
                .foregroundStyle(confidenceColor)
        }
        .frame(maxWidth: .infinity)
    }
}
