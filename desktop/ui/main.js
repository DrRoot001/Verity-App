/** Standalone Verity desktop interview assistant. */

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);

let captureProtectionEnabled = true;
let devices = [];

function apiKeys() {
  return [...new Set($('groq-keys').value.split(/[\n,]+/).map((key) => key.trim()).filter(Boolean))];
}

function updateKeyHint(message) {
  const count = apiKeys().length;
  $('key-hint').textContent = message || `${count} key${count === 1 ? '' : 's'} saved locally · automatic failover in listed order`;
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
    $("chat-model").value = settings.chat_model || "allam-2-7b";
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
  });
}

async function importDocument(button) {
  const input = $(button.dataset.file);
  const file = input.files?.[0];
  if (!file) throw new Error('Choose a PDF, TXT, or Markdown document first.');
  button.disabled = true;
  try {
    const bytes = Array.from(new Uint8Array(await file.arrayBuffer()));
    const text = await invoke('extract_document_text', { fileName: file.name, bytes });
    $(button.dataset.target).value = text;
    updateContextCounts();
  } finally {
    button.disabled = false;
  }
}

for (const button of document.querySelectorAll('.import-button')) {
  button.addEventListener('click', async () => {
    $('setup-error').textContent = '';
    try { await importDocument(button); } catch (error) { $('setup-error').textContent = String(error); }
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

function renderAnswer(content) {
  $("direction").textContent = content.answer_direction || "No answer was returned.";
  $("points").innerHTML = (content.key_points ?? [])
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

listen("verity://event", ({ payload }) => {
  const { kind, payload: data } = payload;
  switch (kind) {
    case "session.ready":
      $("status").textContent = "Listening";
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
      $("question").textContent = String(data.content ?? "");
      $("direction").textContent = "Generating your answer…";
      $("points").innerHTML = "";
      $("structure").textContent = "";
      $("status").textContent = "Question detected";
      break;
    case "answer.delta":
      $("direction").textContent = String(data.content ?? "");
      $("status").textContent = "Answering";
      $("latency").textContent = latencyText(data);
      break;
    case "answer.complete":
      renderAnswer(data.content ?? {});
      $("status").textContent = "Ready";
      $("latency").textContent = latencyText(data, true);
      break;
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

listen("verity://closed", () => {
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
