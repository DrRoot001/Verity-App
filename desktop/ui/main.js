/** Standalone Verity desktop interview assistant. */

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);

let captureProtectionEnabled = true;
let devices = [];
let currentQuestion = "";
let threadTurns = 0;

// Appended once per finished turn, on "answer.complete" — not built up live
// on every "answer.delta", so a streaming answer only ever touches the DOM
// nodes in the live cards above, not a growing thread list on every token.
function appendThreadTurn(question, answer) {
  if (!question && !answer) return;
  $("thread-empty")?.remove();
  const item = document.createElement("div");
  item.className = "thread-item";
  const q = document.createElement("p");
  q.className = "thread-q";
  q.textContent = question || "(question not transcribed)";
  const a = document.createElement("p");
  a.className = "thread-a";
  a.textContent = answer || "(no answer)";
  item.append(q, a);
  $("thread-list").append(item);
  threadTurns += 1;
  $("thread-count").textContent = `${threadTurns} turn${threadTurns === 1 ? "" : "s"}`;
  item.scrollIntoView({ block: "nearest" });
}

function resetThread() {
  currentQuestion = "";
  threadTurns = 0;
  $("thread-count").textContent = "";
  $("thread-list").innerHTML =
    '<p id="thread-empty" class="thread-empty">Questions and answers will collect here as the interview goes, so you can scroll back through what was already asked.</p>';
}

function parseKeys(textareaId) {
  return [...new Set($(textareaId).value.split(/[\n,]+/).map((key) => key.trim()).filter(Boolean))];
}

function apiKeys() {
  return parseKeys('groq-keys');
}

function chatApiKeys() {
  return parseKeys('chat-api-keys');
}

function updateKeyHint(message) {
  const count = apiKeys().length;
  $('key-hint').textContent = message || `${count} key${count === 1 ? '' : 's'} saved locally · automatic failover in listed order`;
}

// A provider other than Groq needs its own key, since Groq's keys only
// authenticate against Groq's API — reusing this instead of a literal
// string keeps every place that decides "does this need its own keys" in
// sync with the one place the provider list itself is defined.
function chatProviderNeedsOwnKeys() {
  return $('chat-provider').value !== 'groq';
}

const CHAT_PROVIDER_DEFAULT_MODEL = {
  // Verified live: openai/gpt-oss-20b reliably switches to bullet points
  // for a definitional question and never leaked a "You:" label across
  // repeated tests, where the faster allam-2-7b did neither reliably.
  groq: 'openai/gpt-oss-20b',
  openai: 'gpt-4o-mini',
  anthropic: 'claude-haiku-4-5-20251001',
  gemini: 'gemini-2.0-flash',
  // Requires enabling model access for this model in the AWS Bedrock
  // console before it will actually invoke — a mandatory one-time AWS
  // account step, unrelated to this app. Bearer-token auth alone doesn't
  // grant it.
  bedrock: 'amazon.nova-lite-v1:0',
};

function updateChatProviderVisibility() {
  const needsOwnKeys = chatProviderNeedsOwnKeys();
  $('chat-keys-row').classList.toggle('hidden', !needsOwnKeys);
  // Only auto-fill when the field still holds a known default (or is
  // empty) — an explicit override the user typed must never be clobbered
  // by switching providers back and forth.
  const current = $('chat-model').value.trim();
  const isKnownDefault = current === '' || Object.values(CHAT_PROVIDER_DEFAULT_MODEL).includes(current);
  if (isKnownDefault) {
    $('chat-model').value = CHAT_PROVIDER_DEFAULT_MODEL[$('chat-provider').value] ?? '';
  }
}

function updateContextCounts() {
  $('resume-count').textContent = `${$('resume-text').value.length.toLocaleString()} characters`;
  $('job-count').textContent = `${$('job-description').value.length.toLocaleString()} characters`;
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
    $("groq-keys").value = (settings.groq_api_keys ?? []).join("\n");
    $("role-title").value = settings.role_title ?? "";
    $("company-name").value = settings.company_name ?? "";
    $("resume-text").value = settings.resume_text ?? "";
    $("job-description").value = settings.job_description ?? "";
    $("language").value = settings.language || "en";
    $("chat-provider").value = settings.chat_provider || "groq";
    $("chat-api-keys").value = (settings.chat_api_keys ?? []).join("\n");
    $("chat-model").value = settings.chat_model || CHAT_PROVIDER_DEFAULT_MODEL[$("chat-provider").value] || "openai/gpt-oss-20b";
    updateChatProviderVisibility();
    renderCaptureProtection(settings.protect_hud_from_screen_capture !== false);
    updateKeyHint();
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
  return invoke("save_desktop_settings", {
    groqApiKeys: apiKeys(),
    roleTitle: $("role-title").value.trim(),
    companyName: $("company-name").value.trim(),
    resumeText: $("resume-text").value.trim(),
    jobDescription: $("job-description").value.trim(),
    language: $("language").value,
    chatModel: $("chat-model").value.trim(),
    chatProvider: $("chat-provider").value,
    chatApiKeys: chatApiKeys(),
  });
}

// FileReader's own base64 encoder, not a manual byte-array-over-JSON
// transfer: a JS number-array of file bytes bloats 3-4x once JSON-encoded
// for IPC, with no progress feedback while it serialized — for a multi-MB
// PDF that looked exactly like the import had silently hung.
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(',') + 1));
    reader.onerror = () => reject(reader.error ?? new Error('Could not read the file.'));
    reader.readAsDataURL(file);
  });
}

async function importDocument(button) {
  const input = $(button.dataset.file);
  const file = input.files?.[0];
  if (!file) throw new Error('Choose a PDF, TXT, or Markdown document first.');
  const originalLabel = button.textContent;
  button.disabled = true;
  try {
    button.textContent = 'Reading…';
    const contents = await readFileAsBase64(file);
    button.textContent = 'Extracting…';
    const text = await invoke('extract_document_text', { fileName: file.name, contents });
    $(button.dataset.target).value = text;
    updateContextCounts();
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

// The real <input type="file"> is visually hidden (styles.css .file-input)
// and pointer-events: none, so it cannot be clicked directly — clicking the
// visible button has to forward to it. Without this, "Import resume" never
// opened a file picker at all: importDocument() ran immediately against
// input.files, which was always empty, and always failed with "Choose a
// document first," on every click, unconditionally.
for (const button of document.querySelectorAll('.import-button')) {
  const input = $(button.dataset.file);
  button.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    $('setup-error').textContent = '';
    try {
      await importDocument(button);
    } catch (error) {
      $('setup-error').textContent = String(error);
    } finally {
      // Clear so choosing the exact same file again still fires "change".
      input.value = '';
    }
  });
}

$('groq-keys').addEventListener('input', () => updateKeyHint());
$('resume-text').addEventListener('input', updateContextCounts);
$('job-description').addEventListener('input', updateContextCounts);

$('test-api-btn').addEventListener('click', async () => {
  const button = $('test-api-btn');
  button.disabled = true;
  $('setup-error').textContent = '';
  try {
    if (!apiKeys().length) throw new Error('Add at least one Groq API key first.');
    await saveSettings();
    const result = await invoke('test_groq_connection');
    updateKeyHint(`Key ${result.working_key} connected in ${result.latency_ms} ms · ${result.total_keys} configured`);
  } catch (error) {
    $('setup-error').textContent = String(error);
  } finally {
    button.disabled = false;
  }
});

$('chat-provider').addEventListener('change', updateChatProviderVisibility);

$('test-chat-btn').addEventListener('click', async () => {
  const button = $('test-chat-btn');
  button.disabled = true;
  $('setup-error').textContent = '';
  try {
    const provider = $('chat-provider').value;
    const keys = chatApiKeys();
    if (!keys.length) throw new Error('Add at least one API key for the selected provider first.');
    await saveSettings();
    const result = await invoke('test_chat_provider', { provider, apiKeys: keys });
    $('chat-key-hint').textContent = `Key ${result.working_key} connected in ${result.latency_ms} ms · ${result.total_keys} configured`;
  } catch (error) {
    $('setup-error').textContent = String(error);
  } finally {
    button.disabled = false;
  }
});

$("device").addEventListener("change", updateDeviceHint);
$("protect-capture").addEventListener("change", (event) => {
  setCaptureProtection(event.currentTarget.checked, "setup-error");
});

$("start-btn").addEventListener("click", async () => {
  const button = $("start-btn");
  button.disabled = true;
  $("setup-error").textContent = "";
  try {
    if (!apiKeys().length) throw new Error("Add at least one Groq API key first.");
    if (!$("device").value) throw new Error("No audio input device is available.");
    await saveSettings();
    await invoke("start_listening", { device: $("device").value });
    $("dot").classList.add("live");
    $("status").textContent = "Listening";
    $("latency").textContent = "Waiting for a question";
    $("question").textContent = "Listening for a question…";
    $("direction").textContent = "Your answer will stream here as soon as a question is detected.";
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

function renderAnswer(content, grounding) {
  const points = content.key_points ?? [];
  const spoken = content.spoken_answer ?? "";
  $("answer").textContent = spoken;
  // The direction line is a one-glance compression of the same answer. With
  // the full answer on screen it is duplication, so it only shows without one.
  // A points-only answer (a definition/explanation question) legitimately has
  // no lead-in sentence — that must not read as "nothing came back".
  $("direction").textContent = spoken
    ? ""
    : content.answer_direction || (points.length ? "" : "No answer was returned.");
  const loose = grounding?.unverified ?? [];
  $("unverified").textContent = loose.length
    ? `Not in your approved evidence: ${loose.join(", ")} — check before saying it.`
    : "";
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
  if (complete && total != null) return `First ${first ?? "—"} ms · total ${total} ms`;
  if (first != null) return `First response ${first} ms`;
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

listenOrReport("verity://event", ({ payload }) => {
  const { kind, payload: data } = payload;
  switch (kind) {
    case "session.ready":
      $("status").textContent = "Listening";
      resetThread();
      break;
    case "stt.started":
      $("status").textContent = "Transcribing";
      $("latency").textContent = "Groq Whisper";
      break;
    case "stt.final":
      $("heard").textContent = String(data.content ?? "").slice(0, 180);
      $("latency").textContent = latencyText(data);
      break;
    case "speech.ignored":
      $("status").textContent = "Listening";
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
    case "question.finalized":
      currentQuestion = String(data.content ?? "");
      $("question").textContent = currentQuestion;
      $("direction").textContent = "Generating your answer…";
      $("answer").textContent = "";
      $("unverified").textContent = "";
      $("points").innerHTML = "";
      $("structure").textContent = "";
      $("status").textContent = "Question detected";
      break;
    case "answer.delta":
      $("direction").textContent = String(data.content ?? "");
      $("status").textContent = "Answering";
      $("latency").textContent = latencyText(data);
      break;
    case "answer.complete": {
      const content = data.content ?? {};
      renderAnswer(content, data.grounding);
      // A points-only answer has no lead-in sentence; without folding the
      // points in here too, the thread would log an empty "A" line for
      // every definition/explanation question.
      const points = content.key_points ?? [];
      const threadAnswer = [
        content.spoken_answer || content.answer_direction,
        ...points.map((p) => `• ${p.text ?? p}`),
      ]
        .filter(Boolean)
        .join("\n");
      appendThreadTurn(currentQuestion, threadAnswer);
      $("status").textContent = "Ready";
      $("latency").textContent = latencyText(data, true);
      break;
    }
    case "warning":
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
