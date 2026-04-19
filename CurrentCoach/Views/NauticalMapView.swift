import SwiftUI
import MapKit

// MARK: - Chart providers

/// Region-aware chart layer identifiers. The app picks the best data source for
/// the user's current map center and composes layers automatically — the UI
/// only exposes a single "Charts" toggle.
enum ChartLayer: String {
    /// OpenSeaMap seamarks (buoys, beacons, nav marks). Transparent overlay, global.
    /// Also carries GEBCO-derived depth contours at z≥12 where no better source exists.
    case openSeaMapSeamark

    /// EMODnet bathymetry — higher-resolution (~115 m) depth shading for European seas.
    /// Opaque base-style tiles; sits under the seamarks.
    case emodnetBathymetry

    var urlTemplate: String {
        switch self {
        case .openSeaMapSeamark:
            return "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"
        case .emodnetBathymetry:
            // v12 RESTful XYZ, multicolour depth shading (red=shallow → blue=deep),
            // transparent over land so Apple's base map still shows coastline/labels.
            // Verified 2026-04: returns 256×256 RGBA PNG.
            return "https://tiles.emodnet-bathymetry.eu/v12/mean_multicolour/web_mercator/{z}/{x}/{y}.png"
        }
    }

    var minZ: Int {
        switch self {
        case .openSeaMapSeamark: return 9
        case .emodnetBathymetry: return 2
        }
    }

    var maxZ: Int {
        switch self {
        case .openSeaMapSeamark: return 18
        case .emodnetBathymetry: return 12
        }
    }

    /// Lower layers paint below higher ones. Bathymetry is a base; seamarks sit on top.
    var level: MKOverlayLevel {
        switch self {
        case .emodnetBathymetry: return .aboveRoads
        case .openSeaMapSeamark: return .aboveLabels
        }
    }

    var canReplaceMapContent: Bool {
        switch self {
        case .emodnetBathymetry: return false  // keep Apple labels visible
        case .openSeaMapSeamark: return false
        }
    }

    /// Stable identity for diffing the overlay set between updates.
    var id: String { rawValue }

    /// Rough bounding box in which this provider has genuinely better data than the global default.
    /// Used for region-based selection.
    func covers(_ coordinate: CLLocationCoordinate2D) -> Bool {
        switch self {
        case .openSeaMapSeamark:
            return true // global
        case .emodnetBathymetry:
            // EMODnet covers all European marine waters: Mediterranean, Baltic,
            // North Sea, Atlantic margin, Black Sea, Arctic margin.
            return coordinate.latitude >= 24 && coordinate.latitude <= 90
                && coordinate.longitude >= -42 && coordinate.longitude <= 55
        }
    }
}

/// MKTileOverlay subclass that carries its layer identity so we can diff
/// overlays on update without reading URLs.
final class IdentifiedTileOverlay: MKTileOverlay {
    let layer: ChartLayer
    init(layer: ChartLayer) {
        self.layer = layer
        super.init(urlTemplate: layer.urlTemplate)
        self.canReplaceMapContent = layer.canReplaceMapContent
        self.minimumZ = layer.minZ
        self.maximumZ = layer.maxZ
    }
}

/// Pick the best chart composition for the given map center. Seamarks are always
/// present when charts are enabled; bathymetry adds EMODnet where it helps.
func chartLayers(for coordinate: CLLocationCoordinate2D) -> [ChartLayer] {
    var layers: [ChartLayer] = []
    if ChartLayer.emodnetBathymetry.covers(coordinate) {
        layers.append(.emodnetBathymetry)
    }
    layers.append(.openSeaMapSeamark)
    return layers
}

// MARK: - Map view

/// UIKit MKMapView wrapper that composes region-aware nautical chart overlays
/// and renders current-measurement arrows as annotations.
struct NauticalMapView: UIViewRepresentable {
    let measurements: [TideMeasurement]
    let showChartOverlay: Bool
    @Binding var region: MKCoordinateRegion

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeUIView(context: Context) -> MKMapView {
        let mapView = MKMapView()
        mapView.delegate = context.coordinator
        mapView.showsUserLocation = true
        mapView.setRegion(region, animated: false)
        return mapView
    }

    func updateUIView(_ mapView: MKMapView, context: Context) {
        // Reconcile chart overlays against the region-aware desired set.
        let desired: [ChartLayer] = showChartOverlay ? chartLayers(for: mapView.region.center) : []
        let desiredSet = Set(desired.map(\.id))

        // Remove overlays we no longer want. Only touch our own tile overlays.
        for overlay in mapView.overlays {
            guard let tiled = overlay as? IdentifiedTileOverlay else { continue }
            if !desiredSet.contains(tiled.layer.id) {
                mapView.removeOverlay(tiled)
            }
        }

        let existingSet = Set(mapView.overlays.compactMap { ($0 as? IdentifiedTileOverlay)?.layer.id })
        for layer in desired where !existingSet.contains(layer.id) {
            let overlay = IdentifiedTileOverlay(layer: layer)
            mapView.addOverlay(overlay, level: layer.level)
        }

        // Update annotations
        mapView.removeAnnotations(mapView.annotations.filter { $0 is CurrentAnnotation })
        let annotations = measurements.map { CurrentAnnotation(measurement: $0) }
        mapView.addAnnotations(annotations)

        // Update region if significantly different
        let currentCenter = mapView.region.center
        let newCenter = region.center
        let delta = abs(currentCenter.latitude - newCenter.latitude) + abs(currentCenter.longitude - newCenter.longitude)
        if delta > 0.001 {
            mapView.setRegion(region, animated: true)
        }
    }

    // MARK: - Coordinator

    final class Coordinator: NSObject, MKMapViewDelegate {
        let parent: NauticalMapView

        init(_ parent: NauticalMapView) {
            self.parent = parent
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: any MKOverlay) -> MKOverlayRenderer {
            if let tileOverlay = overlay as? MKTileOverlay {
                return MKTileOverlayRenderer(tileOverlay: tileOverlay)
            }
            return MKOverlayRenderer(overlay: overlay)
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: any MKAnnotation) -> MKAnnotationView? {
            guard let currentAnnotation = annotation as? CurrentAnnotation else { return nil }

            let identifier = "CurrentArrow"
            let view = mapView.dequeueReusableAnnotationView(withIdentifier: identifier)
                ?? MKAnnotationView(annotation: annotation, reuseIdentifier: identifier)
            view.annotation = annotation

            // Render the SwiftUI overlay into a UIImage via SwiftUI's ImageRenderer.
            // The prior UIHostingController + drawHierarchy path produced 0×0 images
            // because the hosting controller was never in a window.
            let overlay = CurrentArrowOverlay(measurement: currentAnnotation.measurement)
            let renderer = ImageRenderer(content: overlay)
            renderer.scale = UIScreen.main.scale
            if let image = renderer.uiImage {
                view.image = image
                view.centerOffset = CGPoint(x: 0, y: -image.size.height / 2)
            }
            return view
        }

        func mapView(_ mapView: MKMapView, regionDidChangeAnimated animated: Bool) {
            parent.region = mapView.region
        }
    }
}

// MARK: - Annotation Model

final class CurrentAnnotation: NSObject, MKAnnotation {
    let measurement: TideMeasurement
    let coordinate: CLLocationCoordinate2D

    init(measurement: TideMeasurement) {
        self.measurement = measurement
        self.coordinate = measurement.coordinate
        super.init()
    }
}

// MARK: - Arrow SwiftUI overlay for snapshot

private struct CurrentArrowOverlay: View {
    let measurement: TideMeasurement

    private var arrowColor: Color {
        let speed = measurement.speedMetersPerMinute
        if speed < 3 { return NT.accentTeal }
        if speed < 6 { return NT.accentAmber }
        return NT.accentCoral
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Image(systemName: "location.north.fill")
                .font(.title3)
                .fontWeight(.bold)
                .foregroundStyle(arrowColor)
                .rotationEffect(.degrees(measurement.flowDirection))

            VStack(alignment: .leading, spacing: 0) {
                Text(measurement.timeFormatted)
                    .font(.caption2)
                    .fontWeight(.bold)
                    .foregroundStyle(NT.accentAmber)
                Text(String(format: "%.1fm/min", measurement.speedMetersPerMinute))
                    .font(.caption2)
                    .foregroundStyle(.white)
                Text(String(format: "From:%.0f°", measurement.fromDirection))
                    .font(.caption2)
                    .foregroundStyle(.white)
            }
            .padding(3)
            .background(
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(red: 0.10, green: 0.13, blue: 0.18).opacity(0.9))
            )
        }
    }
}
