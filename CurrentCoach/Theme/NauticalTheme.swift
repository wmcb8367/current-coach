import SwiftUI

/// M2X brand design tokens. Mirrors m2xsailing.com: slate-950 background,
/// cyan-300 accent text, white/5 cards with white/10 borders, white primary CTAs.
enum NT {
    // Backgrounds — tailwind slate-950 #020617
    static let bgPrimary = Color(red: 0.008, green: 0.024, blue: 0.090) // #020617 slate-950
    static let bgCard    = Color.white.opacity(0.05)                    // white/5 over bgPrimary
    static let bgSurface = Color(red: 0.059, green: 0.090, blue: 0.165) // slate-900-ish
    static let bgElevated = Color(red: 0.047, green: 0.075, blue: 0.137) // slate-950/60

    // Borders
    static let borderSubtle = Color.white.opacity(0.10)                 // white/10
    static let borderStrong = Color.white.opacity(0.20)                 // white/20

    // Primary accent: cyan-400
    static let accentTeal  = Color(red: 0.133, green: 0.827, blue: 0.933) // #22d3ee cyan-400
    static let accentTealSoft = Color(red: 0.404, green: 0.906, blue: 0.976) // #67e8f9 cyan-300

    // Secondary accents
    static let accentAmber = Color(red: 0.984, green: 0.749, blue: 0.141) // amber-400
    static let accentCoral = Color(red: 0.961, green: 0.361, blue: 0.259) // red-400
    static let accentEmerald = Color(red: 0.204, green: 0.827, blue: 0.600) // emerald-400

    // Text (matches body #ecf4ff with slate-300/400 secondaries)
    static let textPrimary   = Color(red: 0.925, green: 0.957, blue: 1.000) // #ecf4ff
    static let textSecondary = Color(red: 0.796, green: 0.835, blue: 0.882) // slate-300
    static let textDim       = Color(red: 0.580, green: 0.639, blue: 0.722) // slate-400
    static let textFaint     = Color(red: 0.408, green: 0.467, blue: 0.549) // slate-500

    // GPS status
    static let gpsGreat = accentEmerald
    static let gpsGood  = accentTealSoft
    static let gpsFair  = accentAmber
    static let gpsPoor  = accentCoral

    // Gradients
    static let startGradient = LinearGradient(
        colors: [accentTeal, accentTeal.opacity(0.6)],
        startPoint: .topLeading, endPoint: .bottomTrailing
    )
    static let stopGradient = LinearGradient(
        colors: [accentCoral, accentCoral.opacity(0.7)],
        startPoint: .topLeading, endPoint: .bottomTrailing
    )

    // Card corner radius — rounded-2xl (16pt)
    static let cardRadius: CGFloat = 16
    static let largeCardRadius: CGFloat = 22
}

// MARK: - Common modifiers

/// Eyebrow label: uppercase, tight tracking, slate-400 — matches the M2X metric card labels.
struct EyebrowStyle: ViewModifier {
    var color: Color = NT.textDim
    func body(content: Content) -> some View {
        content
            .font(.system(size: 11, weight: .semibold))
            .tracking(2.2)
            .textCase(.uppercase)
            .foregroundStyle(color)
    }
}

/// Card: rounded-2xl, white/5 fill, white/10 border — matches MetricCard from the web app.
struct M2XCardStyle: ViewModifier {
    var radius: CGFloat = NT.cardRadius
    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(NT.bgCard)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(NT.borderSubtle, lineWidth: 1)
            )
    }
}

extension View {
    func eyebrow(_ color: Color = NT.textDim) -> some View {
        modifier(EyebrowStyle(color: color))
    }
    func m2xCard(radius: CGFloat = NT.cardRadius) -> some View {
        modifier(M2XCardStyle(radius: radius))
    }
}
