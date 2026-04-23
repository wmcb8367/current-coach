import SwiftUI

struct AccountView: View {
    let auth: AuthStore
    let sync: SyncService

    var body: some View {
        ZStack {
            NT.bgPrimary.ignoresSafeArea()

            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    M2XLogo(height: 22)
                    Rectangle()
                        .fill(NT.borderSubtle)
                        .frame(width: 1, height: 18)
                    Text("Account")
                        .eyebrow(NT.textSecondary)
                    Spacer()
                }
                .padding(.horizontal, 20)
                .padding(.top, 10)
                .padding(.bottom, 12)

                if auth.isSignedIn {
                    SignedInView(auth: auth, sync: sync)
                } else {
                    SignInForm(auth: auth)
                }

                Spacer()

                Text("M2X · Current Coach")
                    .font(.caption)
                    .foregroundStyle(NT.textFaint)
                    .padding(.bottom, 8)
            }
        }
    }
}

// MARK: - Signed in

private struct SignedInView: View {
    let auth: AuthStore
    let sync: SyncService

    @State private var showDeleteConfirmation = false

    var body: some View {
        VStack(spacing: 18) {
            VStack(spacing: 10) {
                M2XLogo(height: 48, color: NT.textPrimary)
                    .shadow(color: NT.accentTeal.opacity(0.35), radius: 18)
                Text("Wind Analytics")
                    .eyebrow(NT.accentTealSoft)
            }
            .padding(.top, 24)

            VStack(alignment: .leading, spacing: 6) {
                Text("Signed in as")
                    .eyebrow()
                Text(auth.user?.email ?? "—")
                    .font(.headline.monospaced())
                    .foregroundStyle(NT.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .m2xCard()
            .padding(.horizontal)

            VStack(alignment: .leading, spacing: 10) {
                Toggle(isOn: Binding(
                    get: { auth.autoSyncEnabled },
                    set: { newValue in
                        Task {
                            await auth.setAutoSyncEnabled(newValue)
                            if newValue && auth.autoSyncEnabled { sync.syncPending() }
                        }
                    }
                )) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Auto-sync measurements")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(NT.textPrimary)
                        Text("Syncs with your m2x account — changes here also apply on m2xsailing.com.")
                            .font(.caption)
                            .foregroundStyle(NT.textDim)
                    }
                }
                .tint(NT.accentTeal)
                .disabled(auth.isUpdatingPreference)

                if let prefError = auth.preferenceError {
                    Text(prefError)
                        .font(.caption)
                        .foregroundStyle(NT.accentCoral)
                }

                Divider().overlay(NT.borderSubtle)

                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Pending")
                            .eyebrow()
                        Text("\(sync.pendingCount) measurements")
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(NT.textPrimary)
                    }
                    Spacer()
                    Button {
                        sync.syncPending()
                    } label: {
                        HStack(spacing: 6) {
                            if sync.isSyncing {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "arrow.triangle.2.circlepath")
                            }
                            Text(sync.isSyncing ? "Syncing…" : "Sync now")
                        }
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(NT.accentTeal)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(Capsule().fill(NT.accentTeal.opacity(0.12)))
                        .overlay(Capsule().strokeBorder(NT.accentTeal.opacity(0.35), lineWidth: 1))
                    }
                    .disabled(sync.isSyncing || sync.pendingCount == 0)
                }

                if let lastSync = sync.lastSyncedAt {
                    Text("Last synced \(lastSync, style: .relative) ago")
                        .font(.caption)
                        .foregroundStyle(NT.textDim)
                }

                if let error = sync.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(NT.accentCoral)
                }
            }
            .padding(16)
            .m2xCard()
            .padding(.horizontal)

            Button(role: .destructive) {
                auth.signOut()
            } label: {
                Text("Sign Out")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(NT.accentCoral)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 10)
            }
            .padding(.top, 6)

            Button {
                showDeleteConfirmation = true
            } label: {
                HStack(spacing: 6) {
                    if auth.isBusy {
                        ProgressView().controlSize(.small).tint(NT.accentCoral)
                    }
                    Text("Delete Account")
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(NT.accentCoral.opacity(0.7))
                .padding(.horizontal, 20)
                .padding(.vertical, 8)
            }
            .disabled(auth.isBusy)
            .alert("Delete Account", isPresented: $showDeleteConfirmation) {
                Button("Cancel", role: .cancel) { }
                Button("Delete Account", role: .destructive) {
                    Task { await auth.deleteAccount() }
                }
            } message: {
                Text("This will permanently delete your account and all associated data. This action cannot be undone.")
            }
        }
    }
}

// MARK: - Sign in / sign up

private struct SignInForm: View {
    let auth: AuthStore

    @State private var mode: Mode = .signIn
    @State private var email: String = ""
    @State private var password: String = ""
    @State private var name: String = ""
    @FocusState private var focusedField: Field?

    private enum Mode { case signIn, signUp }
    private enum Field { case email, password, name }

    var body: some View {
        VStack(spacing: 20) {
            VStack(spacing: 14) {
                M2XLogo(height: 52, color: NT.textPrimary)
                    .shadow(color: NT.accentTeal.opacity(0.35), radius: 22)
                Text("Wind Analytics")
                    .eyebrow(NT.accentTealSoft)
                Text(mode == .signIn
                     ? "Sign in to sync your current measurements."
                     : "Create an m2x account — works on web and iOS.")
                    .font(.subheadline)
                    .foregroundStyle(NT.textDim)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            .padding(.top, 12)

            VStack(spacing: 10) {
                if mode == .signUp {
                    inputField("Name (optional)", text: $name, field: .name)
                        .textContentType(.name)
                }
                inputField("Email", text: $email, field: .email)
                    .keyboardType(.emailAddress)
                    .textContentType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                inputField("Password", text: $password, field: .password, secure: true)
                    .textContentType(mode == .signIn ? .password : .newPassword)
            }
            .padding(.horizontal)

            if let error = auth.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(NT.accentCoral)
                    .padding(.horizontal)
            }

            Button {
                submit()
            } label: {
                HStack(spacing: 8) {
                    if auth.isBusy { ProgressView().controlSize(.small).tint(NT.bgPrimary) }
                    Text(mode == .signIn ? "Sign In" : "Create Account")
                }
                .font(.headline)
                .foregroundStyle(NT.bgPrimary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(Color.white)
                )
                .shadow(color: Color.white.opacity(0.15), radius: 18, y: 6)
            }
            .disabled(auth.isBusy || !canSubmit)
            .opacity(canSubmit ? 1.0 : 0.6)
            .padding(.horizontal)

            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    mode = (mode == .signIn) ? .signUp : .signIn
                    auth.lastError = nil
                }
            } label: {
                Text(mode == .signIn ? "New to m2x? Create an account" : "Already have an account? Sign in")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(NT.accentTealSoft)
            }
            .padding(.top, 2)
        }
    }

    private var canSubmit: Bool {
        !email.isEmpty && password.count >= (mode == .signIn ? 1 : 8)
    }

    private func submit() {
        focusedField = nil
        Task {
            if mode == .signIn {
                await auth.signIn(email: email.lowercased(), password: password)
            } else {
                await auth.signUp(email: email.lowercased(), password: password, name: name.isEmpty ? nil : name)
            }
        }
    }

    @ViewBuilder
    private func inputField(_ placeholder: String, text: Binding<String>, field: Field, secure: Bool = false) -> some View {
        Group {
            if secure {
                SecureField(placeholder, text: text)
            } else {
                TextField(placeholder, text: text)
            }
        }
        .focused($focusedField, equals: field)
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(NT.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(focusedField == field ? NT.accentTeal.opacity(0.5) : NT.borderSubtle, lineWidth: 1)
        )
        .foregroundStyle(NT.textPrimary)
        .tint(NT.accentTeal)
    }
}
