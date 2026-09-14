# Why nothing was transcribed — root cause and fix

**Status: root cause found and fixed.** The capture pipeline was never broken.
Every backend stage worked. The events carrying the results to the UI were
being **denied by Tauri's permission system** and failing silently.

---

## The evidence that identified it

The final `debug.log` from Windows was decisive:

```
calibrated from 150 samples: voice_threshold=0.0030, level_reference=0.0180
level: peak_rms=0.1157 (threshold=0.0030, calibrated=true) over last 2000ms
level: peak_rms=0.1548 (threshold=0.0030, calibrated=true) over last 2000ms
level: peak_rms=0.1698 (threshold=0.0030, calibrated=true) over last 2000ms
bridge: 26000 messages relayed so far
```

Read together with the earlier runs, this proves, on the user's own hardware:

| Stage | Status | Evidence |
|---|---|---|
| Device selection | works | `device = "Speakers / Headphones (2- Realtek Audio)"` |
| Stream open (WASAPI loopback) | works | `audio::start: stream opened successfully` |
| Realtime capture callback | works | `bridge: first audio message received` |
| Sustained capture | works | 26,000+ messages over 4+ min through a **64-slot bounded channel** — impossible unless the session loop consumes in real time |
| Audio actually audible | works | `peak_rms` up to **0.1698**, ~57x the 0.0030 threshold |
| Voice detection | works | every loud window is far above threshold |
| **Events reaching the UI** | **BROKEN** | meter dead, nothing transcribed, no answers |

Only the last stage could fail while all the others logged success.

---

## Root cause: Tauri v2 grants zero permissions by default

Tauri v2 replaced v1's allowlist with a capability system. The rule that
matters:

> If there is no `capabilities/` directory, **no permissions are granted at
> all.**

This project had none. The generated ACL confirmed it:

```
$ cat src-tauri/gen/schemas/capabilities.json
{}
```

An empty object — nothing permitted.

### Why `invoke` worked but `listen` did not

This is the detail that made the bug so confusing, because it made the app
look half-alive:

- **`invoke("start_listening")` — worked.** Commands *your own app* defines
  with `#[tauri::command]` are not ACL-gated by default. So the button
  worked, the stream opened, preferences saved. Everything looked fine.
- **`listen("verity://event")` — denied.** `listen` is not app code; it is a
  call into Tauri's built-in **event core plugin**, which *is* ACL-gated. It
  requires this chain:

  ```
  core:default → core:event:default → allow-listen
  ```

  With capabilities `{}`, none of that is granted, so the IPC call is
  rejected.

### Why it failed silently

In `ui/main.js` the call was top-level and its promise was never awaited or
caught:

```js
listen("verity://event", ({ payload }) => { ... });   // returns a Promise
```

A rejected promise with no `.catch()` is an unhandled rejection. In the
webview, with no console open, that is **completely invisible**. The listener
simply never registered, and every `audio.level`, `stt.final`,
`question.finalized` and `answer.delta` event the backend emitted was dropped
on the floor.

This is why the symptoms were identical on **macOS and Windows** despite
completely different audio backends — the failure was never in audio at all.

---

## The fix

**1. Grant the permission** — `src-tauri/capabilities/default.json`:

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "default",
  "windows": ["main"],
  "permissions": ["core:default"]
}
```

Verified after rebuild — previously `{}`:

```
capabilities defined: ['default']
 identifier: default | windows: ['main'] | permissions: ['core:default']

core:default expands to: [core:path:default, core:event:default, ...]
core:event:default grants: [allow-listen, allow-unlisten, allow-emit, allow-emit-to]
=> allow-listen granted: True
```

**2. Stop the silent failure** — `ui/main.js` now routes both `listen` calls
through a wrapper that catches rejection and writes the reason into the
visible error line, so an ACL denial can never again present as "audio
doesn't work".

---

## What was already fixed on the way here

These were real defects found and corrected while narrowing this down. They
were genuine bugs, but none of them were *this* bug:

| Fix | Why it was needed |
|---|---|
| `withGlobalTauri: true` | `window.__TAURI__` was undefined; the entire JS bundle threw on line 3, so no button worked at all. |
| Windows loopback enumeration | `list_devices` only listed *input* devices, so the Windows system-audio tap was never offered. |
| `default_output_config` fallback | A render endpoint reports no input config; opening one for loopback failed without this. |
| Self-calibrating voice threshold | The fixed `0.012` was tuned on macOS; Windows loopback levels differ by an order of magnitude between machines. |
| Debug logging | Release builds set `windows_subsystem = "windows"`, so every `eprintln!` went nowhere. This log is what ultimately located the bug. |
| `audio-probe` | Standalone capture test that proved the OS and `cpal` were fine, ruling out the entire audio stack. |

---

## Verifying the fix

After installing the new build, `debug.log` should look the same as before —
it was already correct. The difference is in the **UI**: the level meter moves
with `peak_rms`, transcripts appear, and answers stream in.

If anything still fails, the HUD will now show a concrete error instead of
sitting silent, because of fix #2 above.
