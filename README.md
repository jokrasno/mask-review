# Mask Review

A local browser interface for reviewing segmentation masks at their original image dimensions. Import suggestion ZIPs, paint or erase labels, record named human decisions, inspect saved history and export approved frames. Google Drive collaboration is optional and requires your own private configuration.

Python 3.10 or newer and a modern browser are required. No model, worker, weights, dataset, Google client, token or private library is included. This app does not train models or establish perception or navigation safety.

```sh
git clone https://github.com/jokrasno/mask-review.git
cd mask-review
```

## Start

Windows PowerShell, from this directory:

```powershell
python desktop_launch.py --setup
```

macOS or Linux terminal:

```sh
sh desktop.sh --setup
```

Later starts use `.\desktop.ps1` on Windows or `sh desktop.sh` on Mac/Linux. macOS also provides `Setup Mask Review.command` and `Mask Review.command`; if the download lost execute permissions, run `chmod +x *.command desktop.sh`. macOS launchers have not been verified on an actual Mac. Linux Google sign-in needs a working unlocked Secret Service; macOS uses Keychain, Windows uses current-user DPAPI. Offline editing works without Google sign-in.

The app opens `http://127.0.0.1:8791`. Stop the terminal process to close it. Do not expose its local port to the internet. Check dependencies without launching using `.\.venv\Scripts\python.exe desktop_launch.py --check` on Windows or `.venv/bin/python desktop_launch.py --check` on Mac/Linux.

## Local work

Use Import ZIP to open a `mask-review-v1` package containing `manifest.json`, original PNG/JPEG images and single-channel integer PNG suggestions. The taxonomy is `v6-object-5`: 0 background/unresolved, 1 barrier, 2 bollard, 3 person, 4 industrial_vehicle. ID 0 is not verified free space. Each manifest sample requires text metadata `id`, `filename`, `model`, `dataset`, `session`, `run`, `split`, plus `image` and `suggestion` member paths. Use development or validation review data and preserve session/run split identity.

Imported approvals require fresh human review. Enter your reviewer name, inspect each complete native mask and record the decision. Editing or restoring a saved version clears approval. Export contains approved labels only; pending/rejected/skipped frames remain recorded in the decision manifest. Model assistance is supplied by importing independently generated suggestion ZIPs, with human approval still required. No model process is launched.

Default files stay outside the clone: `%LOCALAPPDATA%\MaskReview` on Windows, `~/Library/Application Support/MaskReview` on macOS, and XDG `mask-review` folders on Linux. Keep backups of both data and private directories. Use `--data` and `--private` with `desktop_launch.py` to select independent workspaces; private configuration must be outside the source and data directories.

See [Google setup](DESKTOP_SETUP.md) for optional collaboration. This source snapshot carries no license grant; verify rights before redistribution or adding a license.

For an offline example and repeatable acceptance run, use `.\.venv\Scripts\python.exe verify_e2e.py` on Windows or `.venv/bin/python verify_e2e.py` on Mac/Linux. It prints an evidence directory outside the clone containing a generated geometric review ZIP you can import. See [E2E verification](E2E.md).
