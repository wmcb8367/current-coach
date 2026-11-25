import Foundation
import Testing
@testable import CurrentCoach

/// A map file built the way the publisher builds it: 3 rows by 4 columns,
/// east-going current 0.1 m/s times (hour + 1), north-going 0.1 m/s times the column.
private func mapFile(frames: Int, flags: UInt16 = 0, land: [(row: Int, col: Int)] = []) -> (header: Data, body: Data) {
    let rows = 3
    let cols = 4
    var body = Data()
    func append(_ value: Int16) {
        let bits = UInt16(bitPattern: value)
        body.append(UInt8(bits & 0xFF))
        body.append(UInt8(bits >> 8))
    }
    for frame in 0..<frames {
        for plane in 0..<2 {
            for row in 0..<rows {
                for col in 0..<cols {
                    if land.contains(where: { $0.row == row && $0.col == col }) {
                        append(-32768)
                    } else {
                        append(Int16(plane == 0 ? 100 * (frame + 1) : 100 * col))
                    }
                }
            }
        }
    }
    var header = Data("CCM1".utf8)
    for word in [UInt16(rows), UInt16(cols), UInt16(frames), flags] {
        header.append(UInt8(word & 0xFF))
        header.append(UInt8(word >> 8))
    }
    let scale = Float(0.001).bitPattern
    for shift in [0, 8, 16, 24] {
        header.append(UInt8((scale >> UInt32(shift)) & 0xFF))
    }
    return (header, body)
}

private let grid = CurrentMapGrid(south: 22.0, north: 22.2, west: 114.0, east: 114.3, rows: 3, cols: 4)

private func close(_ a: Double, _ b: Double, _ tolerance: Double = 1e-5) -> Bool {
    abs(a - b) <= tolerance
}

@Suite("Current model maps")
struct CurrentModelMapTests {

    @Test("A published manifest decodes, including the default model")
    func manifestDecodes() throws {
        let json = """
        {"version":1,"venue":{"id":"hong-kong","name":"Hong Kong","lat":22.28,"lon":114.36,"timeZone":"Asia/Hong_Kong",
        "south":22.109321,"north":22.450679,"west":114.146427,"east":114.573573,"rows":128,"cols":148,
        "mask":{"detail":4,"rows":509,"cols":589,"data":"AAEC"}},
        "frameSeconds":3600,"framesPerDay":25,"updated":1795654000,"default":"day-ahead",
        "models":[{"id":"day-ahead","name":"Physics + wind","summary":"Physics plus wind.","factors":["tide","forecast wind"],
        "days":[{"day":"2026-11-24","kind":"forecast","issued":1795514400,"bytes":326000},
                {"day":"2026-11-26","kind":"forecast","issued":1795600800,"bytes":330000}]},
        {"id":"tide","name":"Tide only","summary":"Tide.","factors":["tide"],
        "days":[{"day":"2026-11-25","kind":"analysis","issued":1795600800,"bytes":280000}]}],
        "attribution":["NOAA STOFS-2D-Global ocean model"]}
        """
        let manifest = try JSONDecoder().decode(CurrentModelManifest.self, from: Data(json.utf8))
        #expect(manifest.defaultModel == "day-ahead")
        #expect(manifest.venue.grid.rows == 128 && manifest.venue.grid.cols == 148)
        #expect(manifest.venue.timeZone == "Asia/Hong_Kong")
        #expect(manifest.venue.mask?.detail == 4 && manifest.venue.mask?.cols == 589)
        #expect(manifest.days == ["2026-11-24", "2026-11-25", "2026-11-26"])
        #expect(manifest.model("day-ahead")?.entry(for: "2026-11-26")?.kind == "forecast")
        #expect(manifest.model("tide")?.entry(for: "2026-11-24") == nil)
        #expect(manifest.model("missing") == nil)
    }

    @Test("Days are UTC days")
    func daysAreUTC() {
        let moment = Date(timeIntervalSince1970: 1_795_491_000) // 2026-11-24 03:30 UTC
        #expect(CurrentDay.name(for: moment) == "2026-11-24")
        let split = CurrentDay.split(moment)
        #expect(split.day == "2026-11-24" && close(split.hours, 3.5))
        #expect(CurrentDay.start(of: "2026-11-24") == Date(timeIntervalSince1970: 1_795_478_400))
        #expect(CurrentDay.name(for: Date(timeIntervalSince1970: 1_795_478_399)) == "2026-11-23")
        #expect(CurrentDay.start(of: "yesterday") == nil)
    }

    @Test("A map file decodes, plain or compressed")
    func fileDecodes() throws {
        let plain = mapFile(frames: 25)
        let field = try CurrentDayField(data: plain.header + plain.body)
        #expect(field.rows == 3 && field.cols == 4 && field.frames == 25)
        #expect(close(field.scale, 0.001, 1e-9))
        #expect(field.units.count == 25 * 2 * 3 * 4)

        // The server sends the body deflated; the app hands in the inflater.
        let packed = mapFile(frames: 25, flags: 1)
        let marker = Data([1, 2, 3])
        let inflated = try CurrentDayField(data: packed.header + marker) { body in
            #expect(body == marker)
            return plain.body
        }
        #expect(inflated == field)
        #expect(throws: CurrentFieldError.compressedWithoutInflater) {
            try CurrentDayField(data: packed.header + marker)
        }
    }

    @Test("Files that are not maps, or are cut short, are refused")
    func badFilesAreRefused() {
        #expect(throws: CurrentFieldError.notACurrentMap) {
            try CurrentDayField(data: Data("not a map file at all".utf8))
        }
        let plain = mapFile(frames: 25)
        #expect(throws: CurrentFieldError.wrongSize) {
            try CurrentDayField(data: plain.header + plain.body.prefix(100))
        }
        #expect(throws: CurrentFieldError.notACurrentMap) {
            try CurrentDayField(data: Data())
        }
    }

    @Test("The current is weighted between grid points and between hours")
    func samplingInterpolates() throws {
        let file = mapFile(frames: 25)
        let field = try CurrentDayField(data: file.header + file.body)

        let corner = try #require(field.velocity(grid: grid, latitude: 22.0, longitude: 114.0, hours: 0))
        #expect(close(corner.u, 0.1) && close(corner.v, 0))

        // Half way between columns 1 and 2, half way between 02:00 and 03:00.
        let middle = try #require(field.velocity(grid: grid, latitude: 22.1, longitude: 114.15, hours: 2.5))
        #expect(close(middle.u, 0.35) && close(middle.v, 0.15))

        // The last frame answers for 24:00, and nothing answers beyond the day.
        let last = try #require(field.velocity(grid: grid, latitude: 22.1, longitude: 114.15, hours: 24))
        #expect(close(last.u, 2.5))
        #expect(field.velocity(grid: grid, latitude: 22.1, longitude: 114.15, hours: 24.01) == nil)
        #expect(field.velocity(grid: grid, latitude: 22.1, longitude: 114.15, hours: -0.1) == nil)
    }

    @Test("There is no answer off the map or on land, and the shore leans on the wet side")
    func landAndEdges() throws {
        let file = mapFile(frames: 2, land: [(0, 0), (0, 1), (1, 0), (1, 1)])
        let field = try CurrentDayField(data: file.header + file.body)
        #expect(field.velocity(grid: grid, latitude: 21.99, longitude: 114.1, hours: 0) == nil)
        #expect(field.velocity(grid: grid, latitude: 22.1, longitude: 114.31, hours: 0) == nil)
        #expect(field.velocity(grid: grid, latitude: 22.05, longitude: 114.05, hours: 0) == nil)
        let shore = try #require(field.velocity(grid: grid, latitude: 22.1, longitude: 114.18, hours: 0))
        #expect(close(shore.u, 0.1) && close(shore.v, 0.2))
    }

    @Test("Speed and direction of a current")
    func vectorDirections() {
        let west = CurrentVector(u: -1852.0 / 3600.0, v: 0)
        #expect(close(west.speedKnots, 1.0))
        #expect(close(west.setDegrees, 270) && close(west.fromDegrees, 90))
        let north = CurrentVector(u: 0, v: 0.2)
        #expect(close(north.setDegrees, 0) && close(north.fromDegrees, 180))
        let southEast = CurrentVector(u: 0.1, v: -0.1)
        #expect(close(southEast.setDegrees, 135) && close(southEast.fromDegrees, 315))
    }

    @Test("The grid converts between places and grid coordinates")
    func gridCoordinates() {
        #expect(close(grid.x(longitude: 114.15), 1.5) && close(grid.y(latitude: 22.1), 1.0))
        #expect(close(grid.longitude(x: 3), 114.3) && close(grid.latitude(y: 0), 22.0))
        #expect(grid.contains(latitude: 22.1, longitude: 114.1))
        #expect(!grid.contains(latitude: 22.3, longitude: 114.1))
        let metres = grid.metresPerCell
        #expect(close(metres.y, 11_132, 1) && metres.x > 10_000 && metres.x < 10_400)
    }
}

@Suite("Current map drawing")
struct CurrentMapDrawingTests {

    @Test("The colour scale runs from slack to strong and stops at its ends")
    func colourScale() {
        let scale = CurrentColorScale.standard
        #expect(scale.color(knots: -1) == scale.color(knots: 0))
        #expect(scale.color(knots: 99) == scale.color(knots: scale.maxKnots))
        #expect(scale.color(knots: .nan) == scale.color(knots: 0))
        // Half way between two stops is half way between their colours.
        let a = scale.color(knots: 1.0)
        let b = scale.color(knots: 1.5)
        let mid = scale.color(knots: 1.25)
        #expect(close(mid.red, 0.5 * (a.red + b.red)) && close(mid.green, 0.5 * (a.green + b.green)))
        #expect(scale.table().count == 256 * 3)
        // Slack water is dark and blue; a knot is bright and warm.
        let slack = scale.color(knots: 0)
        #expect(slack.blue > slack.red && a.red > a.blue)
    }

    @Test("The picture is clear over land and coloured over water")
    func rasterMasksLand() throws {
        let file = mapFile(frames: 2, land: [(0, 0), (0, 1), (1, 0), (1, 1)])
        let field = try CurrentDayField(data: file.header + file.body)
        let frame = try #require(field.frame(hours: 0.5))
        let raster = CurrentRaster.make(frame: frame, detail: 4, opacity: 0.8)
        #expect(raster.width == 13 && raster.height == 9)
        #expect(raster.pixels.count == 13 * 9 * 4)
        func alpha(x: Int, y: Int) -> UInt8 { raster.pixels[(y * raster.width + x) * 4 + 3] }
        // The top row of the picture is the north edge: all water.
        #expect(alpha(x: 0, y: 0) == 204 && alpha(x: 12, y: 0) == 204)
        // The bottom-left corner is the south-west: land.
        #expect(alpha(x: 0, y: 8) == 0 && alpha(x: 2, y: 6) == 0)
        // Colour is premultiplied, so no channel exceeds alpha.
        for index in stride(from: 0, to: raster.pixels.count, by: 4) {
            let a = raster.pixels[index + 3]
            #expect(raster.pixels[index] <= a && raster.pixels[index + 1] <= a && raster.pixels[index + 2] <= a)
        }
    }

    @Test("With a coastline the colour runs to the shore and stops there")
    func rasterFollowsTheCoastline() throws {
        // The model's water is the eastern half; the real shore is one grid cell further west.
        let rows = 3
        let cols = 5
        var u = [Float](repeating: 0.2, count: rows * cols)
        for row in 0..<rows { for col in 0..<2 { u[row * cols + col] = .nan } }
        let frame = CurrentFrame(rows: rows, cols: cols, u: u, v: [Float](repeating: 0, count: rows * cols))

        let filled = frame.filled(passes: 1)
        #expect(filled.u[1] == 0.2 && filled.u[0].isNaN)
        #expect(frame.filled(passes: 5).u.allSatisfy { $0 == 0.2 })
        #expect(frame.filled(passes: 0) == frame)

        // Coastline at four points per cell: 9 rows by 17 columns, water from column 4 eastward.
        let detail = 4
        let maskCols = (cols - 1) * detail + 1
        let maskRows = (rows - 1) * detail + 1
        let rowBits: [UInt8] = [0b0000_1111, 0b1111_1111, 0b1000_0000]
        let mask = try #require(CurrentWaterMask(detail: detail, rows: maskRows, cols: maskCols, bits: Array(repeating: rowBits, count: maskRows).flatMap { $0 }))
        #expect(!mask.isWater(row: 0, col: 3) && mask.isWater(row: 0, col: 4) && mask.isWater(row: 8, col: 16))
        #expect(!mask.isWater(row: 9, col: 4) && !mask.isWater(row: 0, col: 17))
        #expect(CurrentWaterMask(detail: detail, rows: maskRows, cols: maskCols, bits: [1, 2, 3]) == nil)

        let raster = CurrentRaster.make(frame: frame, detail: detail, opacity: 1, mask: mask)
        func alpha(x: Int, y: Int) -> UInt8 { raster.pixels[(y * raster.width + x) * 4 + 3] }
        // Without the coastline the colour would stop at the model's edge (column 8); with it, at the shore (column 4).
        #expect(alpha(x: 3, y: 4) == 0 && alpha(x: 4, y: 4) == 255 && alpha(x: 6, y: 4) == 255 && alpha(x: 16, y: 0) == 255)
        let plain = CurrentRaster.make(frame: frame, detail: detail, opacity: 1)
        let plainAlpha = plain.pixels[(4 * plain.width + 4) * 4 + 3]
        #expect(plainAlpha == 0)

        // A coastline of the wrong size is ignored rather than misdrawn.
        #expect(CurrentRaster.make(frame: frame, detail: 2, opacity: 1, mask: mask) == CurrentRaster.make(frame: frame, detail: 2, opacity: 1))
    }

    @Test("The coastline in the manifest unpacks")
    func maskFromManifest() throws {
        let bits: [UInt8] = [0b1010_0000, 0b0100_0000]
        let entry = CurrentModelManifest.Mask(detail: 2, rows: 2, cols: 3, data: Data([9, 9]).base64EncodedString())
        // The app hands in the inflater; here it stands in for raw DEFLATE.
        let mask = try #require(CurrentWaterMask(entry: entry) { packed in
            #expect(packed == Data([9, 9]))
            return Data(bits)
        })
        #expect(mask.isWater(row: 0, col: 0) && !mask.isWater(row: 0, col: 1) && mask.isWater(row: 0, col: 2))
        #expect(!mask.isWater(row: 1, col: 0) && mask.isWater(row: 1, col: 1))
        #expect(CurrentWaterMask(entry: entry) { _ in throw CurrentFieldError.wrongSize } == nil)
    }

    @Test("Streaks are carried downstream, stay on the water and inside their bounds")
    func particlesFollowTheCurrent() throws {
        // Water everywhere, flowing east at 0.2 m/s.
        let plane = 6 * 8
        let frame = CurrentFrame(rows: 6, cols: 8, u: [Float](repeating: 0.2, count: plane), v: [Float](repeating: 0, count: plane))
        let bounds = CurrentParticleSystem.Bounds(minX: 1, maxX: 6, minY: 1, maxY: 4)
        var system = CurrentParticleSystem(count: 50, seed: 7)
        system.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 2, y: 2))
        let dropped = system.particles
        #expect(dropped.allSatisfy { $0.trail.count == 1 })
        system.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 2, y: 2))
        for (before, after) in zip(dropped, system.particles) where after.trail.count == 2 {
            #expect(close(after.trail[1].x - before.trail[0].x, 0.04) && close(after.trail[1].y, before.trail[0].y))
        }
        for _ in 0..<400 {
            system.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 2, y: 2))
        }
        for particle in system.particles {
            #expect(particle.trail.count <= CurrentParticleSystem.trailLength)
            for point in particle.trail {
                #expect(point.x >= 1 && point.x <= 6 && point.y >= 1 && point.y <= 4)
            }
            #expect(particle.strength >= 0 && particle.strength <= 1)
        }

        // The same seed gives the same streaks.
        var first = CurrentParticleSystem(count: 20, seed: 3)
        var second = CurrentParticleSystem(count: 20, seed: 3)
        for _ in 0..<10 {
            first.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 2, y: 2))
            second.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 2, y: 2))
        }
        #expect(first.particles == second.particles)
    }

    @Test("No streak is dropped on land")
    func particlesAvoidLand() {
        let plane = 6 * 8
        var u = [Float](repeating: 0.1, count: plane)
        // The western half is land.
        for row in 0..<6 { for col in 0..<4 { u[row * 8 + col] = .nan } }
        let frame = CurrentFrame(rows: 6, cols: 8, u: u, v: [Float](repeating: 0, count: plane))
        let bounds = CurrentParticleSystem.Bounds(minX: 0, maxX: 7, minY: 0, maxY: 5)
        var system = CurrentParticleSystem(count: 200, seed: 11)
        for _ in 0..<30 {
            system.step(frame: frame, bounds: bounds, dt: 0.1, cellsPerSecond: (x: 1, y: 1))
        }
        #expect(system.particles.contains { !$0.trail.isEmpty })
        for particle in system.particles {
            for point in particle.trail { #expect(point.x >= 3.5) }
        }
        system.resize(to: 10)
        #expect(system.particles.count == 10)
        system.reset()
        #expect(system.particles.allSatisfy { $0.trail.isEmpty })
    }

    @Test("With a coastline, streaks run along the shore and never onto the land")
    func particlesKeepOffTheShore() throws {
        // A map of 6 rows by 8 columns with a coastline twice as fine: 11 rows by 15 columns.
        // The land is everything east of x = 4.5 (fine columns 10 to 14).
        let detail = 2
        let fineRows = 11
        let fineCols = 15
        var bits = [UInt8](repeating: 0, count: fineRows * 2)
        for row in 0..<fineRows {
            for col in 0..<10 { bits[row * 2 + col / 8] |= 0x80 >> UInt8(col % 8) }
        }
        let coast = try #require(CurrentWaterMask(detail: detail, rows: fineRows, cols: fineCols, bits: bits))
        #expect(coast.fits(rows: 6, cols: 8) && !coast.fits(rows: 6, cols: 9))
        #expect(coast.isWater(x: 4.7, y: 2) && !coast.isWater(x: 4.8, y: 2) && !coast.isWater(x: -1, y: 2) && !coast.isWater(x: 2, y: 9))

        // A step that stays on the water is taken as it is; with no coastline, any step is.
        let head = CurrentParticleSystem.Point(x: 4.6, y: 2)
        let ashore = CurrentParticleSystem.Point(x: 5.0, y: 2.4)
        #expect(CurrentParticleSystem.held(from: head, to: .init(x: 4.7, y: 2.4), coast: coast) == .init(x: 4.7, y: 2.4))
        #expect(CurrentParticleSystem.held(from: head, to: ashore, coast: nil) == ashore)
        // Heading north-east into a shore that runs north: it goes on northwards, more slowly.
        let turned = try #require(CurrentParticleSystem.held(from: head, to: ashore, coast: coast))
        #expect(coast.isWater(x: turned.x, y: turned.y) && turned.y > head.y && turned.x < ashore.x)
        // Straight at the shore, with land on every side it could turn to: nowhere to go.
        let cove = try #require(CurrentWaterMask(detail: 1, rows: 3, cols: 3, bits: [0b0000_0000, 0b0100_0000, 0b0000_0000]))
        #expect(CurrentParticleSystem.held(from: .init(x: 1, y: 1), to: .init(x: 3, y: 1), coast: cove) == nil)

        // Water flowing east and a little north at every point, the land included,
        // as the map view hands it over: carried ashore so the streaks reach the coast.
        let plane = 6 * 8
        let frame = CurrentFrame(rows: 6, cols: 8, u: [Float](repeating: 0.3, count: plane), v: [Float](repeating: 0.1, count: plane))
        let bounds = CurrentParticleSystem.Bounds(minX: 0, maxX: 7, minY: 0, maxY: 5)
        var system = CurrentParticleSystem(count: 150, seed: 5)
        var reachedTheShore = false
        var ranAlongIt = false
        for _ in 0..<300 {
            let before = system.particles
            system.step(frame: frame, bounds: bounds, dt: 0.05, cellsPerSecond: (x: 2, y: 2), coast: coast)
            for (old, new) in zip(before, system.particles) {
                for point in new.trail { #expect(coast.isWater(x: point.x, y: point.y)) }
                guard let was = old.trail.last, let now = new.trail.last, new.age > old.age else { continue }
                if now.x > 4.6 { reachedTheShore = true }
                if was.x > 4.6, now.y > was.y + 0.001 { ranAlongIt = true }
            }
        }
        #expect(reachedTheShore && ranAlongIt)

        // A coastline made for another map is left out rather than misread.
        let other = try #require(CurrentWaterMask(detail: 1, rows: 3, cols: 3, bits: [0, 0, 0]))
        var free = CurrentParticleSystem(count: 20, seed: 2)
        for _ in 0..<5 { free.step(frame: frame, bounds: bounds, dt: 0.05, cellsPerSecond: (x: 2, y: 2), coast: other) }
        #expect(free.particles.contains { $0.trail.count > 1 })
    }

    @Test("The zoom buttons halve and double the view and stop at their limits")
    func zoomButtons() throws {
        let grid = CurrentMapGrid(south: 22.1, north: 22.5, west: 114.1, east: 114.6, rows: 101, cols: 126)
        let whole = CurrentMapRegion(fitting: grid)
        #expect(abs(whole.centerLatitude - 22.3) < 1e-9 && abs(whole.centerLongitude - 114.35) < 1e-9)
        #expect(abs(whole.latitudeSpan - 0.4) < 1e-9 && abs(whole.longitudeSpan - 0.5) < 1e-9)
        #expect(whole.canZoomIn(grid) && whole.canZoomOut(grid))

        // One press in: half as much in view, the same centre, the same shape.
        let closer = whole.zoomed(closer: true, grid: grid)
        #expect(abs(closer.latitudeSpan - 0.2) < 1e-9 && abs(closer.longitudeSpan - 0.25) < 1e-9)
        #expect(closer.centerLatitude == whole.centerLatitude && closer.centerLongitude == whole.centerLongitude)
        // In and out again is where it started.
        let back = closer.zoomed(closer: false, grid: grid)
        #expect(abs(back.latitudeSpan - whole.latitudeSpan) < 1e-9 && abs(back.longitudeSpan - whole.longitudeSpan) < 1e-9)

        // Pressing in stops at two cells of the grid (0.004 degrees each here), and the button says so.
        var view = whole
        for _ in 0..<20 { view = view.zoomed(closer: true, grid: grid) }
        #expect(abs(view.latitudeSpan - 0.008) < 1e-9)
        #expect(abs(view.longitudeSpan / view.latitudeSpan - 1.25) < 1e-6)
        #expect(!view.canZoomIn(grid) && view.canZoomOut(grid))

        // Pressing out stops at eight times the venue.
        for _ in 0..<20 { view = view.zoomed(closer: false, grid: grid) }
        #expect(abs(view.latitudeSpan - 3.2) < 1e-9)
        #expect(view.canZoomIn(grid) && !view.canZoomOut(grid))

        // A view pinched in past the limit is left alone by "in", and "out" still works from it.
        let pinched = CurrentMapRegion(centerLatitude: 22.3, centerLongitude: 114.3, latitudeSpan: 0.001, longitudeSpan: 0.002)
        #expect(pinched.zoomed(closer: true, grid: grid) == pinched)
        #expect(abs(pinched.zoomed(closer: false, grid: grid).latitudeSpan - 0.002) < 1e-12)

        // A view the map has not reported properly falls back to the whole venue.
        let broken = CurrentMapRegion(centerLatitude: 0, centerLongitude: 0, latitudeSpan: 0, longitudeSpan: .nan)
        #expect(broken.zoomed(closer: true, grid: grid) == whole)
    }
}

@Suite("Current model validation")
struct CurrentModelValidationTests {

    @Test("The server's validation report decodes and names the best model")
    func reportDecodes() throws {
        let json = """
        {"ok":true,"venue":"hong-kong","generated":"2026-11-25T04:00:00.000Z","rules":{"minSeconds":30,"maxKnots":6},
        "windows":[
          {"id":"24h","hours":24,
           "mine":{"readings":0,"boats":0,"models":{"day-ahead":{"count":0,"errorKn":null,"speedBiasKn":null,"directionDeg":null,"noCurrentKn":null}}},
           "all":{"readings":6,"boats":1,"models":{"day-ahead":{"count":6,"errorKn":0.21,"speedBiasKn":-0.04,"directionDeg":31,"noCurrentKn":0.46}}}},
          {"id":"7d","hours":168,
           "mine":{"readings":27,"boats":1,"models":{
              "day-ahead":{"count":27,"errorKn":0.19,"speedBiasKn":0.02,"directionDeg":24,"noCurrentKn":0.36},
              "tide":{"count":27,"errorKn":0.39,"speedBiasKn":-0.2,"directionDeg":80,"noCurrentKn":0.36},
              "global":{"count":2,"errorKn":0.05,"speedBiasKn":0,"directionDeg":null,"noCurrentKn":0.2}}},
           "all":{"readings":81,"boats":3,"models":{}}}]}
        """
        let report = try JSONDecoder().decode(CurrentValidationReport.self, from: Data(json.utf8))
        #expect(report.windows.count == 2)

        let week = try #require(report.scope(window: .week, readings: .mine))
        #expect(week.readings == 27)
        // "global" has the smallest error but answered too few readings to be called best.
        #expect(week.best() == "day-ahead")
        let best = try #require(week.models["day-ahead"])
        #expect(best.errorText == "0.19 kn")
        #expect(best.detailText == "27 readings · 0.02 kn fast · 24° off")
        #expect(close(try #require(best.gain), 1 - 0.19 / 0.36))
        #expect(try #require(week.models["tide"]?.gain) < 0)

        let day = try #require(report.scope(window: .day, readings: .mine))
        #expect(day.best() == nil)
        #expect(day.models["day-ahead"]?.errorText == "–")
        #expect(day.models["day-ahead"]?.detailText == "No readings to compare")
        #expect(report.scope(window: .day, readings: .all)?.models["day-ahead"]?.detailText == "6 readings · 0.04 kn slow · 31° off")
    }
}
