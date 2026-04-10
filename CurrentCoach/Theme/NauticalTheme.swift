import SwiftUI

enum NT {
    // Backgrounds
    static let bgPrimary = Color(red: 0.06, green: 0.08, blue: 0.12)
    static let bgCard    = Color(red: 0.10, green: 0.13, blue: 0.18)
    static let bgSurface = Color(red: 0.14, green: 0.17, blue: 0.22)

    // Accents
    static let accentTeal  = Color(red: 0.0, green: 0.82, blue: 0.77)
    static let accentAmber = Color(red: 1.0, green: 0.76, blue: 0.03)
    static let accentCoral = Color(red: 0.96, green: 0.36, blue: 0.26)

    // Text
    static let textPrimary   = Color.white
    static let textSecondary = Color(white: 0.55)
    static let textDim       = Color(white: 0.35)

    // GPS status
    static let gpsGreat = accentTeal
    static let gpsGood  = Color(red: 0.4, green: 0.75, blue: 0.95)
    static let gpsFair  = accentAmber
    static let gpsPoor  = accentCoral

    // Gradients
    static let startGradient = LinearGradient(
        colors: [accentTeal, accentTeal.opacity(0.7)],
        startPoint: .topLeading, endPoint: .bottomTrailing
    )
    static let stopGradient = LinearGradient(
        colors: [accentCoral, accentCoral.opacity(0.7)],
        startPoint: .topLeading, endPoint: .bottomTrailing
    )
}
