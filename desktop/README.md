# Verity Desktop — live interview assistant

A standalone, always-on-top window that listens to a real interview and streams
an answer you can adapt while you respond. It has no login, signup, Verity web
account, workspace, database, or backend dependency.

Audio is segmented locally after a short pause and transcribed directly with
Groq Whisper — that part always uses Groq. Who writes the suggested answer is
your choice: Groq, OpenAI, Anthropic (Claude), or Google Gemini, picked in
Advanced settings. You supply one or more Groq API keys in the desktop app or
through `VERITY_GROQ_API_KEY`/`GROQ_API_KEY`; a non-Groq answer provider needs
its own key, entered in the same settings panel.

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
cd src-tauri && cargo tauri info
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

## When nothing is being transcribed

Every capture failure looks the same from the HUD: it sits there and no
question appears. That covers several unrelated causes, and fixing the wrong
one wastes a build cycle, so work through these in order.

### 1. Rule out the machine itself, with no Verity code involved

`audio-probe` opens a device with the same primitive Verity uses (`cpal`) and
prints a live RMS meter — no API key, no network, none of Verity's own code in
the path. It ships in the same CI artifact as the installer
(`audio-probe.exe` on Windows), or build it yourself:

```bash
cargo run --release --manifest-path audio-probe/Cargo.toml
```

```
audio-probe            # capture the default output device (system audio)
audio-probe --list     # just enumerate what this machine exposes
audio-probe 3          # capture device 3 from that list
```

Play the interviewer's audio while it runs and read the meter:

| What you see | What it means |
|---|---|
| **RMS moves, "capture WORKS"** | The machine is fine. Continue to step 2 — this same device, selected inside Verity, is now the thing to test. |
| **"opened but delivered SILENCE"** | The device opened but carries no audio. The call is almost certainly playing through a *different* device — re-run against the other numbers, especially your headphones. |
| **"could not capture this device"** | That endpoint cannot be opened at all. Try another number from the list. |
| **No devices listed** | The OS sees no sound hardware. |

### 2. If the probe works but Verity still shows nothing

Verity writes its own append-only log next to `desktop-preferences.json`:

| Platform | Location |
|---|---|
| Windows | `%APPDATA%\dev.verity.assistant\debug.log` |
| macOS | `~/Library/Application Support/dev.verity.assistant/debug.log` |

It records, for every session: the exact device string the Start button sent,
whether the stream opened or the specific error if it didn't, and whether the
realtime capture callback ever delivered a single message. Reproduce the
failure, then open that file — the last few lines say exactly which of those
three failed, which is the difference between "select a different device" and
"file a bug."

### 3. If the transcript contains both your own voice and the interviewer's

Verity has no speaker separation — it transcribes whatever is in the captured
stream, whichever voice that is. If your own words show up mixed into the
same transcript as the interviewer's, the stream being captured already
contains both, before Verity ever sees it. That is a Windows/driver setting,
not something app code can filter after the fact:

- **Windows: check "Listen to this device."** Sound Control Panel → Recording
  tab → your microphone → Properties → Listen. If enabled, Windows plays your
  mic back through your speakers in real time — which loopback then dutifully
  captures right alongside the interviewer. Turn it off.
- Confirm you are actually on the loopback/system-audio device, not the
  microphone (check the label in the Interview audio dropdown, or `debug.log`
  from step 2). On the microphone, picking up both voices is expected: it
  hears you directly and the interviewer through your speakers, exactly as
  the table at the top of this README describes.
- A headset removes the acoustic path entirely (nothing to bleed from
  speaker to mic in the first place), so it is the most reliable fix when
  the above doesn't resolve it.

---

## Running it

No backend or database is needed. Launch Verity from `/Applications`, or in
development:

```bash
cd src-tauri && cargo run --release
```

Optionally provide the key through the shell instead of the UI:

```bash
VERITY_GROQ_API_KEY=gsk_... cargo run --release
```

---

## Building the installer

### Easiest: let GitHub build it

You do not need a toolchain at all. This repo has a workflow that builds on
real Windows and macOS machines:

1. Open the **Actions** tab → **Build** → **Run workflow** (it also runs on
   every push to `main`).
2. When it finishes, open the run and download from **Artifacts**:
   - `verity-windows` — the `.exe` installer and `.msi`
   - `verity-macos` — the `.dmg`

Artifacts are zipped by GitHub, so unzip before running the installer.

### Building locally

Build on the OS you are targeting — Tauri does not cross-compile the installer,
because each platform's bundler needs that platform's own tooling.

```bash
cd src-tauri && cargo tauri build
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

1. Paste one or more Groq API keys, one per line, and test the connection. They stay in this machine's local Verity preferences, rotate on auth/rate-limit/service failures, and always handle transcription regardless of which provider answers.
2. Add the role/company and paste or import the resume and job description. PDF, TXT, and Markdown are supported.
3. Choose the interview audio source. A loopback device is recommended.
4. *(Optional)* In **Advanced settings**, pick an **Answer provider** other than Groq — OpenAI, Anthropic, Gemini, or Amazon Bedrock — paste that provider's own key(s), and test the connection. Leave it on Groq to reuse the keys from step 1.
5. Press **Start live assistant**, then position the HUD beside your call.

The window stays above full-screen video calls. **Pinned** toggles that off if
you need it to behave like a normal window.

### Choosing an answer provider

There is no auto-detection of key type from its format — the **Answer
provider** dropdown is the one place this is chosen, and the key field
below it is only for whichever provider is currently selected.

| Provider | Needs its own key | Default model |
|---|---|---|
| Groq | No — reuses the keys above | `openai/gpt-oss-20b` |
| OpenAI | Yes | `gpt-4o-mini` |
| Anthropic (Claude) | Yes | `claude-haiku-4-5-20251001` |
| Google Gemini | Yes | `gemini-2.0-flash` |
| Amazon Bedrock | Yes — an [API key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html), not an AWS access key/secret pair | `amazon.nova-lite-v1:0` |

Every provider streams into the same HUD through the same events — switching
providers changes nothing else about how Verity behaves — with one exception:
**Bedrock's answer arrives as a single block, not token-by-token.** Its real
streaming API returns AWS's own binary frame format rather than the plain
text every other provider here uses, and that was never verified against a
real successful response while building this, so Verity calls its regular
(non-streaming) endpoint instead. For a 160-token answer the practical delay
is under a second — but it is a real, deliberate difference from the rest.

The **Answer model** field is free text, so any model the selected provider
hosts works, not only the default.

#### Amazon Bedrock: model access is a separate AWS step

A Bedrock bearer-token key authenticates you, but by itself grants no model
access — the console list won't tell you this, but your **first Bedrock
request will fail even with a perfectly valid key** if no model has been
enabled. Fixed at [AWS Bedrock console → Model access][bedrock-model-access],
not in this app. Until at least one model is enabled there, every request
fails with `Operation not allowed`, which looks identical to a bad key but
isn't one — verified live: the same key correctly listed 90 available models
via Bedrock's own models endpoint while every single invoke attempt was
rejected, which is what an auth-is-fine-but-nothing-is-enabled account looks
like.

[bedrock-model-access]: https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html

Requests per detected question: exactly one transcription call and one answer
call, regardless of provider — plus one extra answer call per API key skipped
on a retryable failure (rate limit, auth, 5xx). A silent, sub-threshold pause
never triggers a request at all; segmentation only fires once
`SILENCE_FLUSH_MS` of quiet follows at least `MIN_VOICE_MS` of real speech.

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
src-tauri/
  src/main.rs        window, commands, wiring
  src/audio.rs       device enumeration, capture, resampling to 16 kHz mono
  src/session.rs     local VAD, Groq Whisper, streamed Groq answers
  src/platform.rs    per-OS capability detection
ui/                  the HUD itself (no build step — plain HTML/CSS/JS)
```

Groq keys are never sent to the Verity web application. They are used only for
requests to `https://api.groq.com/openai/v1`.
