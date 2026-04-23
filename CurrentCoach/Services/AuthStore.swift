import Foundation

@MainActor
@Observable
final class AuthStore {
    private enum Key {
        static let token = "auth-token"
        static let userJSON = "auth-user-json"
        // Cached copy of the server preference so the toggle has a stable
        // local value to render before a preferences fetch completes.
        static let autoSyncCache = "CC_AUTO_SYNC_ENABLED_CACHED"
    }

    private(set) var token: String?
    private(set) var user: AuthUser?
    var isBusy: Bool = false
    var lastError: String?

    private(set) var autoSyncEnabled: Bool
    private(set) var isUpdatingPreference: Bool = false
    var preferenceError: String?

    var isSignedIn: Bool { token != nil && user != nil }

    private let api: APIClient

    init(api: APIClient = .shared) {
        self.api = api
        if UserDefaults.standard.object(forKey: Key.autoSyncCache) == nil {
            self.autoSyncEnabled = true
        } else {
            self.autoSyncEnabled = UserDefaults.standard.bool(forKey: Key.autoSyncCache)
        }
        self.token = KeychainStore.get(Key.token)
        if let json = KeychainStore.get(Key.userJSON), let data = json.data(using: .utf8) {
            self.user = try? JSONDecoder().decode(AuthUser.self, from: data)
        }
        if token == nil || user == nil {
            self.token = nil
            self.user = nil
        }
    }

    func signIn(email: String, password: String) async {
        await perform { [api] in try await api.signIn(email: email, password: password) }
        if isSignedIn { await refreshPreferences() }
    }

    func signUp(email: String, password: String, name: String?) async {
        await perform { [api] in try await api.signUp(email: email, password: password, name: name) }
        if isSignedIn { await refreshPreferences() }
    }

    func signOut() {
        token = nil
        user = nil
        KeychainStore.delete(Key.token)
        KeychainStore.delete(Key.userJSON)
    }

    func deleteAccount() async {
        guard let token else { return }
        isBusy = true
        lastError = nil
        defer { isBusy = false }
        do {
            try await api.deleteAccount(token: token)
            signOut()
        } catch let error as APIError {
            lastError = error.errorDescription
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// Pull the latest preferences from the server. Safe to call on app
    /// foreground, post-sign-in, or whenever we suspect the local copy is
    /// stale (e.g. after a 403 from the sync endpoint).
    func refreshPreferences() async {
        guard let token else { return }
        do {
            let prefs = try await api.fetchPreferences(token: token)
            applyPreferences(prefs)
            preferenceError = nil
        } catch let error as APIError {
            preferenceError = error.errorDescription
            if case .notAuthenticated = error { signOut() }
        } catch {
            preferenceError = error.localizedDescription
        }
    }

    /// Optimistic update: flip local state immediately, PATCH the server,
    /// revert on failure. Either way the final local value matches the
    /// server's authoritative response.
    func setAutoSyncEnabled(_ value: Bool) async {
        guard let token else { return }
        let previous = autoSyncEnabled
        autoSyncEnabled = value
        isUpdatingPreference = true
        preferenceError = nil
        defer { isUpdatingPreference = false }
        do {
            let prefs = try await api.updatePreferences(currentCoachSyncEnabled: value, token: token)
            applyPreferences(prefs)
        } catch let error as APIError {
            autoSyncEnabled = previous
            preferenceError = error.errorDescription
            if case .notAuthenticated = error { signOut() }
        } catch {
            autoSyncEnabled = previous
            preferenceError = error.localizedDescription
        }
    }

    private func applyPreferences(_ prefs: Preferences) {
        autoSyncEnabled = prefs.currentCoachSyncEnabled
        UserDefaults.standard.set(prefs.currentCoachSyncEnabled, forKey: Key.autoSyncCache)
    }

    private func perform(_ call: @Sendable @escaping () async throws -> AuthResponse) async {
        isBusy = true
        lastError = nil
        defer { isBusy = false }
        do {
            let response = try await call()
            persist(token: response.token, user: response.user)
        } catch let error as APIError {
            lastError = error.errorDescription
        } catch {
            lastError = error.localizedDescription
        }
    }

    private func persist(token: String, user: AuthUser) {
        self.token = token
        self.user = user
        KeychainStore.set(token, account: Key.token)
        if let json = try? JSONEncoder().encode(user), let str = String(data: json, encoding: .utf8) {
            KeychainStore.set(str, account: Key.userJSON)
        }
    }
}
