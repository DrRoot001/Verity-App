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
const MAX_UTTERANCE_MS: u64 = 10_000;
const VOICE_RMS_THRESHOLD: f32 = 0.012;
/// How long a stretch of pure silence (nothing crossing `VOICE_RMS_THRESHOLD`)
/// can run before the HUD is told, instead of continuing to claim "Audio is
/// active" while nothing is actually reaching the pipeline — e.g. the wrong
/// device is selected, or interview audio isn't routed to it.
const NO_VOICE_ALERT_MS: u64 = 12_000;
/// How often the live level meter updates. Frequent enough to feel
/// real-time, far below IPC-flooding territory.
const LEVEL_EMIT_MS: u64 = 150;
/// RMS treated as "full scale" for the HUD meter. Comfortably above normal
/// speech (~0.02-0.05) so the bar has headroom before clipping visually.
const LEVEL_REFERENCE_RMS: f32 = 0.15;
const STT_MODEL: &str = "whisper-large-v3-turbo";
// Measured against the live Groq API: openai/gpt-oss-20b's time-to-first-
// token ranged 300ms-1.3s+ (occasionally 500-erroring) across otherwise
// identical warm-connection requests, while allam-2-7b answered the same
// prompts in a consistent ~120-140ms with equivalent quality once the
// prompt explicitly forbade preamble/meta-commentary. Swapped for the
// latency-sensitive default; still user-overridable in Advanced settings.
const DEFAULT_CHAT_MODEL: &str = "allam-2-7b";
/// Recent Q&A pairs kept so a follow-up like "what was the hardest part?"
/// still has an antecedent, without unbounded prompt growth.
const MAX_HISTORY_TURNS: usize = 6;

#[derive(Debug, Clone)]
pub struct Settings {
    pub api_keys: Vec<String>,
    pub role_title: String,
    pub company_name: String,
    pub resume_text: String,
    pub job_description: String,
    pub language: String,
    pub chat_model: String,
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
            match process_utterance(&processor_app, &client, &settings, &history, utterance).await
            {
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
                let voiced = rms >= VOICE_RMS_THRESHOLD;

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
                            "level": (rms / LEVEL_REFERENCE_RMS).min(1.0),
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
    let resume = truncate_context(&settings.resume_text, 6_000);
    let job_description = truncate_context(&settings.job_description, 6_000);
    let conversation = format_conversation(history);
    let prompt = format!(
        "You are a live interview answer coach. The candidate is in {context}. \
         Output ONLY the exact words the candidate should say aloud right now — never comment on the \
         resume, never explain your reasoning or what you're about to do, never start with phrases like \
         \"While reviewing...\" or \"I notice...\" or \"As a safe answer...\". The very first word must be \
         part of the spoken answer itself. \
         Write in first person, be confident and natural, and stay under 90 words. \
         Use only facts supported by the resume. Align to the job description without copying it. \
         If personal facts are unknown, still answer directly in first person with a safe adaptable \
         response — never invent employers, dates, or metrics, and never tell the candidate that facts are missing. \
         If the question refers back to something earlier (e.g. \"the hardest part\", \"that project\"), resolve it using the recent conversation below.\n\n\
         RESUME CONTEXT:\n{resume}\n\nJOB DESCRIPTION:\n{job_description}\n\nRECENT CONVERSATION:\n{conversation}\n\nINTERVIEWER QUESTION:\n{question}"
    );
    let model = if settings.chat_model.trim().is_empty() {
        DEFAULT_CHAT_MODEL
    } else {
        settings.chat_model.trim()
    };
    let request_body = json!({
        "model": model,
        "messages": [{ "role": "user", "content": prompt }],
        "stream": true,
        "reasoning_effort": "low",
        "include_reasoning": false,
        "temperature": 0.3,
        "max_completion_tokens": 160
    });
    let mut response = None;
    let mut last_error = "Groq answer failed.".to_string();
    for offset in 0..settings.api_keys.len() {
        let index = (preferred_key_index + offset) % settings.api_keys.len();
        let result = client
            .post("https://api.groq.com/openai/v1/chat/completions")
            .bearer_auth(&settings.api_keys[index])
            .json(&request_body)
            .timeout(std::time::Duration::from_secs(12))
            .send()
            .await;
        let candidate = match result {
            Ok(candidate) => candidate,
            Err(error) => {
                last_error = format!("Groq key {} network error: {error}", index + 1);
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
            "Groq key {} answer failed ({status}): {}",
            index + 1,
            concise_error(&detail)
        );
        if !should_rotate_key(status) {
            return Err(anyhow!(last_error));
        }
    }
    let response = response.ok_or_else(|| anyhow!(last_error))?;

    let mut stream = response.bytes_stream();
    let mut pending = String::new();
    let mut answer = String::new();
    let mut first_token_ms = None;
    while let Some(chunk) = stream.next().await {
        pending.push_str(&String::from_utf8_lossy(&chunk?));
        while let Some(newline) = pending.find('\n') {
            let line = pending[..newline].trim().to_string();
            pending.drain(..=newline);
            let Some(data) = line.strip_prefix("data: ") else {
                continue;
            };
            if data == "[DONE]" {
                break;
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(data) else {
                continue;
            };
            let delta = value["choices"][0]["delta"]["content"]
                .as_str()
                .unwrap_or_default();
            if delta.is_empty() {
                continue;
            }
            answer.push_str(delta);
            let first = *first_token_ms
                .get_or_insert_with(|| detection_delay_ms + queued_at.elapsed().as_millis() as u64);
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
    let total_ms = detection_delay_ms + queued_at.elapsed().as_millis() as u64;
    let generation_ms = generation_started.elapsed().as_millis() as u64;
    emit(
        app,
        "answer.complete",
        json!({
            "content": {
                "answer_direction": answer.trim(),
                "key_points": [],
                "structure": ""
            },
            "first_response_ms": first_token_ms,
            "stt_ms": stt_ms,
            "generation_ms": generation_ms,
            "detection_ms": detection_delay_ms,
            "total_ms": total_ms
        }),
    );
    Ok(answer.trim().to_string())
}

/// True exactly when accumulating `duration_ms` onto `before` crosses a
/// multiple of `interval` — i.e. fire once per `interval` of continuous
/// silence, regardless of how large or small each audio chunk is.
fn crosses_interval(before: u64, after: u64, interval: u64) -> bool {
    after / interval > before / interval
}

fn format_conversation(history: &[(String, String)]) -> String {
    if history.is_empty() {
        return "None yet.".to_string();
    }
    history
        .iter()
        .map(|(q, a)| format!("Interviewer: {q}\nYou: {a}"))
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
    fn silence_has_zero_rms() {
        assert_eq!(frame_rms(&[0; 320]), 0.0);
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
        let history = vec![("Tell me about a project.".to_string(), "I built...".to_string())];
        assert_eq!(
            format_conversation(&history),
            "Interviewer: Tell me about a project.\nYou: I built..."
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
