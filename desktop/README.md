# Verity Desktop — live interview assistant

A standalone, always-on-top window that listens to a real interview and streams
an answer you can adapt while you respond. It has no login, signup, Verity web
account, workspace, database, or backend dependency.

Audio is segmented locally after a short pause, transcribed directly with Groq
Whisper, and answered through Groq streaming chat. You supply one or more Groq API keys in
the desktop app or through `VERITY_GROQ_API_KEY`/`GROQ_API_KEY`.

---

## Prerequisites

Needed on **every** platform:

- **Rust** (stable, 1.77+) — <https://rustup.rs>
- **Tauri CLI** — `cargo install tauri-cli --version "^2"`, which provides
  `cargo tauri`

The web UI is plain HTML/CSS/JS with no build step, so Node.js is *not*
required.

Then the platform-specific pieces:

| Platform | Also install |
|---|---|
| **Windows 10 / 11** | [Microsoft C++ Build Tools][msvc] — tick the **Desktop development with C++** workload. Plus [WebView2][webview2], already present on Windows 11 and current Windows 10. |
| **macOS** | Xcode Command Line Tools: `xcode-select --install`. For the macOS 13+ system-audio backend you need full Xcode 15+ (Swift 5.9+). |

[msvc]: https://visualstudio.microsoft.com/visual-cpp-build-tools/
[webview2]: https://developer.microsoft.com/microsoft-edge/webview2/

Verify the toolchain before building:

```bash
cd desktop/src-tauri && cargo tauri info
```

---

## The one thing to understand first

**Where the interviewer's voice comes from decides whether this works well.**

A microphone always works, but it only hears the interviewer through your
speakers — so it fails with headphones and picks up the room. Capturing the
call's audio directly is better, and how you get it depends on the OS:

| Platform | Best source | Setup needed |
|---|---|---|
| **Windows 10 2004+ / 11** | Your speakers, captured directly (WASAPI loopback) | **None.** Pick the entry marked *call audio*. |
| **macOS 13+** | System audio (ScreenCaptureKit) | None, but the build must enable it — see below. |
| **macOS 12 and older** | A loopback device (BlackHole) | Manual, see below. |
| Any | Microphone | Keep the call on speakers, no headphones. |

The setup screen lists every option, marks the ones that carry call audio, and
preselects the best one available.

### Windows

Nothing to install. Windows exposes every output device as a capture source, so
Verity lists your speakers/headphones directly — pick the one the call plays
through (it is preselected) and you are done. Headphones work fine.

### macOS 13+

Apple's ScreenCaptureKit taps system audio with no virtual device. It needs
Swift tools 5.9+ (Xcode 15+) at build time, so it is behind a Cargo feature:

```bash
cargo tauri build --features native-system-audio
```

That build shows **System Audio (Recommended)** in the device list. Grant
Screen Recording permission when macOS asks. A build made without the feature,
or running on macOS 12, falls back to the loopback route below.

### macOS 12 and older

No public API exists for system audio, so route it through a virtual device:

```bash
brew install blackhole-2ch
```

Then in **Audio MIDI Setup** (in `/Applications/Utilities`):

1. Create a **Multi-Output Device** containing your speakers/headphones *and*
   BlackHole 2ch.
2. Set that Multi-Output Device as your Mac's sound output.
3. In Verity Desktop, pick **BlackHole 2ch** as the input.

You now hear the call normally, and Verity hears exactly what you hear.

If the app hears nothing, the usual cause is a browser that picked its output
device before the Multi-Output Device existed: quit it completely, reopen, and
reselect the speaker in the call app's own audio settings.

---

## Running it

No backend or database is needed. Launch Verity from `/Applications`, or in
development:

```bash
cd desktop/src-tauri && cargo run --release
```

Optionally provide the key through the shell instead of the UI:

```bash
VERITY_GROQ_API_KEY=gsk_... cargo run --release
```

---

## Building the installer

Build on the OS you are targeting — Tauri does not cross-compile the installer,
because each platform's bundler needs that platform's own tooling.

```bash
cd desktop/src-tauri && cargo tauri build
```

### Windows

With the prerequisites above installed, the command above produces an `.exe`
NSIS installer and an `.msi` under `target/release/bundle/`.

The installer is unsigned, so SmartScreen shows "Windows protected your PC" on
first run — **More info → Run anyway**. Signing needs a code-signing
certificate.

### macOS

For one Intel + Apple Silicon package, install both Rust targets and build universal:

```bash
rustup target add x86_64-apple-darwin aarch64-apple-darwin
cargo tauri build --target universal-apple-darwin --bundles dmg,app
```

Produces a versioned universal `.dmg` and `.app` under
`target/universal-apple-darwin/release/bundle/`. Add
`--features native-system-audio` on macOS 13+ with Xcode 15+ to include the
ScreenCaptureKit audio backend.

**The build is unsigned.** Distributing a signed, notarized app needs a paid
Apple Developer certificate, which this project does not have. macOS will
refuse to open an unsigned app on first launch — the user opens it once with
**right-click → Open**, and confirms. After that it launches normally.

To sign later, set `APPLE_CERTIFICATE`, `APPLE_ID` and `APPLE_TEAM_ID` and add a
`signingIdentity` to `tauri.conf.json`.

---

## Using it in an interview

1. Paste one or more Groq API keys, one per line, and test the connection. They stay in this Mac's local Verity preferences and rotate on auth, rate-limit, or service failures.
2. Add the role/company and paste or import the resume and job description. PDF, TXT, and Markdown are supported.
3. Choose the interview audio source. A loopback device is recommended.
4. Press **Start live assistant**, then position the HUD beside your call.

The window stays above full-screen video calls. **Pinned** toggles that off if
you need it to behave like a normal window.

### Screen-capture protection

**Protect HUD from screen capture** is enabled by default. The control is
available before sign-in, on interview setup, and in the active HUD. Changes
are applied immediately to the native window and persisted in
`desktop-preferences.json` under the app's per-user config directory.

How the OS enforces it differs, and so does how well:

| Platform | Mechanism | In practice |
|---|---|---|
| **Windows 10 2004+ / 11** | `SetWindowDisplayAffinity` with `WDA_EXCLUDEFROMCAPTURE` | Reliable — the compositor itself omits the window. |
| **macOS 13+** | `NSWindowSharingNone` | Reliable with capturers built on ScreenCaptureKit. |
| **macOS 12 and older** | `NSWindowSharingNone` | Honoured inconsistently, especially for always-on-top windows. Verify before relying on it. |

On macOS, Tauri 2.11 delegates this to AppKit by changing the HUD window's
sharing type (`NSWindowSharingNone` while protected, `NSWindowSharingReadOnly`
while unprotected). This is content-capture protection only. It does **not**
hide the process from macOS. This build uses accessory-app mode, so it has no
Dock/taskbar item or normal Cmd+Tab entry; a menu-bar icon provides Show, Hide,
and Quit. It can still remain visible in Mission Control, Activity Monitor, and
to macOS. This is ordinary UI policy—not process hiding or “undetectability.”

Capture exclusion is OS- and capture-implementation-dependent. In particular,
this setting is not a promise that every third-party recorder or conferencing
client will honor AppKit window sharing restrictions, and it must never be
described as making the application "undetectable." See
`IMPLEMENTATION_PROGRESS.md` for the runtime verification matrix.

The microphone is released the moment you press **Stop** — the OS recording
indicator going out is the signal that nothing is being captured.

---

## What it looks for

Not every sentence is a question. A lightweight local detector recognizes
question punctuation and common interview prompts such as “tell me,” “how,”
“describe,” and “walk me through.” Small talk is transcribed but does not make
an answer request. "What it heard" confirms the audio path is active.

The HUD displays transcription, first-response, and total latency. The path is
optimized for low latency with a 360 ms silence flush, 16 kHz mono WAV, a reused
HTTP connection, `whisper-large-v3-turbo`, streamed output, and
`openai/gpt-oss-20b` at low reasoning effort. Sub-one-second service is a target,
not a guarantee: free-tier queueing, network time, and the required end-of-speech
pause are outside the application’s control.

---

## Layout

```
desktop/
  src-tauri/
    src/main.rs      window, commands, wiring
    src/audio.rs     device enumeration, capture, resampling to 16 kHz mono
    src/session.rs   local VAD, Groq Whisper, streamed Groq answers
  ui/                the HUD itself (no build step — plain HTML/CSS/JS)
```

Groq keys are never sent to the Verity web application. They are used only for
requests to `https://api.groq.com/openai/v1`.
