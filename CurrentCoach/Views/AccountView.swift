import SwiftUI

struct AccountView: View {
    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 20) {
                Spacer()

                Image(systemName: "safari")
                    .font(.system(size: 60))
                    .foregroundStyle(NT.accentTeal)
                    .shadow(color: NT.accentTeal.opacity(0.3), radius: 20)

                Text("Account")
                    .font(.largeTitle.weight(.bold))
                    .foregroundStyle(NT.textPrimary)

                Text("Sign in and manage your account.")
                    .foregroundStyle(NT.textSecondary)

                Button("Sign In") {
                    // Placeholder
                }
                .font(.headline)
                .foregroundStyle(.white)
                .padding(.horizontal, 40)
                .padding(.vertical, 14)
                .background(
                    Capsule()
                        .fill(NT.startGradient)
                        .overlay(Capsule().stroke(NT.accentTeal.opacity(0.5), lineWidth: 1))
                )

                Spacer()
            }
        }
    }
}
