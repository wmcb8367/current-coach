import SwiftUI
import MapKit

/// UIKit MKMapView wrapper that supports OpenSeaMap tile overlay for nautical charts.
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
        // Update chart overlay
        let hasOverlay = mapView.overlays.contains { $0 is MKTileOverlay }
        if showChartOverlay && !hasOverlay {
            // OpenSeaMap seamark tiles (transparent overlay on base map)
            let seamarkOverlay = MKTileOverlay(urlTemplate: "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png")
            seamarkOverlay.canReplaceMapContent = false
            seamarkOverlay.maximumZ = 18
            seamarkOverlay.minimumZ = 9
            mapView.addOverlay(seamarkOverlay, level: .aboveLabels)
        } else if !showChartOverlay && hasOverlay {
            let tileOverlays = mapView.overlays.filter { $0 is MKTileOverlay }
            mapView.removeOverlays(tileOverlays)
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
            let view = mapView.dequeueReusableAnnotationView(withIdentifier: identifier) ?? MKAnnotationView(annotation: annotation, reuseIdentifier: identifier)

            view.annotation = annotation

            let measurement = currentAnnotation.measurement
            let renderer = UIHostingController(rootView: CurrentArrowOverlay(measurement: measurement))
            renderer.view.backgroundColor = .clear
            renderer.view.frame = CGRect(x: 0, y: 0, width: 100, height: 80)
            renderer.view.sizeToFit()

            // Snapshot the SwiftUI view into an image
            let size = renderer.view.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize)
            renderer.view.frame = CGRect(origin: .zero, size: size)
            let image = UIGraphicsImageRenderer(size: size).image { _ in
                renderer.view.drawHierarchy(in: CGRect(origin: .zero, size: size), afterScreenUpdates: true)
            }

            view.image = image
            view.centerOffset = CGPoint(x: 0, y: -size.height / 2)
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
