//! Standalone Groq interview pipeline.
//!
//! This module has no web-auth, user, workspace, database, or backend
//! dependency. Audio is segmented locally, transcribed by Groq Whisper, and
//! answered through Groq's streaming chat endpoint.

use std::time::Instant;

use anyhow::{anyhow, Result};
use futures_util::StreamExt;
use reqwest::multipart;
use serde::Serialize;
use serde_json::json;
use tauri::Emitter;
use tokio::sync::mpsc;

const SILENCE_FLUSH_MS: u64 = 360;
const MIN_VOICE_MS: u64 = 300;
/// A hard cap, not a target: segmentation normally flushes on
/// `SILENCE_FLUSH_MS` of quiet, so this only fires when voice keeps crossing
/// the threshold continuously with no gap that long. There is no speaker
/// separation here — if the captured stream itself contains more than one
/// voice with no silence between them (cross-talk, or the candidate's own
/// mic bleeding into what should be call-only audio, e.g. Windows' "Listen
/// to this device" mic monitoring), everything voiced in that stretch
/// becomes one utterance regardless of who is actually speaking. Was 10s;
/// lower bounds how much of a mixed stream can blend into a single
/// transcript. Real interview questions are almost never a continuous 7s of
/// speech with no pause, so this should rarely clip a legitimate one.
const MAX_UTTERANCE_MS: u64 = 7_000;
/// Fallback only, used until calibration below produces a real number:
/// captured loopback level varies hugely by OS and sound driver (a Realtek
/// Windows loopback measured an order of magnitude quieter here than the
/// value this was tuned against), so a fixed threshold is wrong on some
/// machine no matter what it is set to.
const FALLBACK_VOICE_RMS_THRESHOLD: f32 = 0.012;
/// How long a stretch of pure silence (nothing crossing the voice threshold)
/// can run before the HUD is told, instead of continuing to claim "Audio is
/// active" while nothing is actually reaching the pipeline — e.g. the wrong
/// device is selected, or interview audio isn't routed to it.
const NO_VOICE_ALERT_MS: u64 = 12_000;
/// How often the live level meter updates. Frequent enough to feel
/// real-time, far below IPC-flooding territory.
const LEVEL_EMIT_MS: u64 = 150;
/// How long to sample the incoming stream before committing to a voice
/// threshold. Long enough to see past one lucky quiet or loud chunk, short
/// enough that a real interview question inside this window is still caught
/// by the fallback threshold rather than missed outright.
const CALIBRATION_MS: u64 = 1_500;
/// The calibrated threshold is a multiple of the measured noise floor, not
/// the floor itself — otherwise room hiss alone would count as speech. 4x is
/// comfortably above measurement jitter while still well under normal speech,
/// which runs 10-50x the floor on a quiet mic.
const THRESHOLD_ABOVE_FLOOR: f32 = 4.0;
/// Bounds on the calibrated threshold so a pathological calibration window —
/// dead silence, or someone talking through the whole first second — cannot
/// produce a threshold that is unreachable or that fires on room noise.
const MIN_VOICE_RMS_THRESHOLD: f32 = 0.003;
const MAX_VOICE_RMS_THRESHOLD: f32 = 0.05;
/// The HUD meter's "full scale" also has to move with device loudness, or a
/// quiet device correctly detecting speech would still show a bar stuck near
/// zero. Set as a multiple of the calibrated voice threshold so normal speech
/// reads as a mid-to-high bar with headroom before clipping.
const LEVEL_REFERENCE_ABOVE_THRESHOLD: f32 = 6.0;
const STT_MODEL: &str = "whisper-large-v3-turbo";
// allam-2-7b was the default before this: consistently fast (~120-140ms
// TTFT measured earlier), but verified unreliable at the two things the
// prompt above asks for — switching to bullet points for a definitional
// question, and not leaking a literal "You:" label, both confirmed live
// against the real API. openai/gpt-oss-20b, with the reasoning_effort
// params is_reasoning_model already sends, got both right consistently
// across repeated live tests (~1s wall time each) and carries a *higher*
// per-key token-per-minute budget on Groq (8,000 vs. allam's 6,000), so
// this is not a quota regression. A saved chat_model preference always
// wins over this default regardless.
const DEFAULT_CHAT_MODEL: &str = "openai/gpt-oss-20b";
/// Recent Q&A pairs kept so a follow-up like "what was the hardest part?"
/// still has an antecedent, without unbounded prompt growth.
///
/// Was 6. Measured live against Groq: with the resume and job description
/// each sent in full on every single request (see CONTEXT_FIELD_MAX_CHARS),
/// a worst-case prompt cost 2,418 tokens against this key's 6,000
/// tokens-per-minute cap — enough to exhaust it in 2-3 questions, exactly the
/// failure reported. Cutting history to 3 turns and the two context fields to
/// 1,500 characters each measured 800 tokens for the same worst case, real
/// headroom for roughly 7 requests/minute instead of 2.
const MAX_HISTORY_TURNS: usize = 3;
/// See MAX_HISTORY_TURNS above for the measurement this was cut from (6,000
/// characters) to.
const CONTEXT_FIELD_MAX_CHARS: usize = 1_500;
/// Was 160 (roughly the old 90-word narrative cap). A bullet-point answer
/// needs headroom past that: a short lead-in sentence plus 3-5 points each
/// carries its own marker/newline overhead on top of the words themselves,
/// and 160 was tight enough to risk the model's last point being truncated
/// mid-sentence. This adds ~60 output tokens per request — negligible next
/// to the ~800-token full request measured when the context fields above
/// were cut down, so it doesn't meaningfully undo that budget fix.
const MAX_ANSWER_TOKENS: u32 = 220;

#[derive(Debug, Clone)]
pub struct Settings {
    pub api_keys: Vec<String>,
    pub role_title: String,
    pub company_name: String,
    pub resume_text: String,
    pub job_description: String,
    pub language: String,
    pub chat_model: String,
    pub chat_provider: ChatProvider,
    pub chat_api_keys: Vec<String>,
}

impl Settings {
    /// The keys that actually answer the question: Groq reuses the
    /// transcription keys already entered, since duplicating them into a
    /// second field would only ask the same thing twice for the one
    /// provider where the answer is always the same list.
    fn effective_chat_keys(&self) -> &[String] {
        if self.chat_provider == ChatProvider::Groq {
            &self.api_keys
        } else {
            &self.chat_api_keys
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ServerEvent {
    pub kind: String,
    pub payload: serde_json::Value,
}

#[derive(Debug, Clone, Serialize)]
pub struct ApiTestResult {
    pub working_key: usize,
    pub total_keys: usize,
    pub latency_ms: u64,
}

pub async fn test_api_keys(api_keys: &[String]) -> Result<ApiTestResult> {
    if api_keys.is_empty() {
        return Err(anyhow!("Add at least one Groq API key."));
    }
    let client = reqwest::Client::builder().tcp_nodelay(true).build()?;
    let started = Instant::now();
    let mut last_error = "No Groq API key could connect.".to_string();
    for (index, api_key) in api_keys.iter().enumerate() {
        match client
            .get("https://api.groq.com/openai/v1/models")
            .bearer_auth(api_key)
            .timeout(std::time::Duration::from_secs(8))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                return Ok(ApiTestResult {
                    working_key: index + 1,
                    total_keys: api_keys.len(),
                    latency_ms: started.elapsed().as_millis() as u64,
                });
            }
            Ok(response) => {
                let status = response.status();
                let detail = response.text().await.unwrap_or_default();
                last_error = format!(
                    "Groq key {} failed ({status}): {}",
                    index + 1,
                    concise_error(&detail)
                );
            }
            Err(error) => last_error = format!("Groq key {} network error: {error}", index + 1),
        }
    }
    Err(anyhow!(last_error))
}

/// Which service generates the spoken answer. Transcription always stays on
/// Groq Whisper regardless of this choice: Anthropic has no audio
/// transcription endpoint at all, Gemini's audio input is a different
/// request shape than a Whisper-style transcribe call, and Bedrock has no
/// STT-equivalent endpoint reachable with just an API key either — so only
/// answer generation is genuinely interchangeable across all five.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChatProvider {
    Groq,
    OpenAi,
    Anthropic,
    Gemini,
    Bedrock,
}

/// Bedrock API keys are auth only, not a region selector — the region is
/// baked into the endpoint URL, not the key. Hardcoded rather than a new
/// settings field, to keep Bedrock "paste a key, pick a provider" like every
/// other provider instead of the one exception with an extra required field.
/// A key generated for a different region would need this changed to match.
const BEDROCK_REGION: &str = "us-east-1";

impl ChatProvider {
    pub fn parse(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "openai" => Self::OpenAi,
            "anthropic" | "claude" => Self::Anthropic,
            "gemini" | "google" => Self::Gemini,
            "bedrock" | "amazon" | "amazon-bedrock" | "aws" => Self::Bedrock,
            _ => Self::Groq,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Groq => "groq",
            Self::OpenAi => "openai",
            Self::Anthropic => "anthropic",
            Self::Gemini => "gemini",
            Self::Bedrock => "bedrock",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Groq => "Groq",
            Self::OpenAi => "OpenAI",
            Self::Anthropic => "Anthropic",
            Self::Gemini => "Gemini",
            Self::Bedrock => "Amazon Bedrock",
        }
    }

    fn default_model(self) -> &'static str {
        match self {
            Self::Groq => DEFAULT_CHAT_MODEL,
            Self::OpenAi => "gpt-4o-mini",
            // Current per the model roster this build shipped against, not
            // the older 3.5 Haiku line - keep in sync if that roster moves.
            Self::Anthropic => "claude-haiku-4-5-20251001",
            Self::Gemini => "gemini-2.0-flash",
            // Unverified against a real successful generation: every model
            // tried on the account this was built against returned
            // "Operation not allowed" (400), not an auth failure — Bedrock
            // requires enabling access per model family in the AWS console
            // before ANY model can be invoked, regardless of which one is
            // named here. Amazon's own Nova line needs no separate
            // third-party EULA acceptance, making it the least-friction
            // default once access is granted.
            Self::Bedrock => "amazon.nova-lite-v1:0",
        }
    }

    /// A cheap, unauthenticated-safe endpoint used only to prove the key and
    /// network path work, with no generation cost.
    fn models_request(self, client: &reqwest::Client, api_key: &str) -> reqwest::RequestBuilder {
        match self {
            Self::Groq => client
                .get("https://api.groq.com/openai/v1/models")
                .bearer_auth(api_key),
            Self::OpenAi => client
                .get("https://api.openai.com/v1/models")
                .bearer_auth(api_key),
            Self::Anthropic => client
                .get("https://api.anthropic.com/v1/models")
                .header("x-api-key", api_key)
                .header("anthropic-version", "2023-06-01"),
            Self::Gemini => client
                .get("https://generativelanguage.googleapis.com/v1beta/models")
                .query(&[("key", api_key)]),
            // Verified live: returns 200 with the account's real foundation
            // model list on a bearer token that has no model-invoke access
            // granted at all yet, so this proves auth independent of
            // whether any model is actually usable.
            Self::Bedrock => client
                .get(format!(
                    "https://bedrock.{BEDROCK_REGION}.amazonaws.com/foundation-models"
                ))
                .bearer_auth(api_key),
        }
    }
}

pub async fn test_provider_keys(
    provider: ChatProvider,
    api_keys: &[String],
) -> Result<ApiTestResult> {
    if provider == ChatProvider::Groq {
        return test_api_keys(api_keys).await;
    }
    if api_keys.is_empty() {
        return Err(anyhow!("Add at least one {} API key.", provider.label()));
    }
    let client = reqwest::Client::builder().tcp_nodelay(true).build()?;
    let started = Instant::now();
    let mut last_error = format!("No {} API key could connect.", provider.label());
    for (index, api_key) in api_keys.iter().enumerate() {
        match provider
            .models_request(&client, api_key)
            .timeout(std::time::Duration::from_secs(8))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                return Ok(ApiTestResult {
                    working_key: index + 1,
                    total_keys: api_keys.len(),
                    latency_ms: started.elapsed().as_millis() as u64,
                });
            }
            Ok(response) => {
                let status = response.status();
                let detail = response.text().await.unwrap_or_default();
                last_error = format!(
                    "{} key {} failed ({status}): {}",
                    provider.label(),
                    index + 1,
                    concise_error(&detail)
                );
            }
            Err(error) => {
                last_error = format!(
                    "{} key {} network error: {error}",
                    provider.label(),
                    index + 1
                )
            }
        }
    }
    Err(anyhow!(last_error))
}

struct Utterance {
    pcm: Vec<u8>,
    queued_at: Instant,
    detection_delay_ms: u64,
}

fn emit(app: &tauri::AppHandle, kind: &str, payload: serde_json::Value) {
    let _ = app.emit(
        "verity://event",
        ServerEvent {
            kind: kind.to_string(),
            payload,
        },
    );
}

/// Segment captured PCM into clauses without delaying the realtime callback.
pub async fn run_session(
    app: tauri::AppHandle,
    settings: Settings,
    mut audio: mpsc::Receiver<super::audio::CaptureMessage>,
    mut stop: mpsc::Receiver<()>,
    log_path: Option<std::path::PathBuf>,
) -> Result<()> {
    let client = reqwest::Client::builder()
        .tcp_nodelay(true)
        .pool_max_idle_per_host(4)
        .build()?;
    let (utterance_tx, mut utterance_rx) = mpsc::channel::<Utterance>(4);
    let processor_app = app.clone();
    let processor = tokio::spawn(async move {
        let mut history: Vec<(String, String)> = Vec::new();
        while let Some(utterance) = utterance_rx.recv().await {
            match process_utterance(&processor_app, &client, &settings, &history, utterance).await {
                Ok(Some((question, answer))) => {
                    history.push((question, answer));
                    if history.len() > MAX_HISTORY_TURNS {
                        history.remove(0);
                    }
                }
                Ok(None) => {}
                Err(error) => emit(
                    &processor_app,
                    "warning",
                    json!({ "message": error.to_string() }),
                ),
            }
        }
    });

    emit(&app, "session.ready", json!({ "mode": "standalone" }));
    let mut buffer = Vec::new();
    let mut held_ms = 0_u64;
    let mut voiced_ms = 0_u64;
    let mut quiet_ms = 0_u64;
    let mut explicitly_stopped = false;
    let mut device_error = None;
    let mut silence_since_voice_ms = 0_u64;
    let mut elapsed_ms = 0_u64;
    // Calibration replaces a fixed voice threshold with one measured against
    // this device: loopback level varies by an order of magnitude across OS
    // and sound driver, so no single constant is right on every machine. The
    // fallback threshold covers the calibration window itself so an early
    // question is still caught rather than missed outright.
    let mut calibration_samples: Vec<f32> = Vec::new();
    let mut calibration_ms_elapsed = 0_u64;
    let mut calibrated = false;
    let mut voice_threshold = FALLBACK_VOICE_RMS_THRESHOLD;
    let mut level_reference = FALLBACK_VOICE_RMS_THRESHOLD * LEVEL_REFERENCE_ABOVE_THRESHOLD;
    // Coarser than the HUD meter's own throttle: the debug log exists to be
    // read after the fact, so it summarizes a whole window instead of
    // recording every emit. The number that actually answers "is real audio
    // arriving" is the peak, since RMS on a mixed chunk can look quiet even
    // while speech is present elsewhere in the window.
    let mut log_window_ms = 0_u64;
    let mut log_window_peak_rms = 0_f32;
    const LOG_WINDOW_MS: u64 = 2_000;

    loop {
        tokio::select! {
            Some(message) = audio.recv() => {
                let pcm = match message {
                    super::audio::CaptureMessage::Pcm(pcm) => pcm,
                    super::audio::CaptureMessage::Error(message) => {
                        device_error = Some(message);
                        break;
                    }
                };
                let duration_ms = pcm_duration_ms(&pcm);
                let rms = frame_rms(&pcm);

                if !calibrated {
                    calibration_samples.push(rms);
                    calibration_ms_elapsed += duration_ms;
                    if calibration_ms_elapsed >= CALIBRATION_MS {
                        voice_threshold = calibrate_threshold(&calibration_samples);
                        level_reference = voice_threshold * LEVEL_REFERENCE_ABOVE_THRESHOLD;
                        calibrated = true;
                        if let Some(path) = &log_path {
                            let sample_count = calibration_samples.len();
                            super::debuglog::log(
                                path,
                                &format!(
                                    "calibrated from {sample_count} samples: voice_threshold={voice_threshold:.4}, level_reference={level_reference:.4}"
                                ),
                            );
                        }
                        calibration_samples.clear();
                        calibration_samples.shrink_to_fit();
                    }
                }
                let voiced = rms >= voice_threshold;

                log_window_ms += duration_ms;
                log_window_peak_rms = log_window_peak_rms.max(rms);
                if log_window_ms >= LOG_WINDOW_MS {
                    if let Some(path) = &log_path {
                        super::debuglog::log(
                            path,
                            &format!(
                                "level: peak_rms={log_window_peak_rms:.4} (threshold={voice_threshold:.4}, calibrated={calibrated}) over last {log_window_ms}ms"
                            ),
                        );
                    }
                    log_window_ms = 0;
                    log_window_peak_rms = 0.0;
                }

                // Independent of utterance segmentation below: tell the HUD
                // the truth about whether anything voiced has reached the
                // pipeline recently, instead of leaving a static "Audio is
                // active" label up through an entire silent session.
                if voiced {
                    silence_since_voice_ms = 0;
                } else {
                    let before = silence_since_voice_ms;
                    silence_since_voice_ms += duration_ms;
                    if crosses_interval(before, silence_since_voice_ms, NO_VOICE_ALERT_MS) {
                        if let Some(path) = &log_path {
                            super::debuglog::log(
                                path,
                                &format!("audio.silence fired: {silence_since_voice_ms}ms with nothing crossing the voice threshold"),
                            );
                        }
                        emit(
                            &app,
                            "audio.silence",
                            json!({ "silence_ms": silence_since_voice_ms }),
                        );
                    }
                }

                // Live meter: throttled so the HUD can show real-time level
                // and voiced/silent state without flooding IPC on every
                // realtime-thread callback.
                let previous_elapsed = elapsed_ms;
                elapsed_ms += duration_ms;
                if crosses_interval(previous_elapsed, elapsed_ms, LEVEL_EMIT_MS) {
                    emit(
                        &app,
                        "audio.level",
                        json!({
                            "level": (rms / level_reference).min(1.0),
                            "voiced": voiced
                        }),
                    );
                }

                if voiced {
                    voiced_ms += duration_ms;
                    quiet_ms = 0;
                } else if voiced_ms > 0 {
                    quiet_ms += duration_ms;
                }
                held_ms += duration_ms;
                buffer.extend_from_slice(&pcm);

                let paused = quiet_ms >= SILENCE_FLUSH_MS && voiced_ms >= MIN_VOICE_MS;
                let ceiling = held_ms >= MAX_UTTERANCE_MS && voiced_ms >= MIN_VOICE_MS;
                if paused || ceiling {
                    let utterance = Utterance {
                        pcm: std::mem::take(&mut buffer),
                        queued_at: Instant::now(),
                        detection_delay_ms: if paused { quiet_ms } else { 0 },
                    };
                    if utterance_tx.send(utterance).await.is_err() {
                        break;
                    }
                    held_ms = 0;
                    voiced_ms = 0;
                    quiet_ms = 0;
                } else if held_ms >= MAX_UTTERANCE_MS && voiced_ms < MIN_VOICE_MS {
                    buffer.clear();
                    held_ms = 0;
                    voiced_ms = 0;
                    quiet_ms = 0;
                }
            }
            _ = stop.recv() => {
                explicitly_stopped = true;
                break;
            },
            else => break,
        }
    }

    if explicitly_stopped {
        drop(utterance_tx);
        processor.abort();
        let _ = processor.await;
        emit(&app, "session.ended", json!({}));
        return Ok(());
    }

    if let Some(message) = device_error {
        drop(utterance_tx);
        processor.abort();
        let _ = processor.await;
        emit(&app, "session.ended", json!({}));
        return Err(anyhow!("Audio device disconnected: {message}"));
    }

    if voiced_ms >= MIN_VOICE_MS && !buffer.is_empty() {
        let _ = utterance_tx
            .send(Utterance {
                pcm: buffer,
                queued_at: Instant::now(),
                detection_delay_ms: 0,
            })
            .await;
    }
    drop(utterance_tx);
    let _ = processor.await;
    emit(&app, "session.ended", json!({}));
    Ok(())
}

async fn process_utterance(
    app: &tauri::AppHandle,
    client: &reqwest::Client,
    settings: &Settings,
    history: &[(String, String)],
    utterance: Utterance,
) -> Result<Option<(String, String)>> {
    let request_started = Instant::now();
    emit(app, "stt.started", json!({}));
    let (transcript, key_index) = transcribe(client, settings, utterance.pcm).await?;
    let stt_ms = request_started.elapsed().as_millis() as u64;
    if transcript.trim().is_empty() {
        return Ok(None);
    }
    emit(
        app,
        "stt.final",
        json!({ "content": transcript, "latency_ms": stt_ms }),
    );

    if !looks_like_question(&transcript) {
        emit(app, "speech.ignored", json!({ "content": transcript }));
        return Ok(None);
    }

    emit(
        app,
        "question.finalized",
        json!({ "content": transcript, "should_generate": true, "stt_ms": stt_ms }),
    );
    let answer = stream_answer(
        app,
        client,
        settings,
        history,
        &transcript,
        utterance.queued_at,
        utterance.detection_delay_ms,
        stt_ms,
        key_index,
    )
    .await?;
    Ok(Some((transcript, answer)))
}

async fn transcribe(
    client: &reqwest::Client,
    settings: &Settings,
    pcm: Vec<u8>,
) -> Result<(String, usize)> {
    let wav = wav_bytes(&pcm);
    let mut last_error = "Groq transcription failed.".to_string();
    for (index, api_key) in settings.api_keys.iter().enumerate() {
        let part = multipart::Part::bytes(wav.clone())
            .file_name("interview.wav")
            .mime_str("audio/wav")?;
        let form = multipart::Form::new()
            .part("file", part)
            .text("model", STT_MODEL)
            .text("response_format", "json")
            .text("language", settings.language.clone())
            .text("temperature", "0");
        let response = match client
            .post("https://api.groq.com/openai/v1/audio/transcriptions")
            .bearer_auth(api_key)
            .multipart(form)
            .timeout(std::time::Duration::from_secs(12))
            .send()
            .await
        {
            Ok(response) => response,
            Err(error) => {
                last_error = format!("Groq key {} network error: {error}", index + 1);
                continue;
            }
        };
        if response.status().is_success() {
            let body: serde_json::Value = response.json().await?;
            let text = body
                .get("text")
                .and_then(|value| value.as_str())
                .unwrap_or_default()
                .trim()
                .to_string();
            return Ok((text, index));
        }
        let status = response.status();
        let detail = response.text().await.unwrap_or_default();
        last_error = format!(
            "Groq key {} transcription failed ({status}): {}",
            index + 1,
            concise_error(&detail)
        );
        if !should_rotate_key(status) {
            return Err(anyhow!(last_error));
        }
    }
    Err(anyhow!(last_error))
}

/// Whether `model` accepts Groq's `reasoning_effort`/`include_reasoning`
/// extensions. Every other Groq-hosted model 400s if they're present, so
/// this must stay conservative — checking a real prefix, not guessing from a
/// name that merely mentions "reasoning".
fn is_reasoning_model(model: &str) -> bool {
    model.starts_with("openai/gpt-oss")
}

/// Builds the one outbound HTTP request for `provider`. The prompt already
/// contains every instruction — role, resume, job description, question —
/// as a single block of text, so every provider accepts it unchanged as one
/// user message; only the transport (URL, auth, body shape, streaming
/// format) actually differs between them.
fn build_chat_request(
    client: &reqwest::Client,
    provider: ChatProvider,
    api_key: &str,
    model: &str,
    prompt: &str,
) -> reqwest::RequestBuilder {
    match provider {
        ChatProvider::Groq | ChatProvider::OpenAi => {
            let mut body = json!({
                "model": model,
                "messages": [{ "role": "user", "content": prompt }],
                "stream": true,
                "temperature": 0.3,
                "max_completion_tokens": MAX_ANSWER_TOKENS
            });
            // reasoning_effort/include_reasoning are Groq extensions only
            // the gpt-oss family accepts — every other model, including
            // OpenAI's, 400s outright if they're present.
            if provider == ChatProvider::Groq && is_reasoning_model(model) {
                let object = body.as_object_mut().expect("object literal");
                object.insert("reasoning_effort".to_string(), json!("low"));
                object.insert("include_reasoning".to_string(), json!(false));
            }
            let url = match provider {
                ChatProvider::Groq => "https://api.groq.com/openai/v1/chat/completions",
                _ => "https://api.openai.com/v1/chat/completions",
            };
            client.post(url).bearer_auth(api_key).json(&body)
        }
        ChatProvider::Anthropic => {
            let body = json!({
                "model": model,
                "max_tokens": MAX_ANSWER_TOKENS,
                "temperature": 0.3,
                "stream": true,
                "messages": [{ "role": "user", "content": prompt }]
            });
            client
                .post("https://api.anthropic.com/v1/messages")
                .header("x-api-key", api_key)
                .header("anthropic-version", "2023-06-01")
                .json(&body)
        }
        ChatProvider::Gemini => {
            let body = json!({
                "contents": [{ "role": "user", "parts": [{ "text": prompt }] }],
                "generationConfig": { "temperature": 0.3, "maxOutputTokens": MAX_ANSWER_TOKENS }
            });
            let url = format!(
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
            );
            client
                .post(url)
                .query(&[("alt", "sse"), ("key", api_key)])
                .json(&body)
        }
        ChatProvider::Bedrock => {
            // The non-streaming Converse endpoint, deliberately, not
            // ConverseStream: streaming Bedrock responses are framed in
            // AWS's own binary vnd.amazon.eventstream format, not text SSE
            // like every other provider here, and there was no way to
            // verify that parser against a real successful response — every
            // model tried returned "Operation not allowed" (a per-model
            // access grant needed in the AWS console), never a success to
            // inspect. This body shape and field names (inferenceConfig ->
            // maxTokens/temperature) are verified against AWS's own
            // Converse API reference, not guessed.
            let body = json!({
                "messages": [{ "role": "user", "content": [{ "text": prompt }] }],
                "inferenceConfig": { "maxTokens": MAX_ANSWER_TOKENS, "temperature": 0.3 }
            });
            let url = format!(
                "https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com/model/{model}/converse"
            );
            client.post(url).bearer_auth(api_key).json(&body)
        }
    }
}

/// Pulls the incremental answer text out of one already-parsed SSE data
/// line, whose JSON shape is different for every provider. Returns `None`
/// for event types that carry no text (Anthropic's `message_start`,
/// `content_block_stop`, pings, and so on) so the caller's loop just moves
/// on to the next line instead of treating it as an empty answer chunk.
fn extract_delta_text(provider: ChatProvider, value: &serde_json::Value) -> Option<String> {
    match provider {
        ChatProvider::Groq | ChatProvider::OpenAi => value["choices"][0]["delta"]["content"]
            .as_str()
            .map(str::to_string),
        ChatProvider::Anthropic => {
            if value.get("type").and_then(|t| t.as_str()) != Some("content_block_delta") {
                return None;
            }
            value["delta"]["text"].as_str().map(str::to_string)
        }
        ChatProvider::Gemini => value["candidates"][0]["content"]["parts"][0]["text"]
            .as_str()
            .map(str::to_string),
        // Structurally unreachable: stream_answer special-cases Bedrock into
        // one non-streaming response before this SSE-line loop ever runs
        // (see build_chat_request). Still needs a real arm for the match to
        // be exhaustive.
        ChatProvider::Bedrock => None,
    }
}

// Each parameter is a distinct, already-borrowed piece of session state;
// bundling them into a struct would just move the same count into one more
// place without changing what the caller has to assemble.
#[allow(clippy::too_many_arguments)]
async fn stream_answer(
    app: &tauri::AppHandle,
    client: &reqwest::Client,
    settings: &Settings,
    history: &[(String, String)],
    question: &str,
    queued_at: Instant,
    detection_delay_ms: u64,
    stt_ms: u64,
    preferred_key_index: usize,
) -> Result<String> {
    let generation_started = Instant::now();
    let context = match (settings.role_title.trim(), settings.company_name.trim()) {
        ("", "") => "a job interview".to_string(),
        (role, "") => format!("an interview for {role}"),
        ("", company) => format!("an interview at {company}"),
        (role, company) => format!("an interview for {role} at {company}"),
    };
    let resume = truncate_context(&settings.resume_text, CONTEXT_FIELD_MAX_CHARS);
    let job_description = truncate_context(&settings.job_description, CONTEXT_FIELD_MAX_CHARS);
    let conversation = format_conversation(history);
    let prompt = format!(
        "You are a live interview answer coach. The candidate is in {context}. \
         Output ONLY the exact words the candidate should say aloud right now. Never comment on the \
         resume, never explain your reasoning, never start with phrases like \"While reviewing...\" or \
         \"I notice...\". Never write a speaker label like \"You:\" or \"Interviewer:\" — RECENT \
         CONVERSATION below is reference material, not a transcript to continue; write only the new \
         answer itself, nothing else. \
         Use only facts supported by the resume. Align to the job description without copying it. \
         If personal facts are unknown, still answer directly with a safe adaptable response — never \
         invent employers, dates, or metrics, and never tell the candidate that facts are missing. \
         If the question refers back to something earlier (e.g. \"the hardest part\", \"that project\"), \
         resolve it using the recent conversation below.\n\n\
         Pick ONE of these two formats based on what the question is actually asking:\n\n\
         FORMAT A — PERSONAL / BEHAVIORAL (about the candidate's own experience, a past situation, \
         motivation, strengths, weaknesses): a first-person narrative, 2-4 sentences, confident and \
         natural, under 70 words. No bullets. Example, if asked \"What's your biggest strength?\":\n\
         I stay calm under pressure and focus on root causes instead of quick patches. When a production \
         issue came up, I traced it back to a misconfigured cache instead of just restarting the service, \
         which stopped it recurring for good.\n\n\
         FORMAT B — DEFINITION / EXPLANATION / REASONING (\"what is X\", \"explain Y\", \"how does Z \
         work\", \"walk me through...\", \"what's the difference between...\"): at most one short lead-in \
         sentence, then 3-5 bullet points, each on its own line starting with exactly \"- \", each short \
         enough to read aloud in a few seconds. Example, if asked \"What is a CI/CD pipeline?\":\n\
         A CI/CD pipeline automates getting code from commit to production:\n\
         - Continuous Integration merges and tests changes frequently\n\
         - Continuous Delivery keeps every build in a deployable state\n\
         - Continuous Deployment ships each passing change automatically\n\n\
         Never open two answers in a row the same way. RECENT CONVERSATION below is what was already \
         said — if it already led with a specific company, project, or sentence structure, this answer \
         must open differently and, where the resume supports it, use a different example.\n\n\
         RESUME CONTEXT:\n{resume}\n\nJOB DESCRIPTION:\n{job_description}\n\nRECENT CONVERSATION:\n{conversation}\n\nINTERVIEWER QUESTION:\n{question}"
    );
    let provider = settings.chat_provider;
    let model = if settings.chat_model.trim().is_empty() {
        provider.default_model()
    } else {
        settings.chat_model.trim()
    };
    let keys = settings.effective_chat_keys();
    if keys.is_empty() {
        return Err(anyhow!(
            "Add at least one {} API key before starting.",
            provider.label()
        ));
    }
    let mut response = None;
    let mut last_error = format!("{} answer failed.", provider.label());
    for offset in 0..keys.len() {
        let index = (preferred_key_index + offset) % keys.len();
        let result = build_chat_request(client, provider, &keys[index], model, &prompt)
            .timeout(std::time::Duration::from_secs(12))
            .send()
            .await;
        let candidate = match result {
            Ok(candidate) => candidate,
            Err(error) => {
                last_error = format!(
                    "{} key {} network error: {error}",
                    provider.label(),
                    index + 1
                );
                continue;
            }
        };
        if candidate.status().is_success() {
            response = Some(candidate);
            break;
        }
        let status = candidate.status();
        let detail = candidate.text().await.unwrap_or_default();
        last_error = format!(
            "{} key {} answer failed ({status}): {}",
            provider.label(),
            index + 1,
            concise_error(&detail)
        );
        if !should_rotate_key(status) {
            return Err(anyhow!(last_error));
        }
    }
    let response = response.ok_or_else(|| anyhow!(last_error))?;

    let mut answer = String::new();
    let mut first_token_ms = None;

    if provider == ChatProvider::Bedrock {
        // Converse (not ConverseStream — see build_chat_request) returns the
        // whole answer in one JSON body, so it is emitted as a single
        // answer.delta rather than the incremental stream every other
        // provider produces. Same events, same shape the frontend already
        // handles; the only difference is there is exactly one of them.
        let value: serde_json::Value = response.json().await?;
        let text = value["output"]["message"]["content"][0]["text"]
            .as_str()
            .unwrap_or_default()
            .to_string();
        if !text.is_empty() {
            answer.push_str(&text);
            let first = *first_token_ms
                .get_or_insert_with(|| detection_delay_ms + queued_at.elapsed().as_millis() as u64);
            emit(
                app,
                "answer.delta",
                json!({
                    "delta": text,
                    "content": answer,
                    "first_response_ms": first,
                    "stt_ms": stt_ms
                }),
            );
        }
    } else {
        let mut stream = response.bytes_stream();
        let mut pending = String::new();
        while let Some(chunk) = stream.next().await {
            pending.push_str(&String::from_utf8_lossy(&chunk?));
            while let Some(newline) = pending.find('\n') {
                let line = pending[..newline].trim().to_string();
                pending.drain(..=newline);
                let Some(data) = line.strip_prefix("data: ") else {
                    continue;
                };
                // Only Groq/OpenAI send this literal sentinel; Anthropic and
                // Gemini simply close the connection when done, so this
                // check is a no-op rather than a special case for those two.
                if data == "[DONE]" {
                    break;
                }
                let Ok(value) = serde_json::from_str::<serde_json::Value>(data) else {
                    continue;
                };
                let Some(delta) = extract_delta_text(provider, &value) else {
                    continue;
                };
                if delta.is_empty() {
                    continue;
                }
                answer.push_str(&delta);
                let first = *first_token_ms.get_or_insert_with(|| {
                    detection_delay_ms + queued_at.elapsed().as_millis() as u64
                });
                emit(
                    app,
                    "answer.delta",
                    json!({
                        "delta": delta,
                        "content": answer,
                        "first_response_ms": first,
                        "stt_ms": stt_ms
                    }),
                );
            }
        }
    }
    let total_ms = detection_delay_ms + queued_at.elapsed().as_millis() as u64;
    let generation_ms = generation_started.elapsed().as_millis() as u64;
    let cleaned = strip_leaked_speaker_label(answer.trim());
    let (direction, key_points) = split_into_direction_and_points(cleaned);
    emit(
        app,
        "answer.complete",
        json!({
            "content": {
                "answer_direction": direction,
                "key_points": key_points,
                "structure": ""
            },
            "first_response_ms": first_token_ms,
            "stt_ms": stt_ms,
            "generation_ms": generation_ms,
            "detection_ms": detection_delay_ms,
            "total_ms": total_ms
        }),
    );
    // History keeps the raw (label-stripped) text, bullets and all: the
    // anti-repetition instruction tells the model to look at what it
    // already said, so it needs to see the actual structure it used last
    // time, not a version already split apart for display — but a leaked
    // "You:" must not persist into history either, or the next turn reads
    // it back as an example of the pattern to continue.
    Ok(cleaned.to_string())
}

/// True exactly when accumulating `duration_ms` onto `before` crosses a
/// multiple of `interval` — i.e. fire once per `interval` of continuous
/// silence, regardless of how large or small each audio chunk is.
fn crosses_interval(before: u64, after: u64, interval: u64) -> bool {
    after / interval > before / interval
}

/// Splits the model's final answer into a short lead-in and a list of bullet
/// points, per the two formats the prompt asks for. A personal/behavioral
/// answer is expected to contain no bullet lines at all — that is a fully
/// valid, common shape, not a parse failure — and returns no points, with
/// the whole text as the direction, exactly like before this split existed.
///
/// Markers checked: "- ", "• ", "* ", each followed by real content. Numbered
/// lists ("1. ...") are deliberately not recognized — the prompt asks for
/// "- " specifically, and treating bare digits as a bullet marker risks
/// swallowing a real sentence that happens to start with a number.
/// Defense in depth for the prompt's "never write a speaker label" rule:
/// verified live against a real model that, despite that explicit
/// instruction, it still opened an answer with a literal "You:" — models
/// don't reliably follow this every time, so it is also stripped
/// deterministically here rather than trusted to prompt compliance alone.
/// Only this short, specific set of known leak patterns, never any generic
/// "word:" prefix — a definitional answer legitimately might start with a
/// term followed by a colon (e.g. "CI/CD: it automates...").
fn strip_leaked_speaker_label(text: &str) -> &str {
    const LABELS: [&str; 5] = ["you:", "candidate:", "interviewer:", "answer:", "a:"];
    let trimmed = text.trim_start();
    let lower = trimmed.to_ascii_lowercase();
    for label in LABELS {
        if lower.starts_with(label) {
            return trimmed[label.len()..].trim_start();
        }
    }
    trimmed
}

fn split_into_direction_and_points(answer: &str) -> (String, Vec<String>) {
    let mut direction_lines = Vec::new();
    let mut points = Vec::new();
    for line in answer.lines() {
        let trimmed = line.trim();
        match bullet_content(trimmed) {
            Some(point) if !point.is_empty() => points.push(point.to_string()),
            Some(_) => {} // a marker with nothing after it (e.g. "- "): drop, not a blank point
            None if !trimmed.is_empty() => direction_lines.push(trimmed.to_string()),
            None => {}
        }
    }
    (direction_lines.join(" "), points)
}

/// `line` is already trimmed. Requires the marker be followed by whitespace
/// or be the whole line, so "-5" and "*emphasis*" are left as ordinary text
/// rather than misread as bullets — trimming the marker off first and
/// checking for a literal "- " would miss an intentionally empty bullet like
/// "- ", since trimming the line already ate that trailing space.
fn bullet_content(line: &str) -> Option<&str> {
    let rest = line
        .strip_prefix('-')
        .or_else(|| line.strip_prefix('•'))
        .or_else(|| line.strip_prefix('*'))?;
    if rest.is_empty() || rest.starts_with(char::is_whitespace) {
        Some(rest.trim())
    } else {
        None
    }
}

fn format_conversation(history: &[(String, String)]) -> String {
    if history.is_empty() {
        return "None yet.".to_string();
    }
    // Bracketed, clearly-structural labels — not "Interviewer:"/"You:".
    // Verified live against Groq: those looked enough like an actual
    // dialogue transcript that a real model continued the pattern, opening
    // its own new answer with a literal "You:" prefix and repeating the
    // previous turn's story almost verbatim instead of treating this as
    // reference material to write something new from.
    history
        .iter()
        .map(|(q, a)| format!("[Previously asked] {q}\n[Previous answer] {a}"))
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn looks_like_question(text: &str) -> bool {
    let normalized = text.trim().to_lowercase();
    if normalized.ends_with('?') {
        return true;
    }
    let starters = [
        "what ",
        "why ",
        "how ",
        "when ",
        "where ",
        "who ",
        "which ",
        "tell me",
        "describe ",
        "explain ",
        "walk me",
        "give me",
        "can you",
        "could you",
        "would you",
        "do you",
        "did you",
        "have you",
        "are you",
        "is there",
        "share an example",
    ];
    starters.iter().any(|prefix| normalized.starts_with(prefix))
}

fn pcm_duration_ms(pcm: &[u8]) -> u64 {
    (pcm.len() as u64 / 2) * 1000 / super::audio::TARGET_RATE as u64
}

fn frame_rms(pcm: &[u8]) -> f32 {
    let mut sum = 0.0_f64;
    let mut count = 0_u64;
    for bytes in pcm.chunks_exact(2) {
        let sample = i16::from_le_bytes([bytes[0], bytes[1]]) as f64 / i16::MAX as f64;
        sum += sample * sample;
        count += 1;
    }
    if count == 0 {
        0.0
    } else {
        (sum / count as f64).sqrt() as f32
    }
}

/// Turn a window of measured RMS samples into a voice threshold for this
/// device: the mean of the quietest quarter, scaled above the floor and
/// clamped to a sane range.
///
/// The quietest quarter rather than the mean of the whole window, because
/// speech has pauses even when someone talks through the entire calibration
/// window — the low samples during those pauses are the true noise floor,
/// and the loud samples in between would drag a plain mean up toward a
/// threshold real speech might not clear.
fn calibrate_threshold(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return FALLBACK_VOICE_RMS_THRESHOLD;
    }
    let mut sorted = samples.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let quarter = (sorted.len() / 4).max(1);
    let floor = sorted[..quarter].iter().sum::<f32>() / quarter as f32;
    (floor * THRESHOLD_ABOVE_FLOOR).clamp(MIN_VOICE_RMS_THRESHOLD, MAX_VOICE_RMS_THRESHOLD)
}

fn wav_bytes(pcm: &[u8]) -> Vec<u8> {
    let mut wav = Vec::with_capacity(44 + pcm.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16_u32.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&super::audio::TARGET_RATE.to_le_bytes());
    wav.extend_from_slice(&(super::audio::TARGET_RATE * 2).to_le_bytes());
    wav.extend_from_slice(&2_u16.to_le_bytes());
    wav.extend_from_slice(&16_u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&(pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(pcm);
    wav
}

fn concise_error(raw: &str) -> String {
    serde_json::from_str::<serde_json::Value>(raw)
        .ok()
        .and_then(|value| {
            value
                .pointer("/error/message")
                .and_then(|item| item.as_str())
                .map(str::to_string)
        })
        .unwrap_or_else(|| raw.chars().take(180).collect())
}

fn should_rotate_key(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::UNAUTHORIZED
        || status == reqwest::StatusCode::FORBIDDEN
        || status == reqwest::StatusCode::TOO_MANY_REQUESTS
        || status.is_server_error()
}

fn truncate_context(value: &str, max_chars: usize) -> String {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return "Not provided.".to_string();
    }
    trimmed.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_interview_questions_without_punctuation() {
        assert!(looks_like_question("Tell me about a difficult project"));
        assert!(looks_like_question("How did you resolve the conflict"));
        assert!(!looks_like_question("Thanks, that is all"));
    }

    #[test]
    fn wav_header_describes_pcm_payload() {
        let wav = wav_bytes(&[1, 2, 3, 4]);
        assert_eq!(&wav[..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[40..44], &4_u32.to_le_bytes());
        assert_eq!(&wav[44..], &[1, 2, 3, 4]);
    }

    #[test]
    fn a_plain_narrative_answer_has_no_points() {
        // The common, expected shape for a personal/behavioral question —
        // no bullet markers at all, so the whole thing stays one direction.
        let answer = "I led the migration myself and it cut our deploy time in half.";
        let (direction, points) = split_into_direction_and_points(answer);
        assert_eq!(direction, answer);
        assert!(points.is_empty());
    }

    #[test]
    fn a_lead_in_plus_bullets_splits_into_both() {
        let answer = "CI/CD automates getting code from commit to production:\n\
             - Continuous Integration merges and tests changes frequently\n\
             - Continuous Delivery keeps the build always deployable\n\
             - Continuous Deployment ships every passing change automatically";
        let (direction, points) = split_into_direction_and_points(answer);
        assert_eq!(
            direction,
            "CI/CD automates getting code from commit to production:"
        );
        assert_eq!(
            points,
            vec![
                "Continuous Integration merges and tests changes frequently",
                "Continuous Delivery keeps the build always deployable",
                "Continuous Deployment ships every passing change automatically",
            ]
        );
    }

    #[test]
    fn bullets_with_no_lead_in_leave_direction_empty_not_missing() {
        let answer = "- First point\n- Second point";
        let (direction, points) = split_into_direction_and_points(answer);
        assert_eq!(direction, "");
        assert_eq!(points, vec!["First point", "Second point"]);
    }

    #[test]
    fn every_common_bullet_marker_is_recognized() {
        let answer = "- dash\n• dot\n* star";
        let (_, points) = split_into_direction_and_points(answer);
        assert_eq!(points, vec!["dash", "dot", "star"]);
    }

    #[test]
    fn an_empty_bullet_line_is_dropped_not_kept_as_a_blank_point() {
        let answer = "Intro line\n- \n- real point";
        let (direction, points) = split_into_direction_and_points(answer);
        assert_eq!(direction, "Intro line");
        assert_eq!(points, vec!["real point"]);
    }

    #[test]
    fn empty_input_produces_empty_direction_and_no_points() {
        let (direction, points) = split_into_direction_and_points("");
        assert_eq!(direction, "");
        assert!(points.is_empty());
    }

    #[test]
    fn known_leaked_speaker_labels_are_stripped() {
        assert_eq!(
            strip_leaked_speaker_label("You: I led the migration myself."),
            "I led the migration myself."
        );
        assert_eq!(
            strip_leaked_speaker_label("Interviewer: that's a great question"),
            "that's a great question"
        );
        // Case-insensitive, and works with no space after the colon too.
        assert_eq!(strip_leaked_speaker_label("YOU:hello"), "hello");
    }

    #[test]
    fn a_definitional_answer_starting_with_a_term_and_colon_is_left_alone() {
        // "CI/CD:" is not a speaker label — stripping any generic "word:"
        // prefix would corrupt a legitimate definitional answer that opens
        // exactly this way, which is a common, correct shape for FORMAT B.
        let answer = "CI/CD: it automates getting code from commit to production.";
        assert_eq!(strip_leaked_speaker_label(answer), answer);
    }

    #[test]
    fn text_with_no_label_at_all_is_returned_unchanged() {
        let answer = "I focus on root causes instead of quick patches.";
        assert_eq!(strip_leaked_speaker_label(answer), answer);
    }

    #[test]
    fn a_dash_not_followed_by_whitespace_is_not_mistaken_for_a_bullet() {
        // "we cut latency by -5ms" and "*emphasis*" are ordinary sentences,
        // not bullets — a marker character glued to real content, not
        // followed by whitespace or standing alone, must stay untouched.
        let answer = "Throughput improved -5ms on average.\n*emphasis* still reads as text.";
        let (direction, points) = split_into_direction_and_points(answer);
        assert_eq!(direction, answer.replace('\n', " "));
        assert!(points.is_empty());
    }

    #[test]
    fn silence_has_zero_rms() {
        assert_eq!(frame_rms(&[0; 320]), 0.0);
    }

    #[test]
    fn calibration_tracks_a_quiet_device_instead_of_the_fixed_default() {
        // A Windows loopback device roughly an order of magnitude quieter
        // than the value the fallback was tuned against.
        let quiet_noise_floor: Vec<f32> = vec![0.0008, 0.0009, 0.0007, 0.0010, 0.0006];
        let threshold = calibrate_threshold(&quiet_noise_floor);
        assert!(
            threshold < FALLBACK_VOICE_RMS_THRESHOLD,
            "a device this quiet must calibrate below the fixed fallback, got {threshold}"
        );
        assert!(threshold >= MIN_VOICE_RMS_THRESHOLD);
    }

    #[test]
    fn calibration_ignores_loud_samples_mixed_into_the_window() {
        // Someone talking through part of the calibration window: the loud
        // samples must not drag the floor estimate up with them.
        let mixed: Vec<f32> = vec![0.001, 0.0009, 0.15, 0.18, 0.001, 0.0011, 0.2, 0.001];
        let threshold = calibrate_threshold(&mixed);
        assert!(
            threshold < 0.01,
            "loud transients in the window should not raise the floor estimate, got {threshold}"
        );
    }

    #[test]
    fn calibration_never_exceeds_the_configured_bounds() {
        assert!(calibrate_threshold(&[0.0; 10]) >= MIN_VOICE_RMS_THRESHOLD);
        assert!(calibrate_threshold(&[1.0; 10]) <= MAX_VOICE_RMS_THRESHOLD);
    }

    #[test]
    fn calibration_falls_back_with_no_samples() {
        assert_eq!(calibrate_threshold(&[]), FALLBACK_VOICE_RMS_THRESHOLD);
    }

    #[test]
    fn reasoning_params_are_sent_only_to_models_that_accept_them() {
        // The default and every non-gpt-oss model 400 if these are present —
        // this is what shipped and broke live replies.
        assert!(!is_reasoning_model("allam-2-7b"));
        assert!(!is_reasoning_model("llama-3.3-70b-versatile"));
        assert!(!is_reasoning_model("qwen/qwen3.6-27b"));
        assert!(is_reasoning_model("openai/gpt-oss-20b"));
        assert!(is_reasoning_model("openai/gpt-oss-120b"));
        assert!(is_reasoning_model("openai/gpt-oss-safeguard-20b"));
    }

    #[test]
    fn provider_parse_round_trips_through_as_str_and_falls_back_to_groq() {
        for provider in [
            ChatProvider::Groq,
            ChatProvider::OpenAi,
            ChatProvider::Anthropic,
            ChatProvider::Gemini,
            ChatProvider::Bedrock,
        ] {
            assert_eq!(ChatProvider::parse(provider.as_str()), provider);
        }
        // Aliases a user might reasonably type.
        assert_eq!(ChatProvider::parse("Claude"), ChatProvider::Anthropic);
        assert_eq!(ChatProvider::parse("GOOGLE"), ChatProvider::Gemini);
        assert_eq!(ChatProvider::parse("AWS"), ChatProvider::Bedrock);
        assert_eq!(ChatProvider::parse("Amazon"), ChatProvider::Bedrock);
        // An unrecognized or empty value must not silently pick a paid
        // provider the user never selected.
        assert_eq!(ChatProvider::parse(""), ChatProvider::Groq);
        assert_eq!(ChatProvider::parse("made-up-provider"), ChatProvider::Groq);
    }

    #[test]
    fn every_provider_has_a_distinct_nonempty_default_model() {
        let providers = [
            ChatProvider::Groq,
            ChatProvider::OpenAi,
            ChatProvider::Anthropic,
            ChatProvider::Gemini,
            ChatProvider::Bedrock,
        ];
        let models: Vec<&str> = providers.iter().map(|p| p.default_model()).collect();
        assert!(models.iter().all(|m| !m.is_empty()));
        let mut unique = models.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(
            unique.len(),
            models.len(),
            "default models must be distinct: {models:?}"
        );
    }

    #[test]
    fn extract_delta_text_reads_the_openai_compatible_shape() {
        let chunk = serde_json::json!({
            "choices": [{ "delta": { "content": "Hello" } }]
        });
        assert_eq!(
            extract_delta_text(ChatProvider::Groq, &chunk),
            Some("Hello".to_string())
        );
        assert_eq!(
            extract_delta_text(ChatProvider::OpenAi, &chunk),
            Some("Hello".to_string())
        );
    }

    #[test]
    fn extract_delta_text_reads_anthropic_content_block_delta_only() {
        let text_chunk = serde_json::json!({
            "type": "content_block_delta",
            "delta": { "type": "text_delta", "text": "Hello" }
        });
        assert_eq!(
            extract_delta_text(ChatProvider::Anthropic, &text_chunk),
            Some("Hello".to_string())
        );
        // message_start, content_block_start, ping, message_stop and friends
        // carry no answer text — treating them as an empty delta instead of
        // skipping them would still work, but None makes the "no text here"
        // case explicit rather than accidental.
        let message_start = serde_json::json!({ "type": "message_start" });
        assert_eq!(
            extract_delta_text(ChatProvider::Anthropic, &message_start),
            None
        );
    }

    #[test]
    fn extract_delta_text_reads_the_gemini_candidates_shape() {
        let chunk = serde_json::json!({
            "candidates": [{ "content": { "parts": [{ "text": "Hello" }] } }]
        });
        assert_eq!(
            extract_delta_text(ChatProvider::Gemini, &chunk),
            Some("Hello".to_string())
        );
    }

    #[test]
    fn extract_delta_text_returns_none_on_an_unrecognized_shape_instead_of_panicking() {
        let empty = serde_json::json!({});
        for provider in [
            ChatProvider::Groq,
            ChatProvider::OpenAi,
            ChatProvider::Anthropic,
            ChatProvider::Gemini,
            ChatProvider::Bedrock,
        ] {
            assert_eq!(extract_delta_text(provider, &empty), None);
        }
    }

    #[test]
    fn bedrock_request_uses_the_documented_converse_shape_and_endpoint() {
        // Verified against AWS's own Converse API reference, not guessed:
        // inferenceConfig.maxTokens/temperature (not max_tokens/temperature
        // at the top level like the OpenAI-compatible providers), and
        // content as an array of {"text": ...} blocks, not a bare string.
        let client = reqwest::Client::new();
        let request = build_chat_request(
            &client,
            ChatProvider::Bedrock,
            "test-key",
            "amazon.nova-lite-v1:0",
            "hello",
        )
        .build()
        .unwrap();

        assert_eq!(
            request.url().as_str(),
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.nova-lite-v1:0/converse"
        );
        assert_eq!(
            request.headers().get("authorization").unwrap(),
            "Bearer test-key"
        );

        let body: serde_json::Value =
            serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap();
        assert_eq!(body["messages"][0]["role"], "user");
        assert_eq!(body["messages"][0]["content"][0]["text"], "hello");
        assert_eq!(body["inferenceConfig"]["maxTokens"], MAX_ANSWER_TOKENS);
        // The non-streaming endpoint, not converse-stream: Bedrock's real
        // streaming format is AWS's binary vnd.amazon.eventstream framing,
        // not text SSE, and was never verified against a real successful
        // response (see build_chat_request's own comment for why).
        assert!(!request.url().path().contains("stream"));
    }

    #[test]
    fn bedrock_never_falls_back_to_groq_keys() {
        let settings = Settings {
            api_keys: vec!["gsk_should_not_be_used".to_string()],
            role_title: String::new(),
            company_name: String::new(),
            resume_text: String::new(),
            job_description: String::new(),
            language: "en".to_string(),
            chat_model: String::new(),
            chat_provider: ChatProvider::Bedrock,
            chat_api_keys: vec!["bedrock-key".to_string()],
        };
        assert_eq!(settings.effective_chat_keys(), &["bedrock-key".to_string()]);
    }

    #[test]
    fn groq_provider_reuses_transcription_keys_but_other_providers_need_their_own() {
        let mut settings = Settings {
            api_keys: vec!["gsk_shared".to_string()],
            role_title: String::new(),
            company_name: String::new(),
            resume_text: String::new(),
            job_description: String::new(),
            language: "en".to_string(),
            chat_model: String::new(),
            chat_provider: ChatProvider::Groq,
            chat_api_keys: Vec::new(),
        };
        assert_eq!(settings.effective_chat_keys(), &["gsk_shared".to_string()]);

        settings.chat_provider = ChatProvider::Anthropic;
        assert!(
            settings.effective_chat_keys().is_empty(),
            "an unconfigured non-Groq provider must not silently fall back to Groq's keys"
        );

        settings.chat_api_keys = vec!["sk-ant-real".to_string()];
        assert_eq!(settings.effective_chat_keys(), &["sk-ant-real".to_string()]);
    }

    #[test]
    fn silence_alert_fires_once_per_interval_regardless_of_chunk_size() {
        // One big jump that skips straight past the boundary still fires once.
        assert!(crosses_interval(0, 12_000, 12_000));
        assert!(crosses_interval(11_999, 12_001, 12_000));
        // Small steps that stay within the same interval never fire.
        assert!(!crosses_interval(100, 200, 12_000));
        // A second interval's worth of silence fires again, not the first's leftovers.
        assert!(crosses_interval(23_999, 24_001, 12_000));
        assert!(!crosses_interval(12_001, 13_000, 12_000));
    }

    #[test]
    fn conversation_history_is_empty_by_default_and_formatted_when_present() {
        assert_eq!(format_conversation(&[]), "None yet.");
        let history = vec![(
            "Tell me about a project.".to_string(),
            "I built...".to_string(),
        )];
        assert_eq!(
            format_conversation(&history),
            "[Previously asked] Tell me about a project.\n[Previous answer] I built..."
        );
    }

    #[test]
    fn context_is_bounded_and_empty_context_is_explicit() {
        assert_eq!(truncate_context("", 4), "Not provided.");
        assert_eq!(truncate_context("abcdef", 4), "abcd");
    }

    #[test]
    fn key_rotation_is_limited_to_auth_capacity_and_server_failures() {
        assert!(should_rotate_key(reqwest::StatusCode::UNAUTHORIZED));
        assert!(should_rotate_key(reqwest::StatusCode::TOO_MANY_REQUESTS));
        assert!(should_rotate_key(reqwest::StatusCode::BAD_GATEWAY));
        assert!(!should_rotate_key(reqwest::StatusCode::BAD_REQUEST));
    }
}
