/** Standalone Verity desktop interview assistant. */

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);

let captureProtectionEnabled = true;
let devices = [];
let currentQuestion = "";
let threadTurns = 0;

// ── Providers and keys ──────────────────────────────────────────────
//
// Keys are kept per provider, so the answer provider and the transcription
// provider can be any pair, and switching back and forth never loses a key.
// Each textarea shows one provider's keys at a time; `bound*` records whose,
// so typed keys are stored under the right provider before the box is
// re-pointed at another one.

// Filled from the app itself (get_desktop_settings), so this page never
// carries its own copy of the provider list or default models.
let providerInfo = [];
let providerKeys = {};
let boundAnswerProvider = null;
let boundSttProvider = null;

const DISPLAY_NAMES = { anthropic: "Anthropic (Claude)", gemini: "Google Gemini", bedrock: "Amazon Bedrock" };
const info = (id) => providerInfo.find((p) => p.id === id);
const labelOf = (id) => info(id)?.label ?? id;
const canTranscribe = (id) => Boolean(info(id)?.can_transcribe);
const defaultModelOf = (id) => info(id)?.default_chat_models?.[0] ?? "";

function keysIn(textareaId) {
  return [...new Set($(textareaId).value.split(/[\n,]+/).map((key) => key.trim()).filter(Boolean))];
}

function commitKeyFields() {
  if (boundAnswerProvider) providerKeys[boundAnswerProvider] = keysIn("answer-keys");
  if (boundSttProvider) providerKeys[boundSttProvider] = keysIn("stt-keys");
}

const hasKeys = (id) => (providerKeys[id] ?? []).length > 0;

/** Mirrors providers::resolve_stt_provider in the app. */
function resolveStt() {
  const choice = $("stt-provider").value;
  const chat = $("chat-provider").value;
  if (choice !== "auto") return { provider: choice, automatic: false, found: true };
  const found = ["groq", chat, "openai", "gemini"].find((id) => canTranscribe(id) && hasKeys(id));
  return {
    // Nothing usable yet: suggest the answer provider if it can hear,
    // otherwise Groq, the fastest transcription.
    provider: found ?? (canTranscribe(chat) ? chat : "groq"),
    automatic: true,
    found: Boolean(found),
  };
}

function bindTextarea(textareaId, provider, current) {
  if (provider !== current) $(textareaId).value = (providerKeys[provider] ?? []).join("\n");
  return provider;
}

function refreshProviders() {
  commitKeyFields();
  const chat = $("chat-provider").value;
  boundAnswerProvider = bindTextarea("answer-keys", chat, boundAnswerProvider);
  $("answer-keys-label").textContent = `${labelOf(chat)} API keys`;
  $("chat-model").placeholder = `Default: ${defaultModelOf(chat)}`;

  const stt = resolveStt();
  const separate = stt.provider !== chat;
  $("stt-keys-row").classList.toggle("hidden", !separate);
  boundSttProvider = separate ? bindTextarea("stt-keys", stt.provider, boundSttProvider) : null;
  $("stt-keys-label").textContent = `${labelOf(stt.provider)} API keys (transcription)`;

  const chatName = labelOf(chat);
  const sttName = labelOf(stt.provider);
  let hint;
  if (stt.automatic && !stt.found) {
    hint = canTranscribe(chat)
      ? `Add a ${chatName} key above — it will transcribe too.`
      : `${chatName} can write answers but can't hear audio. Add a Groq key below (fastest), or choose OpenAI or Gemini — only the transcript is sent to ${chatName}.`;
  } else if (!separate) {
    hint = `${sttName} transcribes too — one key does both.`;
  } else {
    hint = `${sttName} transcribes the audio; ${chatName} only receives the transcript.`;
    if (stt.provider === "groq") hint += " Groq Whisper is the fastest option.";
  }
  $("stt-hint").textContent = hint;
}

function updateContextCounts() {
  $("resume-count").textContent = `${$("resume-text").value.length.toLocaleString()} characters`;
  $("job-count").textContent = `${$("job-description").value.length.toLocaleString()} characters`;
}

// ── Conversation thread ─────────────────────────────────────────────

// Appended once per finished turn, on "answer.complete" — not built up live
// on every "answer.delta", so a streaming answer only ever touches the DOM
// nodes in the live cards above, not a growing thread list on every token.
function appendThreadTurn(question, answer, generation) {
  if (!question && !answer) return;
  $("thread-empty")?.remove();
  const item = document.createElement("div");
  item.className = "thread-item";
  if (generation != null) item.dataset.generation = String(generation);
  const q = document.createElement("p");
  q.className = "thread-q";
  q.textContent = question || "(question not transcribed)";
  const a = document.createElement("p");
  a.className = "thread-a";
  a.textContent = answer || "(no answer)";
  item.append(q, a);
  $("thread-list").append(item);
  threadTurns += 1;
  renderThreadCount();
  item.scrollIntoView({ block: "nearest" });
}

/** An answer to part of a question, replaced once the rest was heard. */
function removeThreadTurn(generation) {
  const item = $("thread-list").querySelector(`[data-generation="${generation}"]`);
  if (!item) return;
  item.remove();
  threadTurns -= 1;
  renderThreadCount();
}

function renderThreadCount() {
  $("thread-count").textContent = threadTurns ? `${threadTurns} turn${threadTurns === 1 ? "" : "s"}` : "";
}

function resetThread() {
  currentQuestion = "";
  threadTurns = 0;
  renderThreadCount();
  $("thread-list").innerHTML =
    '<p id="thread-empty" class="thread-empty">Questions and answers will collect here as the interview goes, so you can scroll back through what was already asked.</p>';
}

function show(id) {
  for (const pane of document.querySelectorAll(".pane")) pane.classList.add("hidden");
  $(id).classList.remove("hidden");
}

function renderCaptureProtection(enabled) {
  captureProtectionEnabled = enabled;
  $("protect-capture").checked = enabled;
  $("protect-btn").setAttribute("aria-pressed", String(enabled));
  $("protect-btn").textContent = enabled ? "Shield" : "Unshielded";
}

async function setCaptureProtection(enabled, errorTarget) {
  const previous = captureProtectionEnabled;
  renderCaptureProtection(enabled);
  $(errorTarget).textContent = "";
  try {
    const setting = await invoke("set_capture_protection", { enabled });
    renderCaptureProtection(setting.enabled);
  } catch (error) {
    renderCaptureProtection(previous);
    $(errorTarget).textContent = String(error);
  }
}

function updateDeviceHint() {
  const chosen = devices.find((device) => device.name === $("device").value);
  $("device-hint").textContent = chosen?.is_native_system_audio
    ? "Recommended: hears call audio directly from macOS, no extra setup and works with headphones."
    : chosen?.is_loopback
    ? "Recommended: this captures the interviewer's call audio and works with headphones."
    : "Microphone input selected. The interview must play through speakers for the interviewer to be heard.";
}

async function initialize() {
  $("setup-error").textContent = "";
  try {
    const [settings, availableDevices] = await Promise.all([
      invoke("get_desktop_settings"),
      invoke("list_audio_devices"),
    ]);
    providerInfo = settings.providers ?? [];
    providerKeys = settings.provider_keys ?? {};
    $("chat-provider").innerHTML = providerInfo
      .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(DISPLAY_NAMES[p.id] ?? p.label)}</option>`)
      .join("");
    $("chat-provider").value = settings.chat_provider || "groq";
    $("stt-provider").value = settings.stt_provider || "auto";
    $("chat-model").value = settings.chat_model ?? "";
    $("role-title").value = settings.role_title ?? "";
    $("company-name").value = settings.company_name ?? "";
    $("resume-text").value = settings.resume_text ?? "";
    $("job-description").value = settings.job_description ?? "";
    $("language").value = settings.language || "en";
    boundAnswerProvider = null;
    boundSttProvider = null;
    refreshProviders();
    renderCaptureProtection(settings.protect_hud_from_screen_capture !== false);
    updateContextCounts();

    devices = availableDevices;
    $("device").innerHTML = devices
      .map((device) => {
        const label = device.is_native_system_audio
          ? "System Audio (Recommended)"
          : escapeHtml(device.name);
        const suffix = device.is_loopback ? " — call audio" : device.is_default ? " — default" : "";
        return `<option value="${escapeHtml(device.name)}">${label}${suffix}</option>`;
      })
      .join("");
    const preferred =
      devices.find((device) => device.is_native_system_audio) ??
      devices.find((device) => device.is_loopback) ??
      devices.find((device) => device.is_default);
    if (preferred) $("device").value = preferred.name;
    updateDeviceHint();
  } catch (error) {
    $("setup-error").textContent = String(error);
  }
}

async function saveSettings() {
  commitKeyFields();
  const saved = await invoke("save_desktop_settings", {
    providerKeys,
    roleTitle: $("role-title").value.trim(),
    companyName: $("company-name").value.trim(),
    resumeText: $("resume-text").value.trim(),
    jobDescription: $("job-description").value.trim(),
    language: $("language").value,
    chatModel: $("chat-model").value.trim(),
    chatProvider: $("chat-provider").value,
    sttProvider: $("stt-provider").value,
  });
  providerKeys = saved.provider_keys ?? providerKeys;
  return saved;
}

// FileReader's own base64 encoder, not a manual byte-array-over-JSON
// transfer: a JS number-array of file bytes bloats 3-4x once JSON-encoded
// for IPC, with no progress feedback while it serialized — for a multi-MB
// PDF that looked exactly like the import had silently hung.
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(",") + 1));
    reader.onerror = () => reject(reader.error ?? new Error("Could not read the file."));
    reader.readAsDataURL(file);
  });
}

async function importDocument(button) {
  const input = $(button.dataset.file);
  const file = input.files?.[0];
  if (!file) throw new Error("Choose a PDF, TXT, or Markdown document first.");
  const originalLabel = button.textContent;
  button.disabled = true;
  try {
    button.textContent = "Reading…";
    const contents = await readFileAsBase64(file);
    button.textContent = "Extracting…";
    const text = await invoke("extract_document_text", { fileName: file.name, contents });
    $(button.dataset.target).value = text;
    updateContextCounts();
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

// The real <input type="file"> is visually hidden (styles.css .file-input)
// and pointer-events: none, so it cannot be clicked directly — clicking the
// visible button has to forward to it.
for (const button of document.querySelectorAll(".import-button")) {
  const input = $(button.dataset.file);
  button.addEventListener("click", () => input.click());
  input.addEventListener("change", async () => {
    $("setup-error").textContent = "";
    try {
      await importDocument(button);
    } catch (error) {
      $("setup-error").textContent = String(error);
    } finally {
      // Clear so choosing the exact same file again still fires "change".
      input.value = "";
    }
  });
}

$("resume-text").addEventListener("input", updateContextCounts);
$("job-description").addEventListener("input", updateContextCounts);
$("chat-provider").addEventListener("change", refreshProviders);
$("stt-provider").addEventListener("change", refreshProviders);
// Automatic transcription depends on which keys exist, so the choice (and
// whether a second key box is needed) follows what is typed.
$("answer-keys").addEventListener("input", refreshProviders);
$("stt-keys").addEventListener("input", refreshProviders);

/** A real request, not just a key check: a one-word answer with the chosen
 * model, or a real transcription — so a retired model or a missing model
 * grant shows up here instead of in the interview. */
async function runProviderTest(button, hintId, provider, textareaId, kind) {
  button.disabled = true;
  $("setup-error").textContent = "";
  const original = $(hintId).textContent;
  try {
    const apiKeys = keysIn(textareaId);
    if (!apiKeys.length) throw new Error(`Add at least one ${labelOf(provider)} API key first.`);
    $(hintId).textContent = "Testing…";
    await saveSettings();
    const result = await invoke("test_provider", {
      provider,
      apiKeys,
      model: kind === "answers" ? $("chat-model").value.trim() : "",
      kind,
    });
    $(hintId).textContent = `✓ ${result.detail} in ${result.latency_ms} ms · key ${result.working_key} of ${result.total_keys}`;
  } catch (error) {
    $(hintId).textContent = original;
    $("setup-error").textContent = String(error);
  } finally {
    button.disabled = false;
  }
}

$("test-answer-btn").addEventListener("click", () =>
  runProviderTest($("test-answer-btn"), "answer-key-hint", $("chat-provider").value, "answer-keys", "answers"),
);
$("test-stt-btn").addEventListener("click", () =>
  runProviderTest($("test-stt-btn"), "stt-key-hint", resolveStt().provider, "stt-keys", "transcription"),
);

$("device").addEventListener("change", updateDeviceHint);
$("protect-capture").addEventListener("change", (event) => {
  setCaptureProtection(event.currentTarget.checked, "setup-error");
});

$("start-btn").addEventListener("click", async () => {
  const button = $("start-btn");
  button.disabled = true;
  $("setup-error").textContent = "";
  try {
    commitKeyFields();
    const chat = $("chat-provider").value;
    if (!hasKeys(chat)) throw new Error(`Add at least one ${labelOf(chat)} API key first.`);
    if (!$("device").value) throw new Error("No audio input device is available.");
    await saveSettings();
    // The app checks the transcription side too and explains what is
    // missing before the audio device is opened.
    await invoke("start_listening", { device: $("device").value });
    $("dot").classList.add("live");
    $("status").textContent = "Listening";
    $("latency").textContent = "Waiting for a question";
    $("question").textContent = "Listening for a question…";
    $("question").classList.remove("partial");
    $("direction").textContent = "Your answer will stream here as soon as a question is detected.";
    $("points").innerHTML = "";
    $("heard").textContent = "Listening for audio…";
    $("level-fill").style.width = "0%";
    $("pulse-ring").classList.remove("voiced");
    show("hud");
  } catch (error) {
    $("setup-error").textContent = String(error);
  } finally {
    button.disabled = false;
  }
});

$("stop-btn").addEventListener("click", async () => {
  await invoke("stop_listening");
  $("dot").classList.remove("live");
  $("level-fill").style.width = "0%";
  $("pulse-ring").classList.remove("voiced");
  show("setup");
});

$("pin-btn").addEventListener("click", async () => {
  const pinned = $("pin-btn").getAttribute("aria-pressed") !== "true";
  await invoke("set_always_on_top", { pinned });
  $("pin-btn").setAttribute("aria-pressed", String(pinned));
  $("pin-btn").textContent = pinned ? "Pinned" : "Unpinned";
});

$("protect-btn").addEventListener("click", () => {
  setCaptureProtection(!captureProtectionEnabled, "hud-error");
});

function renderAnswer(content) {
  const points = content.key_points ?? [];
  // A points-only answer (a definition/explanation question) legitimately
  // has no lead-in sentence — that must not read as "nothing came back".
  $("direction").textContent =
    content.answer_direction || (points.length ? "" : "No answer was returned.");
  $("points").innerHTML = points
    .map((point) => `<li>${escapeHtml(point.text ?? point)}</li>`)
    .join("");
  $("structure").textContent = content.structure || "";
}

function escapeHtml(text) {
  const node = document.createElement("span");
  node.textContent = String(text ?? "");
  return node.innerHTML;
}

function latencyText(data, complete = false) {
  const first = data.first_response_ms;
  const total = data.total_ms;
  if (complete && total != null) {
    const model = data.model ? ` · ${data.model}` : "";
    return `First word ${first ?? "—"} ms · done ${total} ms${model}`;
  }
  if (first != null) return `First word ${first} ms`;
  if (data.latency_ms != null) return `Transcribed ${data.latency_ms} ms`;
  return "Processing";
}

// listen() is a core-plugin IPC call, so it is subject to Tauri's ACL: with
// no capability granting core:event it rejects, and an uncaught rejection
// here is invisible — the app looks alive while every backend event is
// silently dropped. Surface it instead of letting it fail quietly.
function listenOrReport(event, handler) {
  listen(event, handler).catch((error) => {
    const message = `Cannot receive backend events (${event}): ${error}`;
    const target = $("hud-error") || $("setup-error");
    if (target) target.textContent = message;
    console.error(message);
  });
}

// Answers are numbered by the app. Only the current one may touch the
// cards: a superseded answer can still land a last token or two after it
// was replaced, and must not overwrite the one that replaced it.
let currentGeneration = null;
let liveAnswer = "";
let answerDone = true;

listenOrReport("verity://event", ({ payload }) => {
  const { kind, payload: data } = payload;
  switch (kind) {
    case "session.ready":
      $("status").textContent = "Listening";
      $("latency").textContent = `${data.stt_provider ?? ""} → ${data.chat_provider ?? ""} · ${data.chat_model ?? ""}`;
      currentGeneration = null;
      liveAnswer = "";
      answerDone = true;
      resetThread();
      break;
    case "stt.started":
      $("status").textContent = "Transcribing";
      $("latency").textContent = `Transcribing · ${data.provider ?? ""}`;
      break;
    case "stt.final":
      $("heard").textContent = String(data.content ?? "").slice(0, 180);
      $("latency").textContent = latencyText(data);
      break;
    case "speech.ignored":
      if (answerDone) $("status").textContent = "Listening";
      break;
    case "audio.level": {
      const level = Math.max(0, Math.min(1, Number(data.level) || 0));
      $("level-fill").style.width = `${Math.round(level * 100)}%`;
      $("pulse-ring").classList.toggle("voiced", Boolean(data.voiced));
      break;
    }
    case "audio.silence": {
      const seconds = Math.round((data.silence_ms ?? 0) / 1000);
      $("status").textContent = "No audio detected";
      $("heard").textContent = `No sound detected for ${seconds}s — check your audio source and volume in Setup.`;
      break;
    }
    case "question.partial":
      // The interviewer paused mid-sentence; the rest is on its way.
      $("question").textContent = String(data.content ?? "");
      $("question").classList.add("partial");
      $("status").textContent = "Listening to the rest…";
      break;
    case "question.finalized":
      if (data.replaces != null) {
        // The earlier answer covered only part of this question.
        removeThreadTurn(data.replaces);
      } else if (!answerDone && liveAnswer) {
        // A new question interrupted an answer still streaming: keep what
        // was shown, marked as cut off, so it can be scrolled back to.
        appendThreadTurn(currentQuestion, `${liveAnswer} …`, currentGeneration);
      }
      currentGeneration = data.generation;
      liveAnswer = "";
      answerDone = false;
      currentQuestion = String(data.content ?? "");
      $("question").textContent = currentQuestion;
      $("question").classList.remove("partial");
      $("direction").textContent = "Generating your answer…";
      $("points").innerHTML = "";
      $("structure").textContent = "";
      $("status").textContent = "Question detected";
      break;
    case "answer.delta":
      if (data.generation !== currentGeneration) break;
      liveAnswer = String(data.content ?? "");
      $("direction").textContent = liveAnswer;
      $("status").textContent = "Answering";
      $("latency").textContent = latencyText(data);
      break;
    case "answer.complete": {
      if (data.generation !== currentGeneration) break;
      answerDone = true;
      const content = data.content ?? {};
      renderAnswer(content);
      // A points-only answer has no lead-in sentence; without folding the
      // points in here too, the thread would log an empty "A" line.
      const points = content.key_points ?? [];
      const threadAnswer = [content.answer_direction, ...points.map((p) => `• ${p.text ?? p}`)]
        .filter(Boolean)
        .join("\n");
      appendThreadTurn(currentQuestion, threadAnswer, data.generation);
      $("status").textContent = "Ready";
      $("latency").textContent = latencyText(data, true);
      break;
    }
    case "warning":
      if (data.generation != null && data.generation !== currentGeneration) break;
      if (data.generation != null) answerDone = true;
      $("status").textContent = "Needs attention";
      $("hud-error").textContent = String(data.message ?? "The AI request failed.");
      break;
    case "session.ended":
      $("dot").classList.remove("live");
      $("status").textContent = "Stopped";
      break;
  }
});

listenOrReport("verity://closed", () => {
  $("dot").classList.remove("live");
  // A user-initiated Stop already navigated back to setup. Reaching here
  // while the HUD is still visible means the session ended on its own
  // (e.g. the audio device was lost) - surface that instead of leaving a
  // stale "Listening" HUD with no way forward.
  if (!$("hud").classList.contains("hidden")) {
    $("setup-error").textContent = $("hud-error").textContent || "The live session ended unexpectedly.";
    $("hud-error").textContent = "";
    show("setup");
  }
});

initialize();
