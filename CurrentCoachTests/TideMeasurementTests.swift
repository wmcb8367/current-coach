import Foundation
import Testing
@testable import CurrentCoach

@Suite("TideMeasurement Tests")
struct TideMeasurementTests {

    @Test("Speed conversions from m/min")
    func speedConversions() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 180,
            speedMetersPerMinute: 6.6,
            fromDirection: 207,
            latitude: 39.5,
            longitude: 2.7,
            isValid: true
        )

        // 6.6 m/min = 396 m/hr = 396/1852 kts ≈ 0.2138
        #expect(abs(m.speedKnots - 0.2138) < 0.001)

        // 6.6 m/min = 660 cm / 60s = 11 cm/s
        #expect(abs(m.speedCmPerSecond - 11.0) < 0.001)

        // 6.6 m/min = 6.6/60 = 0.11 m/s
        #expect(abs(m.speedMetersPerSecond - 0.11) < 0.001)
    }

    @Test("Duration formatting")
    func durationFormatting() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 167,
            speedMetersPerMinute: 5.0,
            fromDirection: 180,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(m.durationFormatted == "02:47")
    }

    @Test("Flow direction is opposite of from")
    func flowDirection() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 60,
            speedMetersPerMinute: 1.0,
            fromDirection: 207,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(abs(m.flowDirection - 27.0) < 0.001)
    }

    @Test("Flow direction wraps around 360")
    func flowDirectionWrap() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 60,
            speedMetersPerMinute: 1.0,
            fromDirection: 10,
            latitude: 0,
            longitude: 0,
            isValid: true
        )
        #expect(abs(m.flowDirection - 190.0) < 0.001)
    }

    @Test("Invalid measurement with zero speed")
    func invalidMeasurement() {
        let m = TideMeasurement(
            id: UUID(),
            timestamp: Date(),
            duration: 3,
            speedMetersPerMinute: 0.0,
            fromDirection: 0,
            latitude: 0,
            longitude: 0,
            isValid: false
        )
        #expect(!m.isValid)
        #expect(m.speedKnots == 0)
    }
}

@Suite("Bearing Calculation Tests")
@MainActor
struct BearingCalculationTests {

    @Test("Normalize angle positive")
    func normalizePositive() {
        #expect(MeasureViewModel.normalizeAngle(370) == 10)
    }

    @Test("Normalize angle negative")
    func normalizeNegative() {
        #expect(MeasureViewModel.normalizeAngle(-10) == 350)
    }

    @Test("Normalize angle zero")
    func normalizeZero() {
        #expect(MeasureViewModel.normalizeAngle(0) == 0)
    }

    @Test("Normalize angle 360")
    func normalize360() {
        #expect(MeasureViewModel.normalizeAngle(360) == 0)
    }
}


private final class GuestUploadProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> (Int, Data))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, data) = Self.handler!(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Suite("Guest upload queue", .serialized)
@MainActor
struct GuestUploadTests {
    @Test("Signed-out uploads preserve invalid readings, batch, deduplicate and respect opt-out")
    func guestQueue() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [GuestUploadProtocol.self]
        let api = APIClient(session: URLSession(configuration: config), installationKeyProvider: { String(repeating: "a", count: 64) })
        let auth = AuthStore(api: api)
        auth.signOut()
        let file = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        let store = MeasurementStore(fileURL: file)
        for _ in 0..<101 {
            store.add(TideMeasurement(id: UUID(), timestamp: Date(), duration: 60,
                speedMetersPerMinute: 5, fromDirection: 210, latitude: 33, longitude: -118, isValid: false))
        }
        let sync = SyncService(api: api, store: store, auth: auth)
        await auth.setAutoSyncEnabled(false)
        sync.syncPending()
        try await Task.sleep(for: .milliseconds(30))
        #expect(sync.pendingCount == 101)
        let responseIDs = store.measurements.map { $0.id.uuidString }
        let counter = GuestRequestCounter()
        GuestUploadProtocol.handler = { request in
            let index = counter.increment()
            #expect(request.url?.path == "/api/current-measurements/guest")
            #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
            #expect(request.value(forHTTPHeaderField: "X-Current-Coach-Installation")?.count == 64)
            let ids = Array(responseIDs.dropFirst(index * 100).prefix(100))
            let accepted = ids.map { ["id": "server-id", "clientId": $0] }
            return (200, try! JSONSerialization.data(withJSONObject: ["ok": true, "syncedAt": "2026-11-18T06:00:00.000Z", "accepted": accepted]))
        }
        await auth.setAutoSyncEnabled(true)
        sync.syncPending()
        for _ in 0..<200 { if sync.pendingCount == 0 { break }; try await Task.sleep(for: .milliseconds(10)) }
        #expect(sync.pendingCount == 0)
        #expect(counter.value == 2)
        #expect(MeasurementStore(fileURL: file).measurements.allSatisfy { $0.syncedAt != nil })
        sync.syncPending()
        try await Task.sleep(for: .milliseconds(30))
        #expect(counter.value == 2)
    }

    @Test("Failed unsigned-in upload remains persisted for retry")
    func failureRetainsQueue() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [GuestUploadProtocol.self]
        let api = APIClient(session: URLSession(configuration: config), installationKeyProvider: { String(repeating: "a", count: 64) })
        let auth = AuthStore(api: api); auth.signOut(); await auth.setAutoSyncEnabled(true)
        let file = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: file) }
        let store = MeasurementStore(fileURL: file)
        store.add(TideMeasurement(id: UUID(), timestamp: Date(), duration: 60,
            speedMetersPerMinute: 5, fromDirection: 210, latitude: 33, longitude: -118, isValid: true))
        GuestUploadProtocol.handler = { _ in (503, Data("{\"ok\":false,\"error\":\"offline\"}".utf8)) }
        let sync = SyncService(api: api, store: store, auth: auth); sync.syncPending()
        for _ in 0..<200 { if sync.lastError != nil { break }; try await Task.sleep(for: .milliseconds(10)) }
        #expect(sync.lastError == "offline")
        #expect(sync.pendingCount == 1)
        #expect(MeasurementStore(fileURL: file).measurements.first?.syncedAt == nil)
    }
}

private final class GuestRequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func increment() -> Int { lock.lock(); defer { lock.unlock() }; let prior = count; count += 1; return prior }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}
