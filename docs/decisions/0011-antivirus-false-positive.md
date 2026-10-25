# 0011 — Packaging that antivirus models don't flag

Date: 2026-10-25

## Problem
Windows Defender deleted `wgmdownloader.exe` at download, first v1.0.1 and then, once the cloud verdict spread, v1.0.0 too: `Trojan:Script/Conteban.A!ml`. The `!ml` suffix marks a machine-learning verdict, not a signature match. The two builds share no suspect code with malware; what they share is the PyInstaller one-file shape (a stock prebuilt launcher that unpacks a compressed archive to a temp folder and runs it), which models flag often, plus a program that starts other programs, including PowerShell with `-NoProfile -NonInteractive`.

## Decision
- **Folder build, zipped.** The release is `wgmdownloader-windows.zip` holding `wgmdownloader/wgmdownloader.exe` and `_internal/`. Nothing unpacks at run time, which also makes start-up and the folder dialog faster.
- **Launcher compiled in CI.** PyInstaller is installed from its source release with `PYINSTALLER_COMPILE_BOOTLOADER=1`, so the launcher isn't the byte-identical prebuilt one that every PyInstaller program, malware included, ships.
- **PyInstaller pinned** (6.22.3) so a new release can't change the launcher unannounced.
- **No PowerShell from the executable.** It bundles Tk, whose dialog is the Explorer one. PowerShell stays as a fallback only when running from source without Tk.
- **Build attestation** (`actions/attest-build-provenance`) for the zip and the exe, so anyone can check a download came from this repository's workflow: `gh attestation verify`.
- The smoke test runs on the program unzipped from the release zip.

Rejected: UPX compression (raises detections); Nuitka (also flagged, slower builds); a console-less build (0007).

## Known limits
Unsigned code can still be flagged by a model at any time. What clears a verdict is a false-positive submission to Microsoft (microsoft.com/wdsi/filesubmission); what prevents most of them is code signing, e.g. SignPath's free programme for open-source projects.
