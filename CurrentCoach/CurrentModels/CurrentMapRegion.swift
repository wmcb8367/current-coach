import Foundation

/// The part of the world a map shows: its centre, and how many degrees it
/// spans from south to north and from west to east. The zoom buttons on the
/// model map work on this, so the arithmetic can be tested without a map.
struct CurrentMapRegion: Sendable, Equatable {
    var centerLatitude: Double
    var centerLongitude: Double
    var latitudeSpan: Double
    var longitudeSpan: Double

    /// One press of a zoom button halves or doubles what is in view.
    static let step = 2.0

    init(centerLatitude: Double, centerLongitude: Double, latitudeSpan: Double, longitudeSpan: Double) {
        self.centerLatitude = centerLatitude
        self.centerLongitude = centerLongitude
        self.latitudeSpan = latitudeSpan
        self.longitudeSpan = longitudeSpan
    }

    /// The whole of a model's grid.
    init(fitting grid: CurrentMapGrid) {
        self.init(
            centerLatitude: 0.5 * (grid.south + grid.north),
            centerLongitude: 0.5 * (grid.west + grid.east),
            latitudeSpan: grid.north - grid.south,
            longitudeSpan: grid.east - grid.west
        )
    }

    /// The closest the buttons go: two cells of the model's grid from top to
    /// bottom. Closer than that there is nothing more of the model to see.
    static func closest(_ grid: CurrentMapGrid) -> Double {
        2 * (grid.north - grid.south) / Double(max(grid.rows - 1, 1))
    }

    /// The widest the buttons go: eight times the venue, enough to see where it lies.
    static func widest(_ grid: CurrentMapGrid) -> Double {
        min(8 * (grid.north - grid.south), 120)
    }

    func canZoomIn(_ grid: CurrentMapGrid) -> Bool {
        latitudeSpan > Self.closest(grid) * 1.01
    }

    func canZoomOut(_ grid: CurrentMapGrid) -> Bool {
        latitudeSpan < Self.widest(grid) / 1.01
    }

    /// The same centre seen closer (`closer` true) or from further away, by
    /// one step, held between the closest and widest views of the grid.
    func zoomed(closer: Bool, grid: CurrentMapGrid) -> CurrentMapRegion {
        guard latitudeSpan > 0, longitudeSpan > 0, latitudeSpan.isFinite, longitudeSpan.isFinite else {
            return CurrentMapRegion(fitting: grid)
        }
        let low = Self.closest(grid)
        let high = Self.widest(grid)
        guard low > 0, high > low else { return self }
        let wanted = closer ? latitudeSpan / Self.step : latitudeSpan * Self.step
        // A view already beyond a limit (reached by pinching) is not pulled back by the button that points further out.
        let target = closer ? max(wanted, min(low, latitudeSpan)) : min(wanted, max(high, latitudeSpan))
        let factor = target / latitudeSpan
        var next = self
        next.latitudeSpan = target
        next.longitudeSpan = min(longitudeSpan * factor, 360)
        return next
    }
}
