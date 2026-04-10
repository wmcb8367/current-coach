import SwiftUI

struct ContentView: View {
    let locationService: LocationService
    let store: MeasurementStore
    let measureViewModel: MeasureViewModel

    var body: some View {
        TabView {
            MeasureView(viewModel: measureViewModel)
                .tabItem {
                    Label("Measure", systemImage: "gauge.with.dots.needle.33percent")
                }

            MeasurementListView(store: store)
                .tabItem {
                    Label("List", systemImage: "rectangle.stack")
                }

            MeasurementMapView(store: store, locationService: locationService)
                .tabItem {
                    Label("Map", systemImage: "map")
                }

            AccountView()
                .tabItem {
                    Label("Account", systemImage: "person.crop.circle")
                }
        }
        .toolbarBackground(NT.bgPrimary, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
    }
}
