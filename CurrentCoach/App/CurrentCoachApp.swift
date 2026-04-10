import SwiftUI

@main
struct CurrentCoachApp: App {
    @State private var locationService = LocationService()
    @State private var store = MeasurementStore()

    init() {
        UITabBar.appearance().unselectedItemTintColor = UIColor(white: 0.45, alpha: 1.0)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(
                locationService: locationService,
                store: store,
                measureViewModel: MeasureViewModel(
                    locationService: locationService,
                    store: store
                )
            )
            .preferredColorScheme(.dark)
            .tint(NT.accentTeal)
        }
    }
}
