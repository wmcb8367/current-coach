import Foundation

enum APIError: LocalizedError {
    case badURL
    case transport(Error)
    case server(status: Int, message: String?)
    case decoding(Error)
    case notAuthenticated

    var errorDescription: String? {
        switch self {
        case .badURL: return "Invalid server URL"
        case .transport(let err): return err.localizedDescription
        case .server(let status, let message): return message ?? "Server error (\(status))"
        case .decoding: return "Unexpected server response"
        case .notAuthenticated: return "You are not signed in"
        }
    }
}

struct AuthUser: Codable, Sendable, Equatable {
    let id: String
    let email: String
    let name: String?
}

struct AuthResponse: Codable, Sendable {
    let ok: Bool
    let token: String
    let user: AuthUser
}

struct ServerError: Codable { let ok: Bool?; let error: String? }

struct SyncRequestItem: Codable {
    let clientId: String
    let timestamp: Date
    let durationSeconds: Double
    let speedMetersPerMinute: Double
    let fromDirectionDegrees: Double
    let latitude: Double
    let longitude: Double
    let confidence: Double?
    let isValid: Bool
    let source: String
}

struct SyncAccepted: Codable { let id: String; let clientId: String }
struct SyncResponse: Codable { let ok: Bool; let syncedAt: Date; let accepted: [SyncAccepted] }

struct Preferences: Codable, Sendable, Equatable {
    let currentCoachSyncEnabled: Bool
}

struct PreferencesResponse: Codable {
    let ok: Bool
    let preferences: Preferences
}

@MainActor
final class APIClient {
    static let shared = APIClient()

    var baseURL: URL {
        if let override = UserDefaults.standard.string(forKey: "CC_API_BASE_URL"),
           let url = URL(string: override) { return url }
        return URL(string: "https://m2xsailing.com")!
    }

    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(session: URLSession = .shared) {
        self.session = session

        self.decoder = JSONDecoder()
        self.decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            let withMs = ISO8601DateFormatter()
            withMs.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = withMs.date(from: raw) { return date }
            let plain = ISO8601DateFormatter()
            plain.formatOptions = [.withInternetDateTime]
            if let date = plain.date(from: raw) { return date }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unrecognized date: \(raw)")
        }

        self.encoder = JSONEncoder()
        self.encoder.dateEncodingStrategy = .custom { date, encoder in
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var container = encoder.singleValueContainer()
            try container.encode(f.string(from: date))
        }
    }

    // MARK: - Auth

    func signIn(email: String, password: String) async throws -> AuthResponse {
        try await postJSON(path: "/api/mobile/session", body: ["email": email, "password": password])
    }

    func signUp(email: String, password: String, name: String?) async throws -> AuthResponse {
        var body: [String: Any] = ["email": email, "password": password, "acceptedLegal": true]
        if let name, !name.isEmpty { body["name"] = name }
        return try await postJSON(path: "/api/mobile/register", body: body)
    }

    // MARK: - Sync

    func syncCurrentMeasurements(_ items: [SyncRequestItem], token: String) async throws -> SyncResponse {
        let payload = ["measurements": items]
        let data = try encoder.encode(payload)
        return try await send(path: "/api/current-measurements", method: "POST", body: data, token: token)
    }

    // MARK: - Preferences

    func fetchPreferences(token: String) async throws -> Preferences {
        let response: PreferencesResponse = try await send(
            path: "/api/mobile/preferences",
            method: "GET",
            body: nil,
            token: token
        )
        return response.preferences
    }

    func updatePreferences(currentCoachSyncEnabled: Bool, token: String) async throws -> Preferences {
        let data = try JSONSerialization.data(withJSONObject: [
            "currentCoachSyncEnabled": currentCoachSyncEnabled,
        ])
        let response: PreferencesResponse = try await send(
            path: "/api/mobile/preferences",
            method: "PATCH",
            body: data,
            token: token
        )
        return response.preferences
    }

    // MARK: - Internals

    private func postJSON<T: Decodable>(path: String, body: [String: Any]) async throws -> T {
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send(path: path, method: "POST", body: data, token: nil)
    }

    private func send<T: Decodable>(path: String, method: String, body: Data?, token: String?) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL) else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body { req.httpBody = body }

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError.transport(error)
        }

        let http = response as? HTTPURLResponse
        let status = http?.statusCode ?? 0
        if !(200..<300).contains(status) {
            let message = (try? JSONDecoder().decode(ServerError.self, from: data))?.error
            if status == 401 { throw APIError.notAuthenticated }
            throw APIError.server(status: status, message: message)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding(error)
        }
    }
}
