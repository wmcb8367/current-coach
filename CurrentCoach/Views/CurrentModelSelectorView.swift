import SwiftUI

/// Pick a current model, and see how each one has matched the readings
/// taken on the water over the last day or the last week.
struct CurrentModelSelectorView: View {
    let models: CurrentModelStore
    let onRefresh: @MainActor () async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var isRefreshing = false

    private var window: Binding<CurrentValidationWindow> {
        Binding(get: { models.window }, set: { models.select(window: $0) })
    }

    private var readings: Binding<CurrentValidationReadings> {
        Binding(get: { models.readings }, set: { models.select(readings: $0) })
    }

    var body: some View {
        NavigationStack {
            ZStack {
                NT.bgPrimary.ignoresSafeArea()
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        settings
                        VStack(spacing: 10) {
                            ForEach(models.manifest?.models ?? []) { model in
                                row(model)
                            }
                        }
                        notes
                    }
                    .padding(16)
                }
            }
            .navigationTitle("Current models")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(NT.bgPrimary, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        Task {
                            isRefreshing = true
                            await onRefresh()
                            isRefreshing = false
                        }
                    } label: {
                        if isRefreshing || models.isLoadingValidation {
                            ProgressView()
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .accessibilityLabel("Refresh models and scores")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                        .fontWeight(.semibold)
                }
            }
        }
        .preferredColorScheme(.dark)
        .tint(NT.accentTeal)
    }

    // MARK: - Setting

    private var settings: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Judge the models on")
                .eyebrow()
            Picker("Readings from", selection: window) {
                ForEach(CurrentValidationWindow.allCases) { choice in
                    Text(choice.title).tag(choice)
                }
            }
            .pickerStyle(.segmented)
            Picker("Whose readings", selection: readings) {
                ForEach(CurrentValidationReadings.allCases) { choice in
                    Text(choice.title).tag(choice)
                }
            }
            .pickerStyle(.segmented)
            Text(summary)
                .font(.footnote)
                .foregroundStyle(NT.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .m2xCard()
    }

    /// One line on what the scores below rest on.
    private var summary: String {
        if let error = models.validationError, models.validation == nil {
            return "The scores could not be loaded: \(error)"
        }
        guard let scope = models.scores else {
            return models.isLoadingValidation ? "Comparing the models with readings." : "No scores yet."
        }
        guard scope.readings > 0 else {
            return "No readings at this venue in the \(models.window.title.lowercased()). A reading counts once it has synced."
        }
        let count = "\(scope.readings) reading\(scope.readings == 1 ? "" : "s")"
        let boats = models.readings == .all ? " from \(scope.boats) boat\(scope.boats == 1 ? "" : "s")" : ""
        return "\(count)\(boats) in the \(models.window.title.lowercased()). Each is compared with the map made before that day began."
    }

    // MARK: - Models

    private func row(_ model: CurrentModelManifest.Model) -> some View {
        let selected = model.id == models.modelID
        let score = models.scores?.models[model.id]
        let best = models.scores?.best() == model.id
        let spokenScore = score.map { "Error " + $0.errorText + ". " + $0.detailText } ?? "No score"
        let spoken = model.name + (best ? ", best match. " : ". ") + spokenScore
        return Button {
            models.select(model: model.id)
            dismiss()
        } label: {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                        .foregroundStyle(selected ? NT.accentTeal : NT.textFaint)
                    Text(model.name)
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(NT.textPrimary)
                    if best {
                        Text("Best match")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(NT.bgPrimary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(Capsule().fill(NT.accentEmerald))
                    }
                    Spacer(minLength: 4)
                    Text(score?.errorText ?? "–")
                        .font(.system(size: 17, weight: .bold).monospacedDigit())
                        .foregroundStyle(score?.errorKn == nil ? NT.textFaint : NT.textPrimary)
                }
                Text(model.summary)
                    .font(.footnote)
                    .foregroundStyle(NT.textSecondary)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Text(model.factors.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(NT.accentTealSoft)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                if let score {
                    ScoreBar(score: score)
                    Text(score.detailText)
                        .font(.caption)
                        .foregroundStyle(NT.textDim)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                    .fill(selected ? NT.accentTeal.opacity(0.10) : NT.bgCard)
            )
            .overlay(
                RoundedRectangle(cornerRadius: NT.cardRadius, style: .continuous)
                    .strokeBorder(selected ? NT.accentTeal.opacity(0.55) : NT.borderSubtle, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    // MARK: - Notes

    private var notes: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Reading the scores")
                .eyebrow()
            Text("The number on the right is how far the model was from a reading on average, in knots, counting both speed and direction. Lower is better. The bar shows how much of the error of assuming no current at all the model removes.")
                .font(.footnote)
                .foregroundStyle(NT.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let attribution = models.manifest?.attribution, !attribution.isEmpty {
                Text("Built from: " + attribution.joined(separator: "; ") + ".")
                    .font(.caption)
                    .foregroundStyle(NT.textFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 2)
    }
}

/// How much of the "no current" error a model removes: a full bar is a
/// perfect match, an empty one no better than assuming slack water.
private struct ScoreBar: View {
    let score: CurrentValidationReport.Score

    var body: some View {
        if let gain = score.gain {
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.08))
                    Capsule()
                        .fill(gain > 0 ? NT.accentTeal : NT.accentCoral)
                        .frame(width: max(4, geometry.size.width * CGFloat(min(1, abs(gain)))))
                }
            }
            .frame(height: 5)
            .accessibilityHidden(true)
        }
    }
}
