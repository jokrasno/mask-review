# Repeatable local acceptance

From a clone after completing setup, use the app's own interpreter. Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe verify_e2e.py
```

macOS or Linux:

```sh
.venv/bin/python verify_e2e.py
```

Or select a new evidence directory outside the clone:

```powershell
.\.venv\Scripts\python.exe verify_e2e.py --output C:\Temp\mask-review-new-evidence
```

```sh
.venv/bin/python verify_e2e.py --output /path/outside/clone/new-evidence
```

The script generates two small geometric images/masks in a compatible ZIP, starts the actual desktop HTTP server, imports pending suggestions, edits native labels, rejects unnamed approval, approves one frame, checks approved-only export and restarts to confirm durable history. It also verifies session/origin guards, invalid configuration rejection, library binding and no network calls during unconfigured offline work. It saves `generated-review.zip`, `approved-export.zip`, `receipt.json` and server logs outside the clone. Import the generated ZIP in the browser for manual visual checks.

Add `--google-fixture` to exercise loopback OAuth PKCE, native credential persistence, immutable queued uploads and disconnect. This requires the OS credential store to be available, but uses only a local fake service and fabricated credentials. No request reaches Google. Keep the resulting private/runtime directories outside source control. The receipt includes exact source hashes and the repeat command. This HTTP acceptance does not verify browser interactions, real Google access or actual macOS behavior.
