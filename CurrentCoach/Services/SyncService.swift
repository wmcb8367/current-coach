import Foundation

@MainActor
@Observable
final class SyncService {
    private(set) var isSyncing: Bool = false
    private(set) var lastSyncedAt: Date?
    private(set) var lastError: String?

    var pendingCount: Int {
        store.measurements.filter { $0.syncedAt == nil }.count
    }

    private let api: APIClient
    private unowned let store: MeasurementStore
    private unowned let auth: AuthStore
    private var currentTask: Task<Void, Never>?

    init(api: APIClient = .shared, store: MeasurementStore, auth: AuthStore) {
        self.api = api
        self.store = store
        self.auth = auth
    }

    /// Uploads saved measurements (including quality flags) without requiring sign-in.
    /// Safe to call repeatedly; re-entry is coalesced.
    func syncPending() {
        guard auth.autoSyncEnabled else { return }
        let token = auth.isSignedIn ? auth.token : nil
        if currentTask != nil { return }
        currentTask = Task { [weak self] in
            guard let self else { return }
            // Drain batches, including readings captured during an in-flight upload.
            while self.auth.autoSyncEnabled && self.auth.token == token && self.pendingCount > 0 {
                let before = self.pendingCount
                await self.performSync(token: token)
                if self.lastError != nil || self.pendingCount >= before { break }
            }
            self.currentTask = nil
        }
    }

    /// Called after a new measurement is saved. Only syncs if auto-sync is on.
    func didAddMeasurement() {
        guard auth.autoSyncEnabled else { return }
        syncPending()
    }

    private func performSync(token: String?) async {
        let pending = Array(store.measurements.filter { $0.syncedAt == nil }.prefix(100))
        guard !pending.isEmpty else { return }

        isSyncing = true
        lastError = nil
        defer { isSyncing = false }

        let items = pending.map { m in
            SyncRequestItem(
                clientId: m.id.uuidString,
                timestamp: m.timestamp,
                durationSeconds: m.duration,
                speedMetersPerMinute: m.speedMetersPerMinute,
                fromDirectionDegrees: m.fromDirection,
                latitude: m.latitude,
                longitude: m.longitude,
                confidence: m.confidence,
                isValid: m.isValid,
                source: "current-coach-ios"
            )
        }

        do {
            let response: SyncResponse
            if let token {
                response = try await api.syncCurrentMeasurements(items, token: token)
            } else {
                response = try await api.syncGuestMeasurements(items)
            }
            let accepted = Set(response.accepted.compactMap { UUID(uuidString: $0.clientId) })
            store.markSynced(ids: accepted, at: response.syncedAt)
            lastSyncedAt = response.syncedAt
        } catch let error as APIError {
            lastError = error.errorDescription
            switch error {
            case .notAuthenticated:
                // Do not silently downgrade an expired account session to guest uploads.
                break
            case .server(let status, _) where status == 403:
                // Server says this account has sync disabled — refresh our
                // local preference so the UI toggle matches reality.
                await auth.refreshPreferences()
            default:
                break
            }
        } catch {
            lastError = error.localizedDescription
        }
    }
}
