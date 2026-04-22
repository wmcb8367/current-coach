import SwiftUI
import MapKit

// MARK: - Chart providers

/// Region-aware chart layer identifiers. The app picks the best data source for
/// the user's current map center and composes layers automatically - the UI
/// only exposes a single "Charts" toggle.
enum ChartLayer: String {
    /// OpenSeaMap seamarks (buoys, beacons, nav marks). Transparent overlay, global.
    case openSeaMapSeamark

    /// EMODnet bathymetry - higher-resolution (~115 m) depth shading for European seas.
    case emodnetBathymetry

    /// NOAA ENC chart display - traditional paper-chart symbology for US waters.
    /// WMS served as XYZ-style tiles via bbox substitution.
    case noaaChart

    var urlTemplate: String {
        switch self {
        case .openSeaMapSeamark:
            return "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"
        case .emodnetBathymetry:
            return "https://tiles.emodnet-bathymetry.eu/v12/mean_multicolour/web_mercator/{z}/{x}/{y}.png"
        case .noaaChart:
            // NOAA Chart Display Service - WMS endpoint. MKTileOverlay will
            // substitute {z}/{x}/{y}; we override loadTile to build the
            // correct WMS bbox request instead.
            return "https://gis.charttools.noaa.gov/arcgis/rest/services/MCS/NOAAChartDisplay/MapServer/exts/MaritimeChartService/WMSServer"
        }
    }

    var minZ: Int {
        switch self {
        case .openSeaMapSeamark: return 9
        case .emodnetBathymetry: return 2
        case .noaaChart: return 8
        }
    }

    var maxZ: Int {
        switch self {
        case .openSeaMapSeamark: return 18
        case .emodnetBathymetry: return 12
        case .noaaChart: return 18
        }
    }

    var level: MKOverlayLevel {
        switch self {
        case .emodnetBathymetry: return .aboveRoads
        case .noaaChart: return .aboveRoads
        case .openSeaMapSeamark: return .aboveLabels
        }
    }

    var canReplaceMapContent: Bool {
        switch self {
        case .emodnetBathymetry: return false
        case .noaaChart: return false
        case .openSeaMapSeamark: return false
        }
    }

    var id: String { rawValue }

    func covers(_ coordinate: CLLocationCoordinate2D) -> Bool {
        switch self {
        case .openSeaMapSeamark:
            return true
        case .emodnetBathymetry:
            return coordinate.latitude >= 24 && coordinate.latitude <= 90
                && coordinate.longitude >= -42 && coordinate.longitude <= 55
        case .noaaChart:
            // NOAA ENC covers US waters (incl. territories, Great Lakes)
            return coordinate.latitude >= 17 && coordinate.latitude <= 72
                && coordinate.longitude >= -180 && coordinate.longitude <= -60
        }
    }
}

/// MKTileOverlay subclass that carries its layer identity so we can diff
/// overlays on update without reading URLs.
final class IdentifiedTileOverlay: MKTileOverlay {
    let layer: ChartLayer
    init(layer: ChartLayer) {
        self.layer = layer
        let template = layer == .noaaChart ? nil : layer.urlTemplate
        super.init(urlTemplate: template)
        self.canReplaceMapContent = layer.canReplaceMapContent
        self.minimumZ = layer.minZ
        self.maximumZ = layer.maxZ
        self.tileSize = CGSize(width: 256, height: 256)
    }

    override func url(forTilePath path: MKTileOverlayPath) -> URL {
        guard layer == .noaaChart else {
            return super.url(forTilePath: path)
        }
        // Convert tile x/y/z to EPSG:3857 bbox for WMS request
        let n = Double(1 << path.z)
        let tileX = Double(path.x)
        let tileY = Double(path.y)
        let originShift = 20037508.342789244
        let tileSize = 2.0 * originShift / n
        let minX = -originShift + tileX * tileSize
        let maxX = minX + tileSize
        let maxY = originShift - tileY * tileSize
        let minY = maxY - tileSize
        let bbox = "\(minX),\(minY),\(maxX),\(maxY)"
        let urlStr = "\(layer.urlTemplate)?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&FORMAT=image/png&TRANSPARENT=true&LAYERS=0,1,2,3,4,5,6,7&SRS=EPSG:3857&WIDTH=256&HEIGHT=256&BBOX=\(bbox)"
        return URL(string: urlStr)!
    }
}

/// Pick the best chart composition for the given map center. Seamarks are always
/// present when charts are enabled; bathymetry adds EMODnet where it helps.
func chartLayers(for coordinate: CLLocationCoordinate2D) -> [ChartLayer] {
    var layers: [ChartLayer] = []
    if ChartLayer.emodnetBathymetry.covers(coordinate) {
        layers.append(.emodnetBathymetry)
    }
    if ChartLayer.noaaChart.covers(coordinate) {
        layers.append(.noaaChart)
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
        let speeds = measurements.map(\.speedMetersPerMinute)
        let minSpd = speeds.min() ?? 0
        let maxSpd = speeds.max() ?? 1
        let range = max(maxSpd - minSpd, 0.001)
        let annotations = measurements.map { m in
            CurrentAnnotation(
                measurement: m,
                speedFraction: (m.speedMetersPerMinute - minSpd) / range
            )
        }
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
            let overlay = CurrentArrowOverlay(
                measurement: currentAnnotation.measurement,
                speedFraction: currentAnnotation.speedFraction
            )
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
    var speedFraction: Double = 0.5

    init(measurement: TideMeasurement, speedFraction: Double = 0.5) {
        self.measurement = measurement
        self.coordinate = measurement.coordinate
        self.speedFraction = speedFraction
        super.init()
    }
}

// MARK: - Arrow SwiftUI overlay for snapshot

/// Speed → color matching the M2X web portal's colorForSpeed().
/// blue(210°) → cyan → green → yellow → red(0°).
/// Uses HSL with same formula as web: hue=210-t*210, sat=75+t*15, lum=50+(1-abs(t-0.5)*2)*15
/// Plus a sqrt() curve to spread out the low end for better visibility of small current differences.
func colorForSpeed(fraction: Double) -> Color {
    let t = max(0, min(1, fraction))
    // sqrt curve spreads out small speed differences at the low end
    let curved = sqrt(t)
    // Match web portal HSL: hue = 210 - t*210, sat = 75 + t*15, lum = 50 + (1-abs(t-0.5)*2)*15
    let hDeg = 210.0 - curved * 210.0
    let hue = hDeg / 360.0  // SwiftUI hue is 0..1
    let satPct = (75.0 + curved * 15.0) / 100.0
    let lumPct = (50.0 + (1.0 - abs(curved - 0.5) * 2.0) * 15.0) / 100.0
    // Convert HSL → HSB for SwiftUI Color(hue:saturation:brightness:)
    let b = lumPct + satPct * min(lumPct, 1.0 - lumPct)
    let s = b > 0 ? 2.0 * (1.0 - lumPct / b) : 0.0
    return Color(hue: hue, saturation: s, brightness: b)
}

private struct CurrentArrowOverlay: View {
    let measurement: TideMeasurement
    var speedFraction: Double = 0.5  // 0..1 relative to session range

    private var arrowColor: Color {
        colorForSpeed(fraction: speedFraction)
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
