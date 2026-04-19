import SwiftUI

enum AppTab: Int, CaseIterable, Identifiable {
    case measure = 0
    case list = 1
    case map = 2
    case account = 3

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .measure: return "Measure"
        case .list: return "List"
        case .map: return "Map"
        case .account: return "Account"
        }
    }

    var icon: String {
        switch self {
        case .measure: return "gauge.with.dots.needle.33percent"
        case .list: return "rectangle.stack"
        case .map: return "map"
        case .account: return "person.crop.circle"
        }
    }
}

struct ContentView: View {
    let locationService: LocationService
    let store: MeasurementStore
    let auth: AuthStore
    let sync: SyncService
    let measureViewModel: MeasureViewModel

    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    @State private var selectedTab: AppTab = {
        let raw = Int(ProcessInfo.processInfo.environment["CC_INITIAL_TAB"] ?? "") ?? 0
        return AppTab(rawValue: raw) ?? .measure
    }()
    @State private var mapFocus: MapFocus?
    @State private var lastMeasurementCount: Int = 0

    var body: some View {
        Group {
            if horizontalSizeClass == .regular {
                iPadLayout
            } else {
                iPhoneLayout
            }
        }
        .onChange(of: selectedTab) { _, newTab in
            if newTab != .map { mapFocus = nil }
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

    // MARK: - iPhone layout (unchanged TabView)

    private var iPhoneLayout: some View {
        TabView(selection: Binding(
            get: { selectedTab.rawValue },
            set: { selectedTab = AppTab(rawValue: $0) ?? .measure }
        )) {
            MeasureView(viewModel: measureViewModel)
                .tag(AppTab.measure.rawValue)
                .tabItem { Label(AppTab.measure.title, systemImage: AppTab.measure.icon) }

            MeasurementListView(store: store, onSelectMeasurement: { measurement in
                mapFocus = MapFocus(measurement: measurement)
                selectedTab = .map
            })
            .tag(AppTab.list.rawValue)
            .tabItem { Label(AppTab.list.title, systemImage: AppTab.list.icon) }

            MeasurementMapView(
                store: store,
                locationService: locationService,
                focus: $mapFocus
            )
            .tag(AppTab.map.rawValue)
            .tabItem { Label(AppTab.map.title, systemImage: AppTab.map.icon) }

            AccountView(auth: auth, sync: sync)
                .tag(AppTab.account.rawValue)
                .tabItem { Label(AppTab.account.title, systemImage: AppTab.account.icon) }
        }
        .toolbarBackground(NT.bgPrimary, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
    }

    // MARK: - iPad layout (sidebar + detail)

    private var iPadLayout: some View {
        NavigationSplitView {
            iPadSidebar
                .navigationSplitViewColumnWidth(min: 220, ideal: 240, max: 280)
        } detail: {
            iPadDetail
                .navigationBarTitleDisplayMode(.inline)
        }
        .navigationSplitViewStyle(.balanced)
        .tint(NT.accentTeal)
    }

    private var iPadSidebar: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 10) {
                    M2XLogo(height: 26)
                    Rectangle()
                        .fill(NT.borderSubtle)
                        .frame(width: 1, height: 20)
                    Text("Current Coach")
                        .eyebrow(NT.textSecondary)
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 20)

                VStack(spacing: 4) {
                    ForEach(AppTab.allCases) { tab in
                        SidebarRow(
                            tab: tab,
                            isSelected: selectedTab == tab,
                            onTap: { selectedTab = tab }
                        )
                    }
                }
                .padding(.horizontal, 12)

                Spacer()

                Text("v1.2.0 · M2X")
                    .font(.caption)
                    .foregroundStyle(NT.textFaint)
                    .padding(.horizontal, 20)
                    .padding(.bottom, 16)
            }
        }
    }

    @ViewBuilder
    private var iPadDetail: some View {
        switch selectedTab {
        case .measure:
            MeasureView(viewModel: measureViewModel)
        case .list:
            MeasurementListView(store: store, onSelectMeasurement: { measurement in
                mapFocus = MapFocus(measurement: measurement)
                selectedTab = .map
            })
        case .map:
            MeasurementMapView(
                store: store,
                locationService: locationService,
                focus: $mapFocus
            )
        case .account:
            AccountView(auth: auth, sync: sync)
        }
    }
}

private struct SidebarRow: View {
    let tab: AppTab
    let isSelected: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 14) {
                Image(systemName: tab.icon)
                    .font(.system(size: 18, weight: .semibold))
                    .frame(width: 24)
                    .foregroundStyle(isSelected ? NT.accentTeal : NT.textSecondary)
                Text(tab.title)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(isSelected ? NT.textPrimary : NT.textSecondary)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(isSelected ? NT.accentTeal.opacity(0.14) : Color.clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(isSelected ? NT.accentTeal.opacity(0.35) : Color.clear, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
    }
}
