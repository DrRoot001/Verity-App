// Verity live interview assistant.
//
// A standalone, always-on-top window that listens to the interview and shows
// low-latency guidance. It needs no Verity web account or backend.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod audio;
mod debuglog;
#[cfg(all(target_os = "macos", feature = "native-system-audio"))]
mod native_audio;
mod platform;
mod preferences;
mod session;

use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;
use tauri::menu::MenuBuilder;
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, State};
use tokio::sync::mpsc;

/// Either capture backend, unified so the rest of the app (session
/// lifecycle, AppState, stop_listening) never needs to know which one is
/// live. Dropping either variant releases the underlying device.
enum CaptureHandle {
    // Held only so dropping it releases the device; never pattern-matched.
    #[allow(dead_code)]
    Loopback(audio::Capture),
    #[cfg(all(target_os = "macos", feature = "native-system-audio"))]
    #[allow(dead_code)]
    Native(native_audio::NativeCapture),
}

#[derive(Default)]
struct AppState {
    stop: Mutex<Option<mpsc::Sender<()>>>,
    /// Held for the life of the session; dropping it stops the audio device.
    capture: Mutex<Option<CaptureHandle>>,
    preferences_path: Mutex<Option<PathBuf>>,
    preferences: Mutex<preferences::Preferences>,
    debug_log_path: Mutex<Option<PathBuf>>,
}

#[derive(Serialize)]
struct StartResult {
    session_id: String,
}

#[derive(Serialize)]
struct DesktopSettings {
    groq_api_keys: Vec<String>,
    role_title: String,
    company_name: String,
    resume_text: String,
    job_description: String,
    language: String,
    chat_model: String,
    chat_provider: String,
    chat_api_keys: Vec<String>,
    protect_hud_from_screen_capture: bool,
}

impl From<&preferences::Preferences> for DesktopSettings {
    fn from(value: &preferences::Preferences) -> Self {
        Self {
            groq_api_keys: value.groq_api_keys.clone(),
            role_title: value.role_title.clone(),
            company_name: value.company_name.clone(),
            resume_text: value.resume_text.clone(),
            job_description: value.job_description.clone(),
            language: value.language.clone(),
            chat_model: value.chat_model.clone(),
            chat_provider: value.chat_provider.clone(),
            chat_api_keys: value.chat_api_keys.clone(),
            protect_hud_from_screen_capture: value.protect_hud_from_screen_capture,
        }
    }
}

fn normalize_api_keys(keys: Vec<String>) -> Vec<String> {
    let mut normalized = Vec::new();
    for key in keys {
        let key = key.trim();
        if !key.is_empty() && !normalized.iter().any(|saved| saved == key) {
            normalized.push(key.to_string());
        }
    }
    normalized
}

#[tauri::command]
async fn list_audio_devices() -> Result<Vec<audio::AudioDevice>, String> {
    let mut devices = audio::list_devices().map_err(|e| e.to_string())?;
    if platform::detect().native_system_audio {
        // Prepended, not appended: on macOS 13+ this needs no setup and
        // should be the default pick ahead of any loopback device.
        devices.insert(
            0,
            audio::AudioDevice {
                name: audio::NATIVE_SYSTEM_AUDIO_DEVICE_ID.to_string(),
                is_loopback: false,
                is_default: false,
                is_native_system_audio: true,
            },
        );
    }
    Ok(devices)
}

#[tauri::command]
async fn get_platform_capabilities() -> platform::PlatformCapabilities {
    platform::detect()
}

#[tauri::command]
async fn get_desktop_settings(state: State<'_, AppState>) -> Result<DesktopSettings, String> {
    let saved = state.preferences.lock().unwrap();
    Ok(DesktopSettings::from(&*saved))
}

// A Tauri command's parameters are exactly its JS-facing API: each one is a
// field the frontend's invoke() call names directly, so bundling them into a
// struct would only relocate the count into the frontend's call site too.
#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn save_desktop_settings(
    state: State<'_, AppState>,
    groq_api_keys: Vec<String>,
    role_title: String,
    company_name: String,
    resume_text: String,
    job_description: String,
    language: String,
    chat_model: String,
    chat_provider: String,
    chat_api_keys: Vec<String>,
) -> Result<DesktopSettings, String> {
    let path = state
        .preferences_path
        .lock()
        .unwrap()
        .clone()
        .ok_or("The preferences path is not available.")?;
    let mut saved = state.preferences.lock().unwrap().clone();
    saved.groq_api_keys = normalize_api_keys(groq_api_keys);
    saved.groq_api_key.clear();
    saved.role_title = role_title.trim().to_string();
    saved.company_name = company_name.trim().to_string();
    saved.resume_text = resume_text.trim().to_string();
    saved.job_description = job_description.trim().to_string();
    saved.language = language.trim().to_string();
    saved.chat_model = chat_model.trim().to_string();
    saved.chat_provider = session::ChatProvider::parse(&chat_provider)
        .as_str()
        .to_string();
    saved.chat_api_keys = normalize_api_keys(chat_api_keys);
    preferences::save(&path, &saved).map_err(|error| error.to_string())?;
    *state.preferences.lock().unwrap() = saved.clone();
    Ok(DesktopSettings::from(&saved))
}

#[tauri::command]
async fn test_groq_connection(
    state: State<'_, AppState>,
) -> Result<session::ApiTestResult, String> {
    let keys = state.preferences.lock().unwrap().groq_api_keys.clone();
    session::test_api_keys(&keys)
        .await
        .map_err(|error| error.to_string())
}

/// Tests whatever is currently typed in the setup screen, not what was last
/// saved — mirrors `test_groq_connection`'s own behavior, and lets a key get
/// verified before the user commits to starting a live session with it.
#[tauri::command]
async fn test_chat_provider(
    provider: String,
    api_keys: Vec<String>,
) -> Result<session::ApiTestResult, String> {
    session::test_provider_keys(session::ChatProvider::parse(&provider), &api_keys)
        .await
        .map_err(|error| error.to_string())
}

/// `contents` is base64, not a raw byte array: a JS number-array of file
/// bytes bloats to 3-4x its size once JSON-encoded for IPC and gave no
/// progress feedback while it serialized, which for a multi-MB PDF looked
/// exactly like the import had silently hung. Base64 is ~1.33x, and the
/// browser's own base64 encoder (`FileReader.readAsDataURL`) does the work
/// off the array-spread path entirely.
#[tauri::command]
async fn extract_document_text(file_name: String, contents: String) -> Result<String, String> {
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(contents.as_bytes())
        .map_err(|_| "The document could not be read.".to_string())?;
    if bytes.is_empty() {
        return Err("The selected document is empty.".to_string());
    }
    if bytes.len() > 10 * 1024 * 1024 {
        return Err("Documents must be 10 MB or smaller.".to_string());
    }
    let extension = std::path::Path::new(&file_name)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let byte_count = bytes.len();
    let text = match extension.as_str() {
        "pdf" => pdf_extract::extract_text_from_mem(&bytes).map_err(|error| {
            format!("Could not read PDF text. Scanned-image PDFs need OCR, which this app does not do: {error}")
        })?,
        "txt" | "md" => String::from_utf8(bytes)
            .map_err(|_| "The document is not valid UTF-8 text.".to_string())?,
        _ => return Err("Use a PDF, TXT, or Markdown document.".to_string()),
    };
    let text = text.trim().to_string();
    if text.is_empty() {
        return Err("No readable text was found in the document. If this PDF is a scanned image rather than real text, paste the content in manually instead — this app has no OCR.".to_string());
    }
    // A real multi-page resume/JD compresses to far more than this from any
    // text-based PDF; this little text from a file this size means
    // extraction likely half-succeeded (a font pdf-extract could not map
    // correctly) rather than that the document is genuinely this short.
    if byte_count > 50_000 && text.len() < 40 {
        return Err(format!(
            "Only {} characters came out of a {} KB PDF — extraction likely failed silently rather than the document being this short. Paste the text in manually instead.",
            text.len(),
            byte_count / 1024
        ));
    }
    Ok(text)
}

/// Begin a local desktop session. Audio goes directly to Groq; no web account,
/// workspace, database record, or backend socket is involved.
#[tauri::command]
async fn start_listening(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    device: String,
) -> Result<StartResult, String> {
    let log_path = state.debug_log_path.lock().unwrap().clone();
    if let Some(path) = &log_path {
        debuglog::log(path, &format!("start_listening: device = {device:?}"));
    }
    if state.capture.lock().unwrap().is_some() {
        return Err("A listening session is already active.".to_string());
    }
    let saved = state.preferences.lock().unwrap().clone();
    if saved.groq_api_keys.is_empty() {
        return Err("Add at least one Groq API key before starting.".to_string());
    }
    let settings = session::Settings {
        api_keys: saved.groq_api_keys,
        role_title: saved.role_title,
        company_name: saved.company_name,
        resume_text: saved.resume_text,
        job_description: saved.job_description,
        language: saved.language,
        chat_model: saved.chat_model,
        chat_provider: session::ChatProvider::parse(&saved.chat_provider),
        chat_api_keys: saved.chat_api_keys,
    };

    let (raw_tx, raw_rx) = std::sync::mpsc::channel::<audio::CaptureMessage>();
    let capture = if device == audio::NATIVE_SYSTEM_AUDIO_DEVICE_ID {
        #[cfg(all(target_os = "macos", feature = "native-system-audio"))]
        {
            match native_audio::start(raw_tx) {
                Ok(handle) => CaptureHandle::Native(handle),
                Err(err) => {
                    if let Some(path) = &log_path {
                        debuglog::log(path, &format!("native_audio::start failed: {err}"));
                    }
                    return Err(err.to_string());
                }
            }
        }
        #[cfg(not(all(target_os = "macos", feature = "native-system-audio")))]
        {
            return Err("Native system audio capture is only available on macOS 13+.".to_string());
        }
    } else {
        match audio::start(&device, raw_tx) {
            Ok(handle) => {
                if let Some(path) = &log_path {
                    debuglog::log(path, "audio::start: stream opened successfully");
                }
                CaptureHandle::Loopback(handle)
            }
            Err(err) => {
                if let Some(path) = &log_path {
                    debuglog::log(path, &format!("audio::start failed: {err}"));
                }
                return Err(err.to_string());
            }
        }
    };

    let (audio_tx, audio_rx) = mpsc::channel::<audio::CaptureMessage>(64);
    let bridge_log_path = log_path.clone();
    std::thread::spawn(move || {
        // Bridge the realtime thread to async without blocking either.
        // The first-message and periodic logs exist to answer one question
        // when capture silently produces nothing downstream: did the
        // realtime callback ever fire at all, or did it fire and something
        // past this point drop it.
        let mut received: u64 = 0;
        while let Ok(message) = raw_rx.recv() {
            received += 1;
            if let Some(path) = &bridge_log_path {
                if received == 1 {
                    debuglog::log(
                        path,
                        "bridge: first audio message received from capture thread",
                    );
                } else if received % 500 == 0 {
                    debuglog::log(path, &format!("bridge: {received} messages relayed so far"));
                }
            }
            if audio_tx.blocking_send(message).is_err() {
                if let Some(path) = &bridge_log_path {
                    debuglog::log(path, "bridge: session receiver dropped, stopping");
                }
                break;
            }
        }
        if received == 0 {
            if let Some(path) = &bridge_log_path {
                debuglog::log(
                    path,
                    "bridge: capture thread ended with ZERO messages ever received",
                );
            }
        }
    });

    let (stop_tx, stop_rx) = mpsc::channel::<()>(1);
    let session_id = format!(
        "local-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_millis())
            .unwrap_or_default()
    );
    let handle = app.clone();
    let session_log_path = log_path.clone();
    tokio::spawn(async move {
        if let Err(err) = session::run_session(
            handle.clone(),
            settings,
            audio_rx,
            stop_rx,
            session_log_path.clone(),
        )
        .await
        {
            eprintln!("session ended: {err}");
            if let Some(path) = &session_log_path {
                debuglog::log(path, &format!("run_session ended with error: {err}"));
            }
            let _ = handle.emit(
                "verity://event",
                session::ServerEvent {
                    kind: "warning".to_string(),
                    payload: serde_json::json!({ "message": err.to_string() }),
                },
            );
        }
        let state = handle.state::<AppState>();
        *state.capture.lock().unwrap() = None;
        *state.stop.lock().unwrap() = None;
        let _ = handle.emit("verity://closed", ());
    });

    *state.stop.lock().unwrap() = Some(stop_tx);
    *state.capture.lock().unwrap() = Some(capture);

    Ok(StartResult { session_id })
}

#[tauri::command]
async fn stop_listening(state: State<'_, AppState>) -> Result<(), String> {
    // Dropping the capture releases the microphone, which is what turns off the
    // recording indicator the OS shows. Leaving it open after a session would
    // be indistinguishable from spyware.
    *state.capture.lock().unwrap() = None;
    let sender = state.stop.lock().unwrap().take();
    if let Some(sender) = sender {
        let _ = sender.send(()).await;
    }
    Ok(())
}

/// Pin the window above full-screen video calls.
#[tauri::command]
async fn set_always_on_top(app: tauri::AppHandle, pinned: bool) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window
            .set_always_on_top(pinned)
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[derive(Serialize)]
struct CaptureProtectionSetting {
    enabled: bool,
}

#[tauri::command]
async fn get_capture_protection(
    state: State<'_, AppState>,
) -> Result<CaptureProtectionSetting, String> {
    Ok(CaptureProtectionSetting {
        enabled: state
            .preferences
            .lock()
            .unwrap()
            .protect_hud_from_screen_capture,
    })
}

/// Apply immediately to the active HUD window, then persist for the next run.
#[tauri::command]
async fn set_capture_protection(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    enabled: bool,
) -> Result<CaptureProtectionSetting, String> {
    let window = app
        .get_webview_window("main")
        .ok_or("The HUD window is not available.")?;
    window
        .set_content_protected(enabled)
        .map_err(|error| format!("Could not apply capture protection: {error}"))?;

    let path = state
        .preferences_path
        .lock()
        .unwrap()
        .clone()
        .ok_or("The preferences path is not available.")?;
    let mut value = state.preferences.lock().unwrap().clone();
    value.protect_hud_from_screen_capture = enabled;
    *state.preferences.lock().unwrap() = value.clone();
    preferences::save(&path, &value)
        .map_err(|error| format!("Capture protection applied but could not be saved: {error}"))?;

    Ok(CaptureProtectionSetting { enabled })
}

/// Runs once per install (gated by `Preferences::permissions_primed`): opens
/// and quickly releases a microphone stream to trigger the macOS
/// microphone permission prompt, and, on macOS 13+, makes one
/// ScreenCaptureKit call to trigger the Screen Recording prompt. Doing this
/// at first launch means both dialogs appear together before the user has
/// even added an API key, instead of surprising them mid-interview.
///
/// Must not block window creation, so this runs as its own background task
/// rather than inline in `.setup()`.
fn prime_permissions(app: tauri::AppHandle, preferences_path: PathBuf) {
    tauri::async_runtime::spawn(async move {
        let (discard_tx, _discard_rx) = std::sync::mpsc::channel::<audio::CaptureMessage>();
        if let Ok(capture) = audio::start("", discard_tx) {
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            drop(capture);
        }

        #[cfg(all(target_os = "macos", feature = "native-system-audio"))]
        if platform::detect().native_system_audio {
            let _ = native_audio::prime_permission();
        }

        let mut saved = preferences::load(&preferences_path);
        saved.permissions_primed = true;
        if preferences::save(&preferences_path, &saved).is_ok() {
            if let Some(state) = app.try_state::<AppState>() {
                *state.preferences.lock().unwrap() = saved;
            }
        }
    });
}

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .setup(|app| {
            #[cfg(target_os = "macos")]
            {
                app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                app.set_dock_visibility(false);
            }

            let tray_menu = MenuBuilder::new(app)
                .text("show", "Show Verity")
                .text("hide", "Hide Verity")
                .separator()
                .text("quit", "Quit Verity")
                .build()?;
            let mut tray = TrayIconBuilder::with_id("verity")
                .menu(&tray_menu)
                .tooltip("Verity Interview Assistant")
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "hide" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.hide();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                });
            if let Some(icon) = app.default_window_icon().cloned() {
                tray = tray.icon(icon);
            }
            tray.build(app)?;

            let config_dir = app.path().app_config_dir()?;
            let preferences_path = preferences::path(&config_dir);
            let debug_log_path = debuglog::path(&config_dir);
            debuglog::log(&debug_log_path, "--- app launched ---");
            let mut saved = preferences::load(&preferences_path);
            if saved.groq_api_keys.is_empty() && !saved.groq_api_key.is_empty() {
                saved.groq_api_keys.push(saved.groq_api_key.clone());
                saved.groq_api_key.clear();
            }
            if saved.groq_api_keys.is_empty() {
                let environment_key = std::env::var("VERITY_GROQ_API_KEY")
                    .or_else(|_| std::env::var("GROQ_API_KEY"))
                    .unwrap_or_default();
                if !environment_key.trim().is_empty() {
                    saved.groq_api_keys.push(environment_key.trim().to_string());
                }
            }
            if !saved.permissions_primed {
                prime_permissions(app.handle().clone(), preferences_path.clone());
            }

            let state = app.state::<AppState>();
            *state.preferences_path.lock().unwrap() = Some(preferences_path);
            *state.preferences.lock().unwrap() = saved.clone();
            *state.debug_log_path.lock().unwrap() = Some(debug_log_path);

            if let Some(window) = app.get_webview_window("main") {
                window.set_always_on_top(true)?;
                // Keep the live HUD on the active Space, including when a call
                // window moves to its own full-screen Space.
                window.set_visible_on_all_workspaces(true)?;
                window.set_content_protected(saved.protect_hud_from_screen_capture)?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            list_audio_devices,
            get_platform_capabilities,
            get_desktop_settings,
            save_desktop_settings,
            test_groq_connection,
            test_chat_provider,
            extract_document_text,
            start_listening,
            stop_listening,
            set_always_on_top,
            get_capture_protection,
            set_capture_protection
        ])
        .run(tauri::generate_context!())
        .expect("failed to start Verity");
}

#[cfg(test)]
mod tests {
    use super::{extract_document_text, normalize_api_keys};
    use base64::Engine;

    fn to_base64(bytes: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(bytes)
    }

    #[test]
    fn api_keys_are_trimmed_deduplicated_and_empty_values_removed() {
        assert_eq!(
            normalize_api_keys(vec![
                " gsk_one ".to_string(),
                "".to_string(),
                "gsk_one".to_string(),
                "gsk_two".to_string(),
            ]),
            vec!["gsk_one".to_string(), "gsk_two".to_string()]
        );
    }

    /// Guards the bug that made the app look like it had no working audio:
    /// Tauri v2 grants nothing without a capability file, and the frontend's
    /// `listen()` is an ACL-gated core-plugin call. Losing this permission
    /// drops every backend event before the UI sees it, and because the
    /// rejected promise is invisible in a release webview, nothing reports
    /// the failure. A missing capability must break the build, not the
    /// interview.
    #[test]
    fn the_ui_is_granted_permission_to_receive_backend_events() {
        let generated =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("gen/schemas/capabilities.json");
        let contents = std::fs::read_to_string(&generated).unwrap_or_else(|error| {
            panic!("could not read {}: {error}", generated.display());
        });

        assert!(
            contents.contains("core:default") || contents.contains("core:event"),
            "no capability grants the event permissions `listen` needs, so the \
             HUD would receive nothing. Generated ACL was: {contents}"
        );
    }

    #[tokio::test]
    async fn plain_text_document_round_trips_through_base64() {
        let contents = to_base64(b"Line one.\nLine two.");
        let text = extract_document_text("resume.txt".to_string(), contents)
            .await
            .unwrap();
        assert_eq!(text, "Line one.\nLine two.");
    }

    #[tokio::test]
    async fn a_real_pdf_extracts_its_actual_text() {
        // Built with cupsfilter from plain text, the simplest real PDF this
        // machine can produce — proof the whole base64 -> decode -> pdf-extract
        // path genuinely runs, not just that it compiles.
        let pdf = std::process::Command::new("cupsfilter")
            .args(["-i", "text/plain"])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(b"VERITY PDF EXTRACTION TEST LINE")?;
                child.wait_with_output()
            });
        let Ok(output) = pdf else {
            eprintln!("cupsfilter unavailable on this machine; skipping");
            return;
        };
        if !output.status.success() || output.stdout.is_empty() {
            eprintln!("cupsfilter did not produce a PDF; skipping");
            return;
        }
        let text = extract_document_text("resume.pdf".to_string(), to_base64(&output.stdout))
            .await
            .unwrap();
        assert!(
            text.contains("VERITY PDF EXTRACTION TEST LINE"),
            "expected the real text back out, got: {text:?}"
        );
    }

    #[tokio::test]
    async fn empty_document_is_a_clear_error_not_a_panic() {
        let error = extract_document_text("resume.pdf".to_string(), String::new())
            .await
            .unwrap_err();
        assert!(error.contains("empty"));
    }

    #[tokio::test]
    async fn invalid_base64_is_a_clear_error_not_a_panic() {
        let error = extract_document_text("resume.pdf".to_string(), "not base64!!".to_string())
            .await
            .unwrap_err();
        assert!(!error.is_empty());
    }

    #[tokio::test]
    async fn unsupported_extension_is_rejected_before_any_parsing() {
        let error = extract_document_text("resume.docx".to_string(), to_base64(b"anything"))
            .await
            .unwrap_err();
        assert!(error.contains("PDF, TXT, or Markdown"));
    }
}
