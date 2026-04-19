import SwiftUI

@main
struct CurrentCoachApp: App {
    @State private var locationService = LocationService()
    @State private var store = MeasurementStore()
    @State private var auth = AuthStore()
    @State private var sync: SyncService

    init() {
        UITabBar.appearance().unselectedItemTintColor = UIColor(white: 0.45, alpha: 1.0)
        let locationService = LocationService()
        let store = MeasurementStore()
        let auth = AuthStore()
        _locationService = State(initialValue: locationService)
        _store = State(initialValue: store)
        _auth = State(initialValue: auth)
        _sync = State(initialValue: SyncService(store: store, auth: auth))
    }

    var body: some Scene {
        WindowGroup {
            ContentView(
                locationService: locationService,
                store: store,
                auth: auth,
                sync: sync,
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
