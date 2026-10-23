# Optional Google Drive setup

Offline review needs no Google setup. A new clone has no library destinations and makes no Google requests until a valid private library configuration is imported. Google client configuration, library destinations and sign-in credentials are separate.

## Library owner

1. Create a private root library folder and three distinct folders for original photos, labels/snapshots and incoming photos. Grant teammates only the Drive permissions they need. This app uses the labels folder for immutable review/source packages and snapshots, and incoming for quarantined photos; it does not automatically create folders or upload original datasets.
2. Copy `library-config.example.json` outside this repository. Replace all four placeholder values with the actual Drive folder IDs, not full URLs. Keep all four IDs distinct. Send this private JSON to authorized teammates through your existing private channel. Do not commit it.
3. In your own Google Cloud project, enable the Drive API, configure OAuth consent and create an OAuth client with application type Desktop app. Add authorized test users while your application is in Testing, or complete Google's applicable production requirements. Testing can require reconnection after token expiry. Download the client JSON and share it privately with authorized users. Do not put it in this repository.
4. Share a compatible snapshot ZIP in the labels folder, or have users import local review ZIPs and review their saved versions. Existing Drive permissions apply; importing IDs does not grant access.

## Each teammate

Open Google setup & library. Import the private library configuration JSON and Desktop client JSON in their separate file controls. Read and acknowledge the Drive scope, then sign in with your own Google account. The app requests the full Drive scope to discover existing shared-folder children, and limits its operations to the configured destinations. Use an account whose permissions you are comfortable granting. The example config is intentionally rejected until replaced with real IDs.

Find a library snapshot, then Download & open. Prior approvals become provenance and need fresh local review. Local saves queue while disconnected or paused and sync while the app is open. Incoming versions never silently replace the canvas. Inspect conflicting branches, choose explicitly and approve again. Incoming photos require rights/provenance and capture metadata before review.

A workspace binds to its first library. To select another library, start with both a new data directory and a new private directory. Imported config lives in the private directory as `library-config.json`, separate from `google-client.json` and native stored tokens. Keep all runtime files outside the clone. Disconnect removes local Google credentials and stops sync; saved local reviews remain.

Windows uses DPAPI; macOS uses Keychain; Linux uses an unlocked Secret Service through keyring. No plaintext token fallback is supported. If secure storage is unavailable, continue offline or repair your OS credential service. Actual Google sign-in and actual Mac behavior require verification in your own environment.
