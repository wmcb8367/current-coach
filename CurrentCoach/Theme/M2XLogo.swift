import SwiftUI

struct M2XLogo: View {
    var height: CGFloat = 28
    var color: Color = NT.textPrimary

    var body: some View {
        Image("M2XLogo")
            .renderingMode(.template)
            .resizable()
            .scaledToFit()
            .foregroundStyle(color)
            .frame(height: height)
            .accessibilityLabel("M2X")
    }
}

#Preview {
    VStack(spacing: 20) {
        M2XLogo(height: 32)
        M2XLogo(height: 56)
    }
    .padding()
    .background(NT.bgPrimary)
}
