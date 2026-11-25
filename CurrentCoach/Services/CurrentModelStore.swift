import Foundation

/// Undoes the raw DEFLATE the server packs a day's map with.
enum CurrentModelInflater {
    static func inflate(_ data: Data) throws -> Data {
        try (data as NSData).decompressed(using: .zlib) as Data
    }
}

/// The current model maps on this phone: which venues and models exist,
/// the maps downloaded so far, and how each model scored against readings.
///
/// Everything comes from the server (`/api/current-models/...`), which
/// answers only for accounts allowed to see the maps. Downloaded maps are
/// kept in the caches folder so a day already seen opens without a signal.
@MainActor
@Observable
final class CurrentModelStore {
    enum Phase: Equatable {
        case idle
        case loading
        case ready
        case failed(String)
    }

    private enum Key {
        static let venue = "CC_MODEL_VENUE"
        static let model = "CC_MODEL_SELECTED"
        static let window = "CC_MODEL_VALIDATION_WINDOW"
        static let readings = "CC_MODEL_VALIDATION_READINGS"
    }

    /// Maps kept in memory at once; each is a couple of megabytes.
    private static let fieldLimit = 6

    private(set) var phase: Phase = .idle
    /// Set when the server could not be reached and saved maps are shown instead.
    private(set) var notice: String?
    private(set) var venues: [CurrentModelManifest.Venue] = []
    private(set) var manifest: CurrentModelManifest?
    private(set) var modelID: String
    private(set) var window: CurrentValidationWindow
    private(set) var readings: CurrentValidationReadings
    private(set) var validation: CurrentValidationReport?
    private(set) var validationError: String?
    private(set) var isLoadingValidation = false
    private(set) var downloading: Set<String> = []
    private(set) var failedDownloads: [String: String] = [:]
    /// Goes up each time a map arrives in memory, so views know to redraw.
    private(set) var loadedRevision = 0

    private var fields: [String: CurrentDayField] = [:]
    private var fieldOrder: [String] = []
    private let api: APIClient

    init(api: APIClient = .shared) {
        self.api = api
        let defaults = UserDefaults.standard
        modelID = defaults.string(forKey: Key.model) ?? ""
        window = CurrentValidationWindow(rawValue: defaults.string(forKey: Key.window) ?? "") ?? .week
        readings = CurrentValidationReadings(rawValue: defaults.string(forKey: Key.readings) ?? "") ?? .all
    }

    // MARK: - Choices

    var venue: CurrentModelManifest.Venue? { manifest?.venue }

    var model: CurrentModelManifest.Model? { manifest?.model(modelID) }

    func select(model id: String) {
        modelID = id
        UserDefaults.standard.set(id, forKey: Key.model)
    }

    func select(window: CurrentValidationWindow) {
        self.window = window
        UserDefaults.standard.set(window.rawValue, forKey: Key.window)
    }

    func select(readings: CurrentValidationReadings) {
        self.readings = readings
        UserDefaults.standard.set(readings.rawValue, forKey: Key.readings)
    }

    /// The scores for the chosen window and readings, if they have arrived.
    var scores: CurrentValidationReport.Scope? {
        validation?.scope(window: window, readings: readings)
    }

    // MARK: - Loading

    /// Fetch the list of venues and the chosen venue's manifest. Falls back
    /// to the manifest saved last time when the server cannot be reached.
    func refresh(token: String?, venueID requested: String? = nil) async {
        guard let token else {
            phase = .failed("Sign in to see the current models.")
            return
        }
        if manifest == nil { phase = .loading }
        let wanted = requested ?? UserDefaults.standard.string(forKey: Key.venue)
        do {
            let index = try JSONDecoder().decode(
                CurrentModelIndex.self,
                from: try await api.currentModelData(path: "index.json", token: token)
            )
            venues = index.venues
            guard let venue = index.venues.first(where: { $0.id == wanted }) ?? index.venues.first else {
                phase = .failed("No venue has been published yet.")
                return
            }
            let data = try await api.currentModelData(path: "\(venue.id)/manifest.json", token: token)
            let fresh = try JSONDecoder().decode(CurrentModelManifest.self, from: data)
            UserDefaults.standard.set(venue.id, forKey: Key.venue)
            try? Self.write(data, to: Self.cacheURL("\(venue.id)/manifest.json"))
            adopt(fresh)
            notice = nil
            phase = .ready
        } catch {
            let message = (error as? APIError)?.errorDescription ?? error.localizedDescription
            if manifest != nil {
                notice = "Could not refresh: \(message)"
                phase = .ready
            } else if let wanted,
                      let data = try? Data(contentsOf: Self.cacheURL("\(wanted)/manifest.json")),
                      let saved = try? JSONDecoder().decode(CurrentModelManifest.self, from: data) {
                adopt(saved)
                notice = "Showing saved maps: \(message)"
                phase = .ready
            } else {
                phase = .failed(message)
            }
        }
    }

    private func adopt(_ fresh: CurrentModelManifest) {
        if manifest?.venue.id != fresh.venue.id {
            fields.removeAll()
            fieldOrder.removeAll()
            validation = nil
        }
        manifest = fresh
        failedDownloads.removeAll()
        Self.removeOldFiles()
        if fresh.model(modelID) == nil {
            select(model: fresh.model(fresh.defaultModel)?.id ?? fresh.models.first?.id ?? "")
        }
    }

    /// Ask the server how each model matched the readings of the last day and week.
    func loadValidation(token: String?) async {
        guard let token, let venue = manifest?.venue.id, !isLoadingValidation else { return }
        isLoadingValidation = true
        defer { isLoadingValidation = false }
        do {
            let data = try await api.currentModelData(path: "\(venue)/validation", token: token)
            validation = try JSONDecoder().decode(CurrentValidationReport.self, from: data)
            validationError = nil
        } catch {
            validationError = (error as? APIError)?.errorDescription ?? error.localizedDescription
        }
    }

    // MARK: - Maps

    private func key(model: String, day: String) -> String? {
        guard let venue = manifest?.venue.id, let entry = manifest?.model(model)?.entry(for: day) else { return nil }
        return "\(venue)/\(model)/\(day)-\(Int(entry.issued))"
    }

    /// Whether the chosen venue has a map of this model for the day at all.
    func hasMap(model: String, day: String) -> Bool {
        key(model: model, day: day) != nil
    }

    func isDownloading(model: String, day: String) -> Bool {
        key(model: model, day: day).map(downloading.contains) ?? false
    }

    func downloadError(model: String, day: String) -> String? {
        key(model: model, day: day).flatMap { failedDownloads[$0] }
    }

    /// The model's map at a moment, if that day's file is in memory.
    func frame(model: String, at date: Date) -> CurrentFrame? {
        let (day, hours) = CurrentDay.split(date)
        guard let key = key(model: model, day: day) else { return nil }
        return fields[key]?.frame(hours: hours)
    }

    /// Bring a model's map for a day into memory: from the caches folder if
    /// it was downloaded before, otherwise from the server.
    func load(model: String, day: String, token: String?) async {
        guard let key = key(model: model, day: day), fields[key] == nil, !downloading.contains(key) else { return }
        downloading.insert(key)
        defer { downloading.remove(key) }
        let file = Self.cacheURL("\(key).bin")
        do {
            let data: Data
            if let saved = try? Data(contentsOf: file) {
                data = saved
            } else {
                guard let token else { throw APIError.notAuthenticated }
                data = try await api.currentModelData(path: "\(manifest?.venue.id ?? "")/\(model)/\(day).bin", token: token)
                try? Self.write(data, to: file)
            }
            let field = try await Task.detached(priority: .userInitiated) {
                try CurrentDayField(data: data) { try CurrentModelInflater.inflate($0) }
            }.value
            fields[key] = field
            fieldOrder.removeAll { $0 == key }
            fieldOrder.append(key)
            while fieldOrder.count > Self.fieldLimit {
                fields.removeValue(forKey: fieldOrder.removeFirst())
            }
            failedDownloads.removeValue(forKey: key)
            loadedRevision += 1
        } catch {
            try? FileManager.default.removeItem(at: file)
            failedDownloads[key] = (error as? APIError)?.errorDescription ?? "This map could not be read."
        }
    }

    /// Forget everything, for sign-out or loss of access.
    func clear() {
        manifest = nil
        venues = []
        validation = nil
        fields.removeAll()
        fieldOrder.removeAll()
        downloading.removeAll()
        failedDownloads.removeAll()
        notice = nil
        phase = .idle
        try? FileManager.default.removeItem(at: Self.cacheRoot)
    }

    // MARK: - Files

    private static var cacheRoot: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appending(path: "current-models")
    }

    private static func cacheURL(_ path: String) -> URL {
        cacheRoot.appending(path: path)
    }

    /// Maps older than the server keeps are of no use: drop them from the caches folder.
    private static func removeOldFiles(olderThan days: Double = 12) {
        let manager = FileManager.default
        let cutoff = Date().addingTimeInterval(-days * CurrentDay.seconds)
        guard let files = manager.enumerator(at: cacheRoot, includingPropertiesForKeys: [.contentModificationDateKey]) else { return }
        for case let file as URL in files where file.pathExtension == "bin" {
            let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            if let modified, modified < cutoff {
                try? manager.removeItem(at: file)
            }
        }
    }

    private static func write(_ data: Data, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }
}
