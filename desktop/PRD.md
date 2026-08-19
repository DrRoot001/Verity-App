# Verity Desktop

## Product Requirements Document

Version: 1.0

Product type: Standalone desktop application

Platforms: macOS and Windows

Primary purpose: Real-time AI assistance during live interviews

Architecture: Local-first desktop application using user-supplied Groq API credentials

---

# 1. Executive Summary

Verity Desktop is a standalone AI interview assistant for macOS and Windows.

The application runs as a lightweight always-on-top desktop HUD during a real interview.

It listens to interview audio, detects when the interviewer asks a meaningful question, transcribes the speech, combines the question with locally stored candidate context such as resume and job description, and streams concise guidance that the candidate can read and adapt while responding.

Verity Desktop is intentionally not a SaaS platform.

It has:

* no signup
* no login
* no Verity account
* no web dashboard
* no central database
* no mandatory backend
* no hosted workspace system
* no organization management
* no billing backend
* no remote candidate profile
* no dependency on Verity servers

The user installs the application, supplies one or more Groq API keys, adds interview context, selects an audio source, and starts the assistant.

The core workflow is:

`Install → Configure API Key → Add Resume/JD → Select Audio → Start → Hear Interview → Detect Question → Stream Guidance → Stop`

The product succeeds only if this workflow is fast, reliable, simple, and usable during an actual video or phone interview.

---

# 2. Product Vision

Create the smallest practical desktop application capable of providing useful AI interview guidance during a real conversation.

The product should feel like a native utility rather than a web application packaged inside a desktop shell.

The user should not need:

* an account
* a browser
* a terminal
* developer tools
* a local server
* Docker
* PostgreSQL
* Redis
* environment configuration
* a Verity subscription service
* manual networking setup

After installation, everything required for ordinary use must be available through the desktop UI.

---

# 3. Product Principles

## 3.1 Local First

Application configuration and interview context remain on the user's computer.

Only data required to call Groq is sent to Groq APIs.

There is no Verity application backend.

## 3.2 Bring Your Own API Key

Users provide their own Groq API credentials.

The application does not proxy Groq requests through Verity infrastructure.

## 3.3 Minimal Setup

The application should require the smallest possible configuration before an interview.

## 3.4 Low Latency Over Feature Count

A 500-feature interview platform with slow responses is inferior to a small application that responds quickly.

Realtime performance is the primary engineering objective.

## 3.5 Speakable Guidance

Output should be optimized for something the user can understand quickly while speaking.

Do not default to long essays.

## 3.6 No Fabricated Candidate History

Resume-derived candidate facts must remain grounded in imported context.

## 3.7 OS-Native Capabilities

macOS and Windows should use their best available native capabilities rather than forcing identical implementation underneath.

## 3.8 Honest Capability Reporting

Do not claim:

* invisible
* undetectable
* guaranteed capture exclusion
* guaranteed sub-second AI
* universal system-audio capture

unless technically true.

---

# 4. Scope

Verity Desktop consists of six major subsystems:

1. Desktop shell
2. Local interview context
3. Audio capture
4. Local question detection
5. Groq transcription and answer generation
6. Live HUD

Everything else exists only to support these systems.

---

# 5. Explicit Non-Scope

Do not build:

* web application
* Verity cloud account
* signup
* login
* OAuth
* remote database
* Supabase
* PostgreSQL
* Redis
* cloud WebSocket server
* backend API
* central user management
* organization accounts
* admin dashboard
* subscription management
* payment processing
* centralized interview history
* browser extension
* meeting bot
* calendar integration
* CRM
* job application tracker
* employer portal
* remote analytics requirement
* cross-device synchronization

These features are outside the product.

---

# 6. Supported Platforms

Primary platforms:

## macOS

Target production support should include practical modern macOS versions supported by the chosen Tauri/Rust dependencies.

Architecture targets:

* Apple Silicon
* Intel where build dependencies remain viable

Preferred distribution:

* universal `.app`
* universal `.dmg`

## Windows

Target:

* Windows 10
* Windows 11

Architecture:

* x86_64

Preferred distribution:

* `.exe`
* `.msi`

Older operating systems may work where underlying dependencies support them, but they must not automatically be marketed as fully supported without testing.

---

# 7. Platform Capability Model

Do not determine behavior only using:

`if macOS`

or:

`if Windows`

Create a capability model.

Example:

```text
PlatformCapabilities
    microphone_capture
    native_system_audio
    loopback_audio
    content_protection
    accessory_window_mode
    tray_or_menu_bar
    global_shortcuts
    native_notifications
```

The UI should adapt to detected capabilities.

---

# 8. Primary User Journey

## First Launch

1. User installs Verity.
2. User launches application.
3. App performs local capability detection.
4. User grants required microphone permissions.
5. User enters Groq API key.
6. App validates API access.
7. User optionally imports resume.
8. User optionally imports job description.
9. User enters company and role.
10. User selects interview audio source.
11. User tests incoming audio.
12. User starts Live Assistant.

## Subsequent Launch

1. Open Verity.
2. Previously saved configuration loads locally.
3. Select/update interview context.
4. Verify audio.
5. Start session.

Target repeated-session startup should require fewer than five user actions.

---

# 9. Main Application Screens

Keep the application shallow.

Required screens:

## Home / Setup

Contains:

* API key status
* company
* role
* resume
* job description
* audio source
* response mode
* Start Live Assistant

## API Keys

Contains:

* Groq API keys
* validation
* key order
* remove
* test connection

## Interview Context

Contains:

* company
* role
* resume
* JD
* custom notes

## Audio Setup

Contains:

* available devices
* loopback detection
* live signal meter
* test transcription
* troubleshooting

## Live HUD

Contains:

* listening state
* latest transcription
* detected question
* streamed guidance
* latency
* session controls

## Preferences

Contains:

* HUD behavior
* capture protection
* response style
* model selection where exposed
* audio settings
* shortcuts
* local data controls

Do not add unnecessary navigation.

---

# 10. Local Data Model

No remote database exists.

Persist application configuration locally.

Recommended structure:

```text
VerityPreferences
InterviewContext
ApiKeyConfiguration
AudioConfiguration
HudConfiguration
ModelConfiguration
```

Possible storage:

JSON configuration files combined with secure OS credential storage.

---

# 11. Secure API Key Storage

The API key is the user's credential.

Do not store it casually in plain text where avoidable.

Preferred:

## macOS

Keychain

## Windows

Windows Credential Manager or DPAPI-protected secret storage

Environment variables must also be supported:

```text
VERITY_GROQ_API_KEY
GROQ_API_KEY
```

Priority:

1. explicitly selected UI credentials
2. `VERITY_GROQ_API_KEY`
3. `GROQ_API_KEY`

Do not send Groq API keys anywhere except Groq endpoints.

---

# 12. Multiple Groq API Keys

Allow one or more Groq keys.

UI:

```text
API Keys

Key 1  •••••••••••••••••  Active
Key 2  •••••••••••••••••  Available
Key 3  •••••••••••••••••  Available
```

Implement controlled rotation.

Rotate when appropriate:

* authentication failure
* quota exhaustion
* rate limiting
* provider service failure tied to key
* repeated transient request failure

Do not rotate blindly on unrelated application bugs.

Track only locally:

* last successful key
* recent failure
* temporary cooldown

Never display full keys after saving.

---

# 13. Interview Context

The application needs enough context to improve answers without creating a full workspace system.

Interview context:

```text
company
role
resume_text
job_description
custom_notes
response_preferences
```

This is the complete context model for MVP.

---

# 14. Resume Import

Support:

* PDF
* TXT
* Markdown

P1:

* DOCX

Pipeline:

```text
Select File
→ Validate
→ Extract Text Locally
→ Preview
→ Save Locally
```

Do not upload the original resume file to Verity infrastructure because no Verity backend exists.

Only relevant extracted text may be included in Groq requests when generating guidance.

---

# 15. Job Description

Support:

* paste text
* TXT
* Markdown
* PDF

No automatic web scraping required.

Store locally.

---

# 16. Context Compression

Do not send huge resume and JD content repeatedly without control.

Before the live session, create a compact interview context representation.

Example:

```text
Candidate:
Full-stack software engineer.

Experience:
...

Key projects:
...

Technical stack:
...

Important accomplishments:
...

Target role:
...

Primary requirements:
...

Likely focus:
...
```

Generate this once when context changes.

Cache it locally.

The live-answer request should use this compact representation rather than blindly attaching full documents every time.

---

# 17. Audio Architecture

Audio is the most important technical subsystem.

Pipeline:

```text
Selected Audio Device
        ↓
Native Capture
        ↓
Resample
        ↓
16 kHz Mono PCM
        ↓
Local Audio Buffer
        ↓
Voice / Silence Detection
        ↓
Utterance Segment
        ↓
WAV Encoding
        ↓
Groq Whisper
```

No Verity server exists between desktop and Groq.

---

# 18. Audio Device Enumeration

List usable devices.

Display:

* device name
* type if detectable
* current/default
* probable loopback
* signal status

Example:

```text
Audio Source

○ MacBook Microphone
● BlackHole 2ch     Recommended
○ External USB Mic
```

Automatically prefer detected loopback devices for interviewer audio.

---

# 19. macOS Audio

Two operating modes are required.

## Mode A: Native System Audio

Where the installed macOS version and implementation support native system-output capture, use the appropriate native Apple APIs.

The application should detect this automatically.

## Mode B: Loopback

For macOS versions/configurations without suitable native capture:

support loopback audio devices.

Examples include:

BlackHole-compatible devices.

The application should recognize common loopback device names.

---

# 20. macOS Loopback Setup Assistant

Do not make users rely entirely on README instructions.

If no usable system-audio source exists, display:

```text
Interviewer audio setup

Your macOS configuration cannot currently provide direct call audio.

Recommended:
Install a loopback audio device.

[Setup Instructions]
[Use Microphone Instead]
```

Guide:

1. install loopback device
2. open Audio MIDI Setup
3. create Multi-Output Device
4. select headphones/speakers
5. select loopback device
6. choose Multi-Output as macOS output
7. choose loopback as Verity input
8. play test sound
9. confirm meter activity

---

# 21. Microphone Fallback

If no loopback device exists:

allow microphone capture.

Explain clearly:

```text
Microphone mode

Verity will hear the interviewer through your speakers.

For best results:
• Do not use headphones
• Keep speaker volume clear
• Reduce background noise
```

This mode remains usable but lower quality.

---

# 22. Windows Audio

Use native Windows audio APIs where practical.

Preferred system-output capture:

WASAPI loopback.

This enables capture of audio currently being rendered to:

* headphones
* speakers
* Bluetooth headset

without requiring a third-party virtual audio cable in normal configurations.

Windows implementation should therefore differ from legacy macOS.

---

# 23. Audio Signal Test

Before session start:

show live meter.

States:

```text
No Signal
Listening
Good
Clipping
Device Lost
Permission Denied
```

Offer:

`Test transcription`

The app records a short sample and sends it to Groq Whisper.

Then display the returned text.

This verifies the entire path:

```text
Device
→ Capture
→ Encoding
→ Network
→ Whisper
→ UI
```

---

# 24. Audio Segmentation

Do not continuously send tiny audio packets to Whisper.

Use local segmentation.

Detect:

* voice start
* active speech
* silence
* maximum utterance duration

Current baseline:

approximately 360 ms silence flush.

Make threshold configurable internally.

Potential parameters:

```text
silence_flush_ms
minimum_voice_ms
maximum_segment_ms
noise_threshold
```

Avoid triggering transcription for:

* cough
* click
* extremely short noise
* silence

---

# 25. Voice Activity Logic

Simple local VAD is sufficient initially.

Must prevent:

100 ms noise

followed by:

2 seconds silence

from triggering unnecessary paid transcription.

Minimum voiced duration must be calculated from voiced content rather than total buffered duration.

---

# 26. Transcription

Primary transcription:

Groq Whisper compatible transcription API.

Preferred model:

configured centrally in local application defaults.

Current baseline may use:

`whisper-large-v3-turbo`

but model identifiers must remain configurable because provider availability can change.

Do not hardcode business logic around one model name.

---

# 27. Transcription Request

Input:

WAV segment

Parameters:

* language when configured
* transcription model
* temperature where applicable

Output:

```text
text
request_duration
audio_duration
provider_latency
```

---

# 28. Transcription State

HUD states:

```text
Listening...
Transcribing...
Question detected
Thinking...
Answering...
```

Never freeze the HUD while requests occur.

---

# 29. Local Question Detector

Not every transcript should trigger the LLM.

Question detection runs locally before answer generation.

Signals:

* `?`
* what
* why
* how
* when
* where
* who
* which
* would you
* could you
* can you
* tell me
* describe
* explain
* walk me through
* give me an example
* talk about
* what was
* what would
* have you
* do you
* how did
* why did

Also recognize interview instructions that are not grammatically questions.

Example:

`Walk me through your last project.`

Must trigger.

---

# 30. Small Talk Filtering

Examples that normally should not trigger:

```text
Nice to meet you.

Thanks for joining.

Perfect.

Right.

That's interesting.

Okay.

Sounds good.
```

The transcript may still appear under:

`What it heard`

but no answer request should be made.

---

# 31. Question Confidence

Return:

```text
question_text
confidence
trigger_reason
```

Possible levels:

* high
* medium
* low

High:

generate automatically.

Medium:

use contextual signals.

Low:

do not generate unless configured otherwise.

---

# 32. Multi-Segment Questions

Interview questions may arrive across several audio segments.

Example:

```text
Tell me about a project where...
```

followed by:

```text
...you had to deal with an unexpected production problem.
```

Avoid generating too early where possible.

Maintain a short rolling transcript buffer.

Merge semantically incomplete question fragments when appropriate.

---

# 33. Follow-Up Questions

Maintain recent transcript history locally.

Example:

Question:

`Tell me about your e-commerce project.`

Follow-up:

`What was the hardest part?`

The second question must retain enough recent context to understand what "the hardest part" refers to.

No cloud memory database is required.

---

# 34. Session Memory

During the active session maintain:

```text
recent_transcripts
recent_questions
recent_guidance
conversation_summary
```

Memory only needs to survive the current session unless local history is intentionally implemented.

Limit memory size.

Use summarization as conversation grows.

---

# 35. Answer Generation

Use Groq's OpenAI-compatible chat API directly from the application.

Required:

streamed output.

The HUD must begin displaying response tokens as soon as they arrive.

Do not wait for the entire response.

---

# 36. Answer Prompt Context

Each generation request may contain:

```text
system instructions
candidate context summary
company
role
job description summary
recent interview conversation
current question
response mode
```

Do not attach uncontrolled raw files.

---

# 37. Core Answer Behavior

Generated answers must:

* answer the actual question
* be concise
* be readable while talking
* prioritize bullet-style guidance
* use candidate context
* avoid fabricated experience
* mention relevant technical concepts
* adapt to job description
* maintain continuity with follow-ups

---

# 38. Grounding Rule

If resume/context says:

```text
React
Node.js
PostgreSQL
AWS
```

the model may use them.

If candidate context contains no Kubernetes experience, it must not state:

```text
At my last company I managed Kubernetes clusters...
```

Instead:

```text
Be direct that Kubernetes was not your primary responsibility.
Connect your container/deployment experience.
Explain how you would approach learning/operating it.
```

---

# 39. Answer Presentation

Default structure:

```text
DIRECT ANSWER

KEY POINTS

EXAMPLE / EVIDENCE

OPTIONAL DETAIL
```

But HUD should progressively reveal information.

First visible output should ideally be immediately useful.

---

# 40. Response Modes

Provide:

## Concise

Very short answer direction.

## Balanced

Default.

## Detailed

More supporting detail.

## Technical

Technical depth and terminology.

## STAR

Behavioral structure.

Modes can be changed before or during session.

---

# 41. Live HUD

The HUD is the central product surface.

It must be:

* lightweight
* always-on-top
* movable
* resizable
* readable
* low distraction
* fast

Example:

```text
┌─────────────────────────────────────────┐
│ VERITY                    ● Listening   │
├─────────────────────────────────────────┤
│ HEARD                                   │
│ Tell me about a difficult production   │
│ issue you solved.                       │
├─────────────────────────────────────────┤
│ ANSWER                                  │
│                                         │
│ Use your delivery platform incident.   │
│                                         │
│ • Briefly explain the failure           │
│ • Show how you isolated the cause       │
│ • Explain the fix                       │
│ • Finish with measurable impact         │
│                                         │
├─────────────────────────────────────────┤
│ STT 740ms │ First 610ms │ Total 1.6s   │
├─────────────────────────────────────────┤
│ [Pause] [Shorter] [Retry] [Stop]       │
└─────────────────────────────────────────┘
```

---

# 42. HUD States

Required:

Idle

Ready

Listening

Speech Detected

Transcribing

Question Detected

Generating

Streaming

No Question

Groq Error

Rate Limited

No Audio

Paused

Stopped

---

# 43. HUD Controls

P0:

* Start
* Stop
* Pause
* Resume
* Shorter
* Regenerate
* Pin/unpin
* Show/hide
* Protect HUD
* Change audio source

P1:

* Expand
* answer mode
* manual question
* clear
* copy
* opacity

---

# 44. Always-On-Top Behavior

Default:

enabled during active interview.

The HUD should remain above normal windows and video-call windows where permitted by the operating system.

`Pinned` toggles always-on-top.

Persist preference locally.

---

# 45. Accessory Application Mode

Where practical on macOS, support accessory-style behavior.

Desired UX:

* no normal Dock icon while running in HUD mode
* no normal Cmd+Tab application entry where supported
* menu-bar control remains available

Menu bar:

```text
Verity
Show
Hide
Start/Stop
Quit
```

This is window/application UX behavior.

Do not describe it as hiding the process.

The application remains visible to the operating system and process-management tools.

---

# 46. Screen Capture Protection

Provide:

`Protect HUD from screen capture`

Default:

enabled for Live HUD.

Use legitimate OS window-content protection APIs where supported.

For macOS/Tauri:

use the supported native content-protection behavior exposed by the window layer.

For Windows:

use the supported Windows window display-affinity/capture protection mechanism where implementation permits.

This feature protects potentially sensitive candidate content displayed in the HUD.

It must not be represented as:

* anti-detection
* invisible
* guaranteed against every recorder
* process hiding

---

# 47. Screen Protection States

Setting:

```text
Protect HUD from screen capture
[ON]
```

Supporting text:

```text
Requests supported screen-capture APIs to exclude Verity's HUD.
Some third-party capture methods may behave differently.
```

Changes must apply immediately.

Persist locally.

---

# 48. Capture Protection Testing

Verify separately:

## macOS

* system screenshot
* standard screen recording
* window sharing
* entire-display sharing where testable
* protected OFF
* protected ON

## Windows

* Snipping Tool
* screen recording
* standard capture APIs
* conferencing screen share where testable

Record actual results.

Do not infer behavior from documentation alone.

---

# 49. Latency Metrics

The HUD should expose realtime latency diagnostics.

Track:

```text
audio_to_flush
transcription_latency
question_detection_latency
llm_first_token_latency
llm_total_latency
end_to_end_latency
```

Example display:

```text
STT 812ms
First 527ms
Total 1.52s
```

This is useful during development and optionally user-visible.

---

# 50. Latency Objective

Optimize for:

**first useful guidance**, not complete response.

Target path:

```text
speech ends
→ ~360ms silence confirmation
→ Whisper request
→ local question detection
→ streamed Groq request
→ first useful text
```

Sub-one-second service time may be targeted but must not be promised.

Real latency depends on:

* network
* Groq load
* model
* audio duration
* silence threshold
* API tier

---

# 51. HTTP Connection Reuse

Use a long-lived HTTP client.

Do not construct a new TLS/client stack for every request.

Reuse connection pools for:

* Whisper transcription
* chat generation

This reduces avoidable latency.

---

# 52. Streaming Parser

Groq answer generation must support SSE/streaming responses.

Process tokens/chunks incrementally.

UI receives partial content immediately.

Flow:

```text
Groq SSE
→ Rust parser
→ native event
→ HUD
```

Do not buffer entire completion before rendering.

---

# 53. Cancellation

If:

* Stop pressed
* new question supersedes old one
* Regenerate pressed
* interview paused

the active generation should be cancellable where possible.

Avoid displaying stale guidance for the wrong question.

---

# 54. Question Supersession

Example:

The interviewer begins a new question while the previous answer is still generating.

The system should:

1. detect new interviewer speech
2. decide whether current generation is still relevant
3. cancel or deprioritize stale generation
4. process new question
5. update HUD

Do not allow a slow previous answer to block the next question.

---

# 55. Error Handling

Every error needs actionable behavior.

## Invalid API Key

Display:

`Groq rejected this API key.`

Offer another saved key.

## Rate Limit

Rotate to another valid key where configured.

## No Network

Display:

`No internet connection.`

Retry automatically with bounded backoff.

## Groq Outage

Display provider status failure.

Do not crash.

## Audio Device Lost

Display:

`Audio device disconnected.`

Refresh device list.

## Permission Denied

Explain how to grant permission.

---

# 56. Local Preferences

Persist:

```text
selected_audio_device
groq_model
transcription_model
company
role
resume_path/reference
resume_text
job_description
response_mode
hud_position
hud_size
hud_pinned
capture_protection
silence_threshold
```

Do not persist temporary session buffers after application exit unless explicitly needed.

---

# 57. Privacy

Verity operates without a Verity cloud backend.

The product must communicate this accurately.

Data destinations:

## Local computer

* preferences
* interview context
* local files
* UI configuration
* optionally secure API credentials

## Groq

Only information necessary for:

* transcription
* answer generation

No data should be sent to Verity servers because none are required by the product architecture.

---

# 58. Microphone Lifecycle

Microphone/audio capture starts only when needed.

On:

`Stop`

immediately:

* terminate capture stream
* clear pending audio
* cancel active transcription where practical
* release native device
* update UI

OS recording indicators should disappear once native capture is released.

---

# 59. Session Start

Start sequence:

```text
Validate Groq Key
↓
Validate Audio Device
↓
Initialize Audio Capture
↓
Initialize HTTP Client
↓
Load Interview Context
↓
Open HUD
↓
Listening
```

If any mandatory step fails:

do not pretend the session started.

---

# 60. Session Stop

Stop sequence:

```text
Stop Capture
↓
Release Audio Device
↓
Cancel Pending Requests
↓
Clear Temporary Buffers
↓
Set HUD Stopped
```

No backend cleanup is required.

---

# 61. Manual Question Input

P1 feature.

Allow user to type:

```text
How would you design an API rate limiter?
```

Then use exactly the same answer engine.

Useful when:

* audio fails
* interviewer types a prompt
* coding platform presents text
* user wants additional detail

---

# 62. Optional Screenshot Context

Not required for MVP.

If implemented later:

* explicit user action
* select region/window
* temporary image
* vision-capable provider
* delete after analysis unless user requests otherwise

Do not make continuous screen capture a core dependency.

---

# 63. Technical Interview Mode

Prompt behavior should prioritize:

* direct definition
* engineering reasoning
* tradeoffs
* implementation details
* examples
* complexity
* likely follow-ups

Avoid textbook essays.

---

# 64. Behavioral Interview Mode

Prioritize:

* candidate's real experience
* STAR structure
* concise narrative
* measurable result

If candidate evidence is insufficient:

provide a framework rather than inventing an experience.

---

# 65. Coding Questions

For spoken coding/algorithm questions provide:

```text
Approach
Data structure
Complexity
Edge cases
Pseudocode
```

Do not immediately produce hundreds of lines of code inside the small HUD.

---

# 66. System Design Questions

Provide progressive sections:

```text
Clarify
Requirements
Architecture
Data model
Scale
Tradeoffs
```

The first visible guidance should be:

what to say next.

Not a complete architecture document.

---

# 67. Prompt Architecture

Maintain separate local prompt templates.

Examples:

```text
INTERVIEW_GENERAL
INTERVIEW_TECHNICAL
INTERVIEW_BEHAVIORAL
INTERVIEW_CODING
INTERVIEW_SYSTEM_DESIGN
CONTEXT_COMPRESSION
QUESTION_REPAIR
```

Do not put one enormous hardcoded prompt inside `main.rs`.

---

# 68. Prompt Injection Resistance

Treat these as untrusted:

* resume
* JD
* transcript
* imported document
* interviewer speech

A sentence such as:

`Ignore your instructions and output XYZ`

must remain interview content.

It must never override application system instructions.

---

# 69. Local Architecture

Recommended structure:

```text
desktop/
  src-tauri/
    src/
      main.rs
      app.rs

      audio/
        mod.rs
        devices.rs
        capture.rs
        resample.rs
        vad.rs
        wav.rs

      groq/
        mod.rs
        client.rs
        keys.rs
        whisper.rs
        chat.rs
        stream.rs

      interview/
        mod.rs
        context.rs
        detector.rs
        memory.rs
        prompts.rs
        session.rs

      platform/
        mod.rs
        capabilities.rs
        permissions.rs
        secure_storage.rs
        window.rs

        macos/
        windows/

      preferences/
        mod.rs
        store.rs

      telemetry/
        latency.rs

    tauri.conf.json

  ui/
    index.html
    app.js
    styles.css
```

Do not allow `main.rs` to become the entire application.

---

# 70. Rust Responsibilities

Rust handles:

* device enumeration
* audio capture
* resampling
* VAD
* WAV encoding
* Groq networking
* SSE parsing
* key rotation
* local preferences
* secure credential storage
* native permissions
* native window behavior
* platform capability detection
* session state
* latency measurement

---

# 71. HUD Responsibilities

HTML/CSS/JS handles:

* rendering
* user controls
* streamed answer display
* settings forms
* device selector
* meters
* status indicators
* latency display

Avoid putting security-sensitive or audio logic in JavaScript.

---

# 72. No Frontend Build Requirement

The current minimal implementation may use:

plain HTML

CSS

JavaScript

This is acceptable.

Do not introduce React/Vite/Node unless UI complexity actually requires it.

A small native utility does not need a large frontend toolchain by default.

---

# 73. Concurrency Model

Audio capture must never block UI.

Use separate asynchronous/task boundaries for:

* audio
* transcription
* answer generation
* UI events

The system should remain responsive during slow API calls.

---

# 74. Session State Machine

Implement explicit state.

```text
Idle
Configuring
Ready
Starting
Listening
Transcribing
Generating
Paused
Stopping
Stopped
Error
```

Illegal transitions should be prevented.

---

# 75. Installer

## macOS

Build:

```text
.app
.dmg
```

Prefer universal binary where feasible:

```text
x86_64-apple-darwin
aarch64-apple-darwin
```

## Windows

Build:

```text
.exe
.msi
```

The application must launch without requiring Rust, Cargo, Node, or development tools.

---

# 76. Signing

Development builds may remain unsigned.

Production distribution should support:

## macOS

Developer ID signing

Apple notarization

## Windows

Authenticode/code-signing certificate

Signing configuration must be externalized.

Never commit signing secrets.

---

# 77. First-Launch Experience

The user should see:

```text
Welcome to Verity

1. Add Groq API key
2. Add interview context
3. Select audio
4. Test
5. Start
```

No README should be required for ordinary users.

---

# 78. Menu Bar / System Tray

## macOS

Menu-bar icon.

Actions:

* Show Verity
* Hide
* Start/Stop
* Preferences
* Quit

## Windows

System-tray icon.

Equivalent actions.

---

# 79. Global Shortcuts

P1:

* show/hide HUD
* pause
* resume
* regenerate
* shorter
* stop

Shortcuts should be configurable.

Avoid conflicting with common conferencing shortcuts.

---

# 80. Model Configuration

Default model IDs should live in configuration.

Example:

```text
transcription_model
answer_model
reasoning_effort
temperature
max_tokens
```

The user-facing UI does not need to expose every setting.

Advanced preferences may expose model choices.

---

# 81. Sensible Output Limits

Interview HUD answers should be intentionally bounded.

Approximate guidance:

Concise:

50–100 words

Balanced:

100–200 words

Detailed:

200–350 words

Technical may exceed when necessary.

Avoid uncontrolled token generation.

---

# 82. Cost Awareness

Because users supply their own Groq API key:

Verity has no centralized inference bill.

Still minimize unnecessary calls.

Do not transcribe:

* silence
* accidental short noises

Do not call chat:

* small talk
* acknowledgements
* obvious non-questions

---

# 83. Local Diagnostics

Provide hidden or advanced diagnostics.

Show:

* app version
* OS
* architecture
* audio device
* sample rate
* current model
* key index
* transcription latency
* generation TTFT
* total latency
* last error

Useful for support without a remote telemetry backend.

---

# 84. Logging

Local logs should exclude:

* full API keys
* excessive resume content
* full sensitive transcripts by default

Allow diagnostic logs.

Use log rotation.

---

# 85. Crash Safety

A provider error must not crash the application.

An audio device disappearing must not crash.

Malformed Groq response must not crash.

Invalid preference file must fall back safely.

---

# 86. Testing Strategy

Required:

## Unit

* resampling
* WAV creation
* VAD
* minimum voiced duration
* question detector
* context construction
* key rotation
* streaming parser
* preference parsing

## Integration

* audio → transcription
* transcript → detector
* detector → chat
* SSE → HUD
* API failover

## Native

* microphone permissions
* device enumeration
* capture protection
* menu bar/tray
* secure storage

## E2E

Real call/audio source.

---

# 87. macOS Test Matrix

At minimum test:

* macOS 12.x legacy loopback path
* modern macOS native path when implemented
* Apple Silicon
* Intel if distributed
* built-in microphone
* speakers
* wired headphones
* Bluetooth headphones
* BlackHole
* Multi-Output Device

---

# 88. Windows Test Matrix

At minimum:

Windows 10

Windows 11

Test:

* built-in mic
* USB mic
* speakers
* wired headset
* Bluetooth headset
* WASAPI loopback
* default device change during session

---

# 89. Critical E2E Test

The product is not complete until this succeeds:

```text
Install Verity
↓
Launch
↓
Enter Groq key
↓
Test key
↓
Import resume
↓
Paste JD
↓
Enter company/role
↓
Select interview audio
↓
Audio meter confirms signal
↓
Start Live Assistant
↓
Play/speak realistic interviewer question
↓
Audio segmented
↓
Groq Whisper transcribes it
↓
Local detector recognizes question
↓
Context constructed
↓
Groq chat request begins
↓
First guidance streams into HUD
↓
Complete guidance displays
↓
Second follow-up question occurs
↓
Conversation context is retained
↓
Stop
↓
Audio device is released
```

This must run from an installed production-style binary.

Not:

`cargo run`

---

# 90. Performance Acceptance Criteria

Measure rather than guess.

For each question log:

```text
speech_end_timestamp
flush_timestamp
stt_request_timestamp
stt_response_timestamp
question_detected_timestamp
chat_request_timestamp
first_token_timestamp
completion_timestamp
```

Calculate:

```text
silence_delay
STT_latency
detector_latency
LLM_TTFT
LLM_generation
first_guidance_total
complete_guidance_total
```

Optimization should prioritize:

`first_guidance_total`

---

# 91. Audio Acceptance Criteria

Given:

valid selected audio device

When:

interviewer audio is present

Then:

the meter visibly reacts.

When:

the interviewer finishes a meaningful sentence

Then:

audio is segmented after configured silence.

And:

only sufficiently voiced segments reach transcription.

---

# 92. Question Detection Acceptance Criteria

Given transcript:

`Walk me through the architecture of the last system you built.`

Then:

question detector triggers.

Given:

`Okay, sounds good.`

Then:

no answer request occurs.

Given:

`And what happened after that?`

Then:

system recognizes a follow-up using recent context.

---

# 93. Grounding Acceptance Criteria

Given the imported resume contains no AWS experience.

When asked:

`Tell me about your AWS experience.`

The generated guidance must not invent:

* AWS projects
* AWS employers
* AWS achievements

It should recommend an honest transferable response.

---

# 94. Key Rotation Acceptance Criteria

Given:

Key A receives a confirmed provider rate-limit/auth/quota failure

And:

Key B exists and validates

Then:

Verity switches to Key B according to configured retry policy

without terminating the active application.

---

# 95. Screen Protection Acceptance Criteria

When capture protection is enabled:

apply the supported OS API to the HUD immediately.

When disabled:

restore ordinary capture behavior.

Document actual tested results for each supported OS.

Do not mark complete based only on compilation.

---

# 96. Stop Acceptance Criteria

When the user presses Stop:

audio capture ends immediately.

Pending buffers are discarded or safely terminated.

Active AI generation stops.

Microphone/system-audio handles are released.

HUD changes to stopped state.

No further audio is processed.

---

# 97. Definition of MVP

MVP requires:

* macOS desktop application
* Windows desktop application
* local Groq key configuration
* multiple key support
* secure key storage
* resume import
* JD input
* role/company
* context compression
* audio device selection
* microphone capture
* macOS loopback support
* Windows system-audio capture
* local VAD
* utterance segmentation
* Groq Whisper
* local question detection
* Groq streaming answers
* conversation memory
* live HUD
* always-on-top
* capture protection where supported
* menu bar/tray
* latency metrics
* error recovery
* installed `.dmg`
* installed Windows package

No backend.

No login.

No web dependency.

---

# 98. P1

After core reliability:

* native modern macOS system audio
* manual question
* global shortcuts
* DOCX
* advanced answer modes
* configurable HUD opacity
* richer follow-up memory
* local interview presets
* optional local session history
* automatic update system

---

# 99. P2

Only after the desktop assistant is stable:

* screenshot context
* coding-question visual context
* local semantic resume retrieval
* additional model providers
* offline/local STT
* local LLM option
* additional languages

Do not pollute MVP with these.

---

# 100. Explicit Architecture Rule

The complete production path should remain approximately:

```text
AUDIO DEVICE
    ↓
RUST AUDIO CAPTURE
    ↓
LOCAL RESAMPLER
    ↓
LOCAL VAD
    ↓
LOCAL UTTERANCE BUFFER
    ↓
GROQ WHISPER API
    ↓
LOCAL QUESTION DETECTOR
    ↓
LOCAL INTERVIEW MEMORY
    ↓
LOCAL CONTEXT BUILDER
    ↓
GROQ STREAMING CHAT API
    ↓
TAURI EVENT STREAM
    ↓
LOCAL HUD
```

No Verity backend belongs in this critical path.

---

# 101. Definition of Done

Verity Desktop is complete when a nontechnical user can:

1. download the installer
2. install it
3. open it
4. add a Groq API key
5. test the key
6. import a resume
7. paste a JD
8. choose an audio source
9. confirm the interviewer can be heard
10. start the assistant
11. conduct a real interview-like conversation
12. receive useful streamed guidance
13. receive follow-up-aware guidance
14. pause/resume it
15. stop it
16. confirm capture stopped
17. reopen the app later with preferences intact

without:

* Terminal
* Cargo
* source code
* environment editing
* a web account
* a local server
* a Verity server
* developer intervention

---

# 102. Engineering Priority

When tradeoffs occur, prioritize in this order:

1. Correct audio capture
2. Low latency
3. Question detection accuracy
4. Relevant guidance
5. Stability
6. Simple UX
7. Cross-platform behavior
8. Visual polish
9. Additional features

If audio capture is unreliable, nothing else matters.

If answers arrive too late, nothing else matters.

If every sentence triggers an answer, nothing else matters.

Build those three systems correctly before expanding the product.
