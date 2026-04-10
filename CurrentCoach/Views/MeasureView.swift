import SwiftUI
import Charts

struct MeasureView: View {
    @Bindable var viewModel: MeasureViewModel

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
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
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 12)
                        .fill(NT.bgCard)
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .stroke(NT.accentTeal.opacity(0.3), lineWidth: 1)
                        )
                )
                .padding(.horizontal)
                .padding(.top, 8)

                // Speed unit row
                HStack(spacing: 0) {
                    UnitLabel(title: "KTS", value: String(format: "%.2f", viewModel.speedKnots))
                    Divider().frame(height: 30).overlay(NT.textDim)
                    UnitLabel(title: "CM/S", value: String(format: "%.2f", viewModel.speedCmPerSecond))
                    Divider().frame(height: 30).overlay(NT.textDim)
                    UnitLabel(title: "M/S", value: String(format: "%.2f", viewModel.speedMetersPerSecond))
                }
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: 12)
                        .fill(NT.bgCard)
                )
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
                .shadow(color: NT.accentTeal.opacity(0.3), radius: 20)
                .minimumScaleFactor(0.5)
                .lineLimit(1)

                Text("m/min")
                    .font(.title)
                    .fontWeight(.medium)
                    .foregroundStyle(NT.accentAmber)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.trailing, 32)

                Spacer()

                // Direction display
                HStack(alignment: .lastTextBaseline) {
                    VStack(alignment: .leading, spacing: 4) {
                        Image(systemName: "safari")
                            .font(.title3)
                            .foregroundStyle(NT.accentTeal)
                            .rotationEffect(.degrees(viewModel.currentDirection))
                        Text("From:")
                            .font(.title)
                            .fontWeight(.medium)
                            .foregroundStyle(NT.accentAmber)
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

                // Bottom: sparkline + start/stop
                HStack(spacing: 16) {
                    // Last 10s sparkline
                    VStack(alignment: .leading) {
                        Text("LAST 10s")
                            .font(.caption)
                            .foregroundStyle(NT.textSecondary)
                        if viewModel.recentSpeeds.isEmpty {
                            RoundedRectangle(cornerRadius: 8)
                                .fill(NT.bgSurface)
                                .frame(height: 50)
                                .overlay {
                                    HStack(spacing: 8) {
                                        ForEach(0..<3, id: \.self) { _ in
                                            Capsule()
                                                .fill(NT.textDim)
                                                .frame(width: 20, height: 3)
                                        }
                                    }
                                }
                        } else {
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
                        }
                    }
                    .padding()
                    .background(
                        RoundedRectangle(cornerRadius: 12)
                            .fill(NT.bgCard)
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .stroke(NT.accentTeal.opacity(0.3), lineWidth: 1)
                            )
                    )
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
                            Text(viewModel.isMeasuring ? "Recording..." : viewModel.statusLabel)
                                .font(.subheadline)
                        }
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .frame(height: 100)
                        .background(
                            RoundedRectangle(cornerRadius: 16)
                                .fill(viewModel.isMeasuring ? NT.stopGradient : NT.startGradient)
                                .overlay(
                                    RoundedRectangle(cornerRadius: 16)
                                        .stroke(
                                            viewModel.isMeasuring ? NT.accentCoral.opacity(0.5) : NT.accentTeal.opacity(0.5),
                                            lineWidth: 1
                                        )
                                )
                        )
                    }
                    .disabled(!viewModel.isMeasuring && !viewModel.isGPSReady)
                    .opacity(!viewModel.isMeasuring && !viewModel.isGPSReady ? 0.4 : 1.0)
                }
                .padding(.horizontal)

                // Version footer
                Text("Ver:1.0.0")
                    .font(.caption)
                    .foregroundStyle(NT.textDim)
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
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(NT.textDim)
            Text(value)
                .font(.title2)
                .fontWeight(.bold)
                .foregroundStyle(valueColor)
        }
    }
}

private struct UnitLabel: View {
    let title: String
    let value: String

    var body: some View {
        VStack(spacing: 2) {
            Text(title)
                .font(.caption2)
                .fontWeight(.semibold)
                .foregroundStyle(NT.accentAmber)
            Text(value)
                .font(.system(size: 22, weight: .bold, design: .monospaced))
                .foregroundStyle(NT.textPrimary)
        }
        .frame(maxWidth: .infinity)
    }
}
