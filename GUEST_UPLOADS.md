# Guest current upload implementation

Branch: feat/guest-current-uploads. Companion backend/Android branch: feat/current-coach-guest-uploads in m2x-ecosystem.

Unsigned-in users upload by default using a random device-only Keychain identity; no fabricated account or hardware identifier. The main screen discloses location/time uploads and provides an opt-out. Existing cached opt-outs are preserved. Signed-in users keep their authenticated path and server preference. An expired account token is not silently downgraded to anonymous upload.

Persistent readings (including isValid=false quality flags) upload in batches of100. Acknowledged IDs alone are marked synced. Offline queues retry every30seconds while the app is active, and on foreground/sign-in/new readings. OS-suspended or terminated background delivery is not guaranteed. Guest observations are available to backend superusers, not public users and not automatically added to a later signed-in personal library.

Local validation: simulator build succeeded;11Swift tests passed including guest no-login queue, batching, opt-out, duplicate avoidance and failed-upload persistence. Network tests inject a deterministic installation key; real device-only Keychain behavior still needs signed-device verification. Backend14tests and TypeScript check passed; Android debug build passed. UI verification status is separate from these results.

Not released: backend additive migration/endpoint deployment must precede new client rollout. Public privacy disclosures/store data-collection declarations must reflect unsigned-in location/time collection. No production uploads or App Store/TestFlight release were performed. Release new build/version through the existing app distribution workflow after coordinated backend rollout. Old installed versions are unchanged.
