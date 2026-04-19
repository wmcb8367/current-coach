import SwiftUI

struct ContentView: View {
    let locationService: LocationService
    let store: MeasurementStore
    let auth: AuthStore
    let sync: SyncService
    let measureViewModel: MeasureViewModel

    @Environment(\.scenePhase) private var scenePhase

    @State private var selectedTab: Int = {
        Int(ProcessInfo.processInfo.environment["CC_INITIAL_TAB"] ?? "") ?? 0
    }()
    @State private var mapFocus: MapFocus?
    @State private var lastMeasurementCount: Int = 0

    var body: some View {
        TabView(selection: $selectedTab) {
            MeasureView(viewModel: measureViewModel)
                .tag(0)
                .tabItem { Label("Measure", systemImage: "gauge.with.dots.needle.33percent") }

            MeasurementListView(store: store, onSelectMeasurement: { measurement in
                mapFocus = MapFocus(measurement: measurement)
                selectedTab = 2
            })
            .tag(1)
            .tabItem { Label("List", systemImage: "rectangle.stack") }

            MeasurementMapView(
                store: store,
                locationService: locationService,
                focus: $mapFocus
            )
            .tag(2)
            .tabItem { Label("Map", systemImage: "map") }

            AccountView(auth: auth, sync: sync)
                .tag(3)
                .tabItem { Label("Account", systemImage: "person.crop.circle") }
        }
        .toolbarBackground(NT.bgPrimary, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .onChange(of: selectedTab) { _, newTab in
            if newTab != 2 { mapFocus = nil }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await auth.refreshPreferences() }
                sync.syncPending()
            }
        }
        .onChange(of: auth.isSignedIn) { _, signedIn in
            if signedIn {
                Task { await auth.refreshPreferences() }
                sync.syncPending()
            }
        }
        .onChange(of: store.measurements.count) { old, new in
            if new > old { sync.didAddMeasurement() }
            lastMeasurementCount = new
        }
        .onAppear {
            lastMeasurementCount = store.measurements.count
            sync.syncPending()
        }
    }
}
