//! The live interview pipeline.
//!
//! ```text
//! capture ─▶ segmenter ──utterances──▶ transcriber ──transcripts──▶ assembler ─▶ answer stream
//!                 └──────────────── "still speaking" signals ─────────▲
//! ```
//!
//! * The **segmenter** cuts audio into utterances at 360 ms of quiet, so
//!   transcription starts the moment the interviewer pauses.
//! * The **transcriber** turns each utterance into text on the chosen
//!   transcription provider. It runs in its own task, so the next utterance
//!   is transcribed while the previous answer is still streaming.
//! * The **assembler** decides what the question actually is (see
//!   `questions`): it answers a finished question at once, holds one that
//!   stops mid-sentence for the rest of it, and rewrites the answer when the
//!   interviewer adds to the question right after asking it.
//! * The **answer** streams token by token from the chosen answer provider.
//!
//! No web account, backend or database is involved; the only network traffic
//! is to the AI providers the user holds keys for.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use futures_util::StreamExt;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::audio::{CaptureMessage, TARGET_RATE};
use crate::providers::{self, ChatEngine, Provider, ProviderKeys, SttEngine};
use crate::questions::{self, Readiness};

const SILENCE_FLUSH_MS: u64 = 360;
const MIN_VOICE_MS: u64 = 300;
/// A hard cap, not a target: segmentation normally flushes on
/// `SILENCE_FLUSH_MS` of quiet, so this only fires on 7 s of speech with no
/// gap that long. A question longer than that is no longer split into two
/// questions: the assembler merges the second part back in.
const MAX_UTTERANCE_MS: u64 = 7_000;
/// Fallback only, used until calibration produces a real number: captured
/// loopback level varies by an order of magnitude across OS and driver.
const FALLBACK_VOICE_RMS_THRESHOLD: f32 = 0.012;
/// How long pure silence can run before the HUD says so, instead of claiming
/// "audio is active" while the wrong device is selected.
const NO_VOICE_ALERT_MS: u64 = 12_000;
/// How often the live level meter updates.
const LEVEL_EMIT_MS: u64 = 150;
/// How long to sample the stream before committing to a voice threshold.
const CALIBRATION_MS: u64 = 1_500;
/// The calibrated threshold is this multiple of the measured noise floor.
const THRESHOLD_ABOVE_FLOOR: f32 = 4.0;
const MIN_VOICE_RMS_THRESHOLD: f32 = 0.003;
const MAX_VOICE_RMS_THRESHOLD: f32 = 0.05;
/// The HUD meter's full scale, as a multiple of the voice threshold.
const LEVEL_REFERENCE_ABOVE_THRESHOLD: f32 = 6.0;
/// While the interviewer is audibly speaking, the assembler is told this
/// often, so a question waiting for its second half waits exactly as long as
/// that second half is being spoken.
const VOICE_HEARTBEAT_MS: u64 = 400;
/// Idle connections are re-used for this long, and pinged at
/// `WARM_INTERVAL` so the TLS handshake (150-400 ms) is paid before a
/// question rather than after it — interviews have long gaps between
/// questions, longer than a pooled connection would otherwise survive.
const POOL_IDLE: Duration = Duration::from_secs(300);
const WARM_INTERVAL: Duration = Duration::from_secs(40);
/// Recent Q&A pairs kept so a follow-up ("what was the hardest part?") has
/// an antecedent. Measured against Groq's 6,000 TPM free tier: 3 turns plus
/// the two 1,500-char context fields is ~800 tokens per request, room for
/// ~7 requests/minute instead of 2.
const MAX_HISTORY_TURNS: usize = 3;
const CONTEXT_FIELD_MAX_CHARS: usize = 1_500;

#[derive(Debug, Clone)]
pub struct Settings {
    pub keys: ProviderKeys,
    pub chat_provider: Provider,
    pub chat_model: String,
    /// `None` = automatic (see `providers::resolve_stt_provider`).
    pub stt_provider: Option<Provider>,
    pub role_title: String,
    pub company_name: String,
    pub resume_text: String,
    pub job_description: String,
    pub language: String,
}

impl Settings {
    pub fn keys_for(&self, provider: Provider) -> Vec<String> {
        self.keys.get(&provider).cloned().unwrap_or_default()
    }

    /// Both engines for this session, or the reason the session cannot
    /// start — checked before the audio device is opened, so a missing key
    /// is reported on the setup screen rather than mid-interview.
    pub fn engines(&self) -> Result<(SttEngine, ChatEngine)> {
        let chat_keys = self.keys_for(self.chat_provider);
        if chat_keys.is_empty() {
            return Err(anyhow!(
                "Add at least one {} API key for answers.",
                self.chat_provider.label()
            ));
        }
        let stt_provider =
            providers::resolve_stt_provider(self.stt_provider, self.chat_provider, &self.keys)?;
        Ok((
            SttEngine::new(stt_provider, self.keys_for(stt_provider), &self.language),
            ChatEngine::new(self.chat_provider, &self.chat_model, chat_keys),
        ))
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ServerEvent {
    pub kind: String,
    pub payload: Value,
}

/// Where pipeline events go. The app forwards them to the HUD; tests
/// collect them.
pub type EventSink = Arc<dyn Fn(&str, Value) + Send + Sync>;

pub fn tauri_sink(app: tauri::AppHandle) -> EventSink {
    use tauri::Emitter;
    Arc::new(move |kind: &str, payload: Value| {
        let _ = app.emit(
            "verity://event",
            ServerEvent {
                kind: kind.to_string(),
                payload,
            },
        );
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct ApiTestResult {
    pub working_key: usize,
    pub total_keys: usize,
    pub latency_ms: u64,
    pub detail: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TestKind {
    Answers,
    Transcription,
}

pub fn http_client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .tcp_nodelay(true)
        .pool_max_idle_per_host(4)
        .pool_idle_timeout(POOL_IDLE)
        .tcp_keepalive(Duration::from_secs(30))
        .build()?)
}

/// Proves a provider will actually do the job before an interview depends
/// on it: a real one-word generation with the configured model (which
/// catches a retired model or Bedrock's per-model access grant, not just a
/// bad key), or a real transcription of half a second of silence.
pub async fn test_provider(
    provider: Provider,
    keys: Vec<String>,
    model: &str,
    kind: TestKind,
) -> Result<ApiTestResult> {
    let client = http_client()?;
    let total_keys = keys.len();
    let started = Instant::now();
    match kind {
        TestKind::Answers => {
            let chat = ChatEngine::new(provider, model, keys);
            let response = chat.open(&client, "Reply with the single word OK.").await?;
            // Drain it so a mid-stream failure still counts as a failure.
            let mut stream = response.bytes_stream();
            while let Some(chunk) = stream.next().await {
                chunk?;
            }
            Ok(ApiTestResult {
                working_key: chat.ring().current() + 1,
                total_keys,
                latency_ms: started.elapsed().as_millis() as u64,
                detail: format!("{} answered with {}", provider.label(), chat.model()),
            })
        }
        TestKind::Transcription => {
            if !provider.can_transcribe() {
                return Err(anyhow!(
                    "{} cannot transcribe audio; choose Groq, OpenAI or Gemini.",
                    provider.label()
                ));
            }
            let stt = SttEngine::new(provider, keys, "en");
            let silence = vec![0_u8; (TARGET_RATE as usize) * 2 / 2];
            stt.transcribe(&client, &wav_bytes(&silence)).await?;
            Ok(ApiTestResult {
                working_key: stt.ring().current() + 1,
                total_keys,
                latency_ms: started.elapsed().as_millis() as u64,
                detail: format!("{} transcribed with {}", provider.label(), stt.model()),
            })
        }
    }
}

/// A spawned task that is cancelled when its owner lets go of it — so
/// stopping a session or superseding an answer can never leave a request
/// streaming in the background.
struct AbortOnDrop(Option<JoinHandle<()>>);

impl AbortOnDrop {
    fn spawn(future: impl std::future::Future<Output = ()> + Send + 'static) -> Self {
        Self(Some(tokio::spawn(future)))
    }

    async fn join(mut self) {
        if let Some(handle) = self.0.take() {
            let _ = handle.await;
        }
    }
}

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            handle.abort();
        }
    }
}

struct Utterance {
    pcm: Vec<u8>,
    queued_at: Instant,
    detection_delay_ms: u64,
    /// Position on the audio timeline (ms since the session started) where
    /// speech in this utterance began and where it last had voice.
    start_ms: u64,
    end_ms: u64,
}

struct Transcript {
    text: String,
    start_ms: u64,
    end_ms: u64,
    queued_at: Instant,
    detection_delay_ms: u64,
    stt_ms: u64,
}

enum Heard {
    /// The interviewer is speaking in an utterance that began at this point
    /// on the audio timeline.
    Voice {
        utterance_start_ms: u64,
    },
    Transcript(Transcript),
}

/// Segment captured PCM into utterances and run the pipeline until stopped.
pub async fn run_session(
    sink: EventSink,
    settings: Settings,
    mut audio: mpsc::Receiver<CaptureMessage>,
    mut stop: mpsc::Receiver<()>,
    log_path: Option<PathBuf>,
) -> Result<()> {
    let (stt, chat) = settings.engines()?;
    let client = http_client()?;
    let context = Arc::new(AnswerContext::from(&settings));

    let mut warm_targets = vec![(stt.provider, settings.keys_for(stt.provider))];
    if chat.provider != stt.provider {
        warm_targets.push((chat.provider, settings.keys_for(chat.provider)));
    }
    let _warmer = AbortOnDrop::spawn(keep_warm(client.clone(), warm_targets));

    let (utterance_tx, utterance_rx) = mpsc::channel::<Utterance>(8);
    let (heard_tx, heard_rx) = mpsc::unbounded_channel::<Heard>();
    let transcriber = AbortOnDrop::spawn(transcribe_utterances(
        sink.clone(),
        client.clone(),
        stt.clone(),
        utterance_rx,
        heard_tx.clone(),
    ));
    let assembler = AbortOnDrop::spawn(
        Assembler::new(sink.clone(), client, chat.clone(), context).run(heard_rx),
    );

    sink(
        "session.ready",
        json!({
            "mode": "standalone",
            "stt_provider": stt.provider.label(),
            "stt_model": stt.model(),
            "chat_provider": chat.provider.label(),
            "chat_model": chat.model()
        }),
    );

    let mut buffer = Vec::new();
    let mut held_ms = 0_u64;
    let mut voiced_ms = 0_u64;
    let mut quiet_ms = 0_u64;
    let mut explicitly_stopped = false;
    let mut device_error = None;
    let mut silence_since_voice_ms = 0_u64;
    let mut elapsed_ms = 0_u64;
    // Audio timeline, advanced by each chunk's duration rather than read
    // from a clock, so merge decisions depend on what was said, not on how
    // fast it was processed.
    let mut audio_ms = 0_u64;
    let mut utterance_start_ms: Option<u64> = None;
    let mut last_voice_end_ms = 0_u64;
    let mut last_heartbeat_ms = 0_u64;
    let mut calibration_samples: Vec<f32> = Vec::new();
    let mut calibration_ms_elapsed = 0_u64;
    let mut calibrated = false;
    let mut voice_threshold = FALLBACK_VOICE_RMS_THRESHOLD;
    let mut level_reference = FALLBACK_VOICE_RMS_THRESHOLD * LEVEL_REFERENCE_ABOVE_THRESHOLD;
    let mut log_window_ms = 0_u64;
    let mut log_window_peak_rms = 0_f32;
    const LOG_WINDOW_MS: u64 = 2_000;

    loop {
        tokio::select! {
            message = audio.recv() => {
                // The capture ending on its own finishes the session
                // gracefully; it must not wait on a stop that never comes.
                let Some(message) = message else { break };
                let pcm = match message {
                    CaptureMessage::Pcm(pcm) => pcm,
                    CaptureMessage::Error(message) => {
                        device_error = Some(message);
                        break;
                    }
                };
                let duration_ms = pcm_duration_ms(&pcm);
                let rms = frame_rms(&pcm);
                audio_ms += duration_ms;

                if !calibrated {
                    calibration_samples.push(rms);
                    calibration_ms_elapsed += duration_ms;
                    if calibration_ms_elapsed >= CALIBRATION_MS {
                        voice_threshold = calibrate_threshold(&calibration_samples);
                        level_reference = voice_threshold * LEVEL_REFERENCE_ABOVE_THRESHOLD;
                        calibrated = true;
                        if let Some(path) = &log_path {
                            let sample_count = calibration_samples.len();
                            crate::debuglog::log(
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
                        crate::debuglog::log(
                            path,
                            &format!(
                                "level: peak_rms={log_window_peak_rms:.4} (threshold={voice_threshold:.4}, calibrated={calibrated}) over last {log_window_ms}ms"
                            ),
                        );
                    }
                    log_window_ms = 0;
                    log_window_peak_rms = 0.0;
                }

                if voiced {
                    silence_since_voice_ms = 0;
                } else {
                    let before = silence_since_voice_ms;
                    silence_since_voice_ms += duration_ms;
                    if crosses_interval(before, silence_since_voice_ms, NO_VOICE_ALERT_MS) {
                        if let Some(path) = &log_path {
                            crate::debuglog::log(
                                path,
                                &format!("audio.silence fired: {silence_since_voice_ms}ms with nothing crossing the voice threshold"),
                            );
                        }
                        sink("audio.silence", json!({ "silence_ms": silence_since_voice_ms }));
                    }
                }

                let previous_elapsed = elapsed_ms;
                elapsed_ms += duration_ms;
                if crosses_interval(previous_elapsed, elapsed_ms, LEVEL_EMIT_MS) {
                    sink(
                        "audio.level",
                        json!({ "level": (rms / level_reference).min(1.0), "voiced": voiced }),
                    );
                }

                if voiced {
                    let start = *utterance_start_ms.get_or_insert(audio_ms - duration_ms);
                    last_voice_end_ms = audio_ms;
                    if audio_ms.saturating_sub(last_heartbeat_ms) >= VOICE_HEARTBEAT_MS
                        || voiced_ms == 0
                    {
                        last_heartbeat_ms = audio_ms;
                        let _ = heard_tx.send(Heard::Voice { utterance_start_ms: start });
                    }
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
                        start_ms: utterance_start_ms.take().unwrap_or(audio_ms),
                        end_ms: last_voice_end_ms,
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
                    utterance_start_ms = None;
                }
            }
            // Only an actual stop request stops; a dropped sender alone
            // would otherwise read as one and abort a finishing answer.
            Some(()) = stop.recv() => {
                explicitly_stopped = true;
                break;
            },
        }
    }

    if explicitly_stopped || device_error.is_some() {
        // Dropping the tasks aborts them, including any answer mid-stream.
        drop(transcriber);
        drop(assembler);
        sink("session.ended", json!({}));
        return match device_error {
            Some(message) => Err(anyhow!("Audio device disconnected: {message}")),
            None => Ok(()),
        };
    }

    // The capture ended on its own: finish what was already heard.
    if voiced_ms >= MIN_VOICE_MS && !buffer.is_empty() {
        let _ = utterance_tx
            .send(Utterance {
                pcm: buffer,
                queued_at: Instant::now(),
                detection_delay_ms: 0,
                start_ms: utterance_start_ms.unwrap_or(audio_ms),
                end_ms: last_voice_end_ms,
            })
            .await;
    }
    drop(utterance_tx);
    drop(heard_tx);
    transcriber.join().await;
    assembler.join().await;
    sink("session.ended", json!({}));
    Ok(())
}

async fn keep_warm(client: reqwest::Client, targets: Vec<(Provider, Vec<String>)>) {
    loop {
        for (provider, keys) in &targets {
            if let Some(key) = keys.first() {
                providers::warm(&client, *provider, key).await;
            }
        }
        tokio::time::sleep(WARM_INTERVAL).await;
    }
}

async fn transcribe_utterances(
    sink: EventSink,
    client: reqwest::Client,
    stt: SttEngine,
    mut utterances: mpsc::Receiver<Utterance>,
    heard: mpsc::UnboundedSender<Heard>,
) {
    while let Some(utterance) = utterances.recv().await {
        let started = Instant::now();
        sink("stt.started", json!({ "provider": stt.provider.label() }));
        match stt.transcribe(&client, &wav_bytes(&utterance.pcm)).await {
            Ok(text) => {
                let stt_ms = started.elapsed().as_millis() as u64;
                if text.is_empty() {
                    sink("speech.ignored", json!({ "content": "" }));
                    continue;
                }
                sink(
                    "stt.final",
                    json!({ "content": text, "latency_ms": stt_ms }),
                );
                let _ = heard.send(Heard::Transcript(Transcript {
                    text,
                    start_ms: utterance.start_ms,
                    end_ms: utterance.end_ms,
                    queued_at: utterance.queued_at,
                    detection_delay_ms: utterance.detection_delay_ms,
                    stt_ms,
                }));
            }
            Err(error) => sink("warning", json!({ "message": error.to_string() })),
        }
    }
}

/// The session's fixed context, shaped once rather than per question.
struct AnswerContext {
    setting: String,
    resume: String,
    job_description: String,
}

impl From<&Settings> for AnswerContext {
    fn from(settings: &Settings) -> Self {
        let setting = match (settings.role_title.trim(), settings.company_name.trim()) {
            ("", "") => "a job interview".to_string(),
            (role, "") => format!("an interview for {role}"),
            ("", company) => format!("an interview at {company}"),
            (role, company) => format!("an interview for {role} at {company}"),
        };
        Self {
            setting,
            resume: truncate_context(&settings.resume_text, CONTEXT_FIELD_MAX_CHARS),
            job_description: truncate_context(&settings.job_description, CONTEXT_FIELD_MAX_CHARS),
        }
    }
}

fn build_prompt(context: &AnswerContext, history: &[(String, String)], question: &str) -> String {
    let AnswerContext {
        setting,
        resume,
        job_description,
    } = context;
    let conversation = format_conversation(history);
    format!(
        "You are a live interview answer coach. The candidate is in {setting}. \
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
    )
}

struct Pending {
    text: String,
    /// Where the question's last word is on the audio timeline.
    end_ms: u64,
    /// While waiting for the rest of a question: (wait until, never past).
    hold: Option<(Instant, Instant)>,
    /// The answer generation currently shown for this question.
    generation: Option<u64>,
    /// An earlier answer to part of this question, to be replaced on screen.
    replaces: Option<u64>,
    queued_at: Instant,
    detection_delay_ms: u64,
    stt_ms: u64,
}

/// Context kept ahead of a question: a couple of sentences, not a monologue.
const PREAMBLE_MAX_CHARS: usize = 600;

/// Whether speech starting at `start_ms` directly follows speech that ended
/// at `end_ms`.
fn follows(end_ms: u64, start_ms: u64) -> bool {
    start_ms >= end_ms && start_ms - end_ms <= questions::MERGE_GAP_MS
}

/// The last `max_chars` characters, starting at a word boundary.
fn keep_tail(text: &str, max_chars: usize) -> String {
    let count = text.chars().count();
    if count <= max_chars {
        return text.to_string();
    }
    let tail: String = text.chars().skip(count - max_chars).collect();
    match tail.find(' ') {
        Some(space) => tail[space + 1..].to_string(),
        None => tail,
    }
}

struct AnswerDone {
    generation: u64,
    question: String,
    answer: Option<String>,
}

struct Assembler {
    sink: EventSink,
    client: reqwest::Client,
    chat: ChatEngine,
    context: Arc<AnswerContext>,
    pending: Option<Pending>,
    /// (generation, question, answer)
    history: Vec<(u64, String, String)>,
    task: Option<AbortOnDrop>,
    next_generation: u64,
    /// The latest utterance heard speaking (start on the audio timeline, and
    /// when it was last heard), to tell "the interviewer has stopped" from
    /// "the interviewer paused and is already talking again".
    speaking: Option<(u64, Instant)>,
    /// What the interviewer said just before, when it was not itself a
    /// question: context for the question that follows ("We use Kafka for
    /// event sourcing. How would you guarantee exactly-once?"). (text, end)
    preamble: Option<(String, u64)>,
    done_tx: mpsc::UnboundedSender<AnswerDone>,
    done_rx: mpsc::UnboundedReceiver<AnswerDone>,
}

impl Assembler {
    fn new(
        sink: EventSink,
        client: reqwest::Client,
        chat: ChatEngine,
        context: Arc<AnswerContext>,
    ) -> Self {
        let (done_tx, done_rx) = mpsc::unbounded_channel();
        Self {
            sink,
            client,
            chat,
            context,
            pending: None,
            history: Vec::new(),
            task: None,
            next_generation: 1,
            speaking: None,
            preamble: None,
            done_tx,
            done_rx,
        }
    }

    async fn run(mut self, mut heard: mpsc::UnboundedReceiver<Heard>) {
        let mut listening = true;
        loop {
            let deadline = self
                .pending
                .as_ref()
                .and_then(|p| p.hold)
                .map(|(until, cap)| until.min(cap));
            let sleep_until = tokio::time::Instant::from_std(deadline.unwrap_or_else(Instant::now));
            tokio::select! {
                message = heard.recv(), if listening => match message {
                    Some(Heard::Voice { utterance_start_ms }) => self.on_voice(utterance_start_ms),
                    Some(Heard::Transcript(transcript)) => self.on_transcript(transcript),
                    None => {
                        listening = false;
                        if deadline.is_some() {
                            self.answer_pending();
                        }
                    }
                },
                Some(done) = self.done_rx.recv() => self.on_done(done),
                _ = tokio::time::sleep_until(sleep_until), if deadline.is_some() => self.answer_pending(),
            }
            if !listening && self.task.is_none() {
                break;
            }
        }
    }

    /// Live speech right after a held question keeps the wait going while
    /// the rest of it is being said.
    fn on_voice(&mut self, utterance_start_ms: u64) {
        self.speaking = Some((utterance_start_ms, Instant::now()));
        let Some(pending) = self.pending.as_mut() else {
            return;
        };
        let Some((until, cap)) = pending.hold else {
            return;
        };
        let continues = utterance_start_ms >= pending.end_ms
            && utterance_start_ms - pending.end_ms <= questions::MERGE_GAP_MS;
        if continues {
            let extended = Instant::now() + Duration::from_millis(questions::VOICE_EXTEND_MS);
            pending.hold = Some((until.max(extended).min(cap), cap));
        }
    }

    fn on_transcript(&mut self, transcript: Transcript) {
        if let Some(pending) = self.pending.as_mut() {
            let continues = transcript.start_ms >= pending.end_ms
                && questions::continues_question(
                    transcript.start_ms - pending.end_ms,
                    &transcript.text,
                );
            if continues {
                pending.text = questions::merge(&pending.text, &transcript.text);
                pending.end_ms = transcript.end_ms;
                pending.queued_at = transcript.queued_at;
                pending.detection_delay_ms = transcript.detection_delay_ms;
                pending.stt_ms = transcript.stt_ms;
                if let Some(previous) = pending.generation.take() {
                    // The earlier answer covered only part of the question.
                    pending.replaces = Some(previous);
                    self.task = None;
                    self.history
                        .retain(|(generation, _, _)| *generation != previous);
                }
                self.decide();
                return;
            }
        }

        if questions::looks_like_question(&transcript.text) {
            // A new question: whatever was still streaming for the last one
            // is abandoned in favour of what is being asked now.
            self.task = None;
            let text = match self.preamble.take() {
                Some((context, end_ms)) if follows(end_ms, transcript.start_ms) => {
                    questions::merge(&context, &transcript.text)
                }
                _ => transcript.text,
            };
            self.pending = Some(Pending {
                text,
                end_ms: transcript.end_ms,
                hold: None,
                generation: None,
                replaces: None,
                queued_at: transcript.queued_at,
                detection_delay_ms: transcript.detection_delay_ms,
                stt_ms: transcript.stt_ms,
            });
            self.decide();
        } else {
            if !questions::is_backchannel(&transcript.text) {
                let context = match self.preamble.take() {
                    Some((earlier, end_ms)) if follows(end_ms, transcript.start_ms) => {
                        questions::merge(&earlier, &transcript.text)
                    }
                    _ => transcript.text.clone(),
                };
                self.preamble = Some((keep_tail(&context, PREAMBLE_MAX_CHARS), transcript.end_ms));
            }
            (self.sink)("speech.ignored", json!({ "content": transcript.text }));
        }
    }

    /// True when speech that began after `end_ms` (within the merge gap) is
    /// being heard right now — the question is not over yet.
    fn still_speaking_after(&self, end_ms: u64) -> bool {
        self.speaking.is_some_and(|(start, heard_at)| {
            start >= end_ms
                && start - end_ms <= questions::MERGE_GAP_MS
                && heard_at.elapsed() < Duration::from_millis(questions::VOICE_EXTEND_MS)
        })
    }

    fn decide(&mut self) {
        let Some(end_ms) = self.pending.as_ref().map(|p| p.end_ms) else {
            return;
        };
        // A transcript arrives ~0.5 s after the pause that produced it. If the
        // interviewer is already talking again by then, this was a breath,
        // not the end: wait for the rest rather than answer half. Costs
        // nothing when they really have stopped.
        let readiness = match questions::readiness(&self.pending.as_ref().unwrap().text) {
            Readiness::AnswerNow if self.still_speaking_after(end_ms) => {
                Readiness::Hold(questions::VOICE_EXTEND_MS)
            }
            other => other,
        };
        let Some(pending) = self.pending.as_mut() else {
            return;
        };
        match readiness {
            Readiness::AnswerNow => self.answer_pending(),
            Readiness::Hold(ms) => {
                let now = Instant::now();
                let cap = pending
                    .hold
                    .map(|(_, cap)| cap)
                    .unwrap_or(now + Duration::from_millis(questions::MAX_FRAGMENT_HOLD_MS));
                pending.hold = Some(((now + Duration::from_millis(ms)).min(cap), cap));
                (self.sink)("question.partial", json!({ "content": pending.text }));
            }
        }
    }

    fn answer_pending(&mut self) {
        let Some(pending) = self.pending.as_mut() else {
            return;
        };
        pending.hold = None;
        let generation = self.next_generation;
        self.next_generation += 1;
        pending.generation = Some(generation);
        let replaces = pending.replaces.take();
        (self.sink)(
            "question.finalized",
            json!({
                "content": pending.text,
                "generation": generation,
                "replaces": replaces,
                "stt_ms": pending.stt_ms
            }),
        );

        let history: Vec<(String, String)> = self
            .history
            .iter()
            .map(|(_, q, a)| (q.clone(), a.clone()))
            .collect();
        let question = pending.text.clone();
        let timing = Timing {
            queued_at: pending.queued_at,
            detection_delay_ms: pending.detection_delay_ms,
            stt_ms: pending.stt_ms,
        };
        let sink = self.sink.clone();
        let client = self.client.clone();
        let chat = self.chat.clone();
        let context = self.context.clone();
        let done_tx = self.done_tx.clone();
        // Replacing the task aborts any answer still streaming.
        self.task = Some(AbortOnDrop::spawn(async move {
            let prompt = build_prompt(&context, &history, &question);
            let answer =
                match stream_answer(&sink, &client, &chat, &prompt, generation, timing).await {
                    Ok(answer) => Some(answer),
                    Err(error) => {
                        sink(
                            "warning",
                            json!({ "message": error.to_string(), "generation": generation }),
                        );
                        None
                    }
                };
            let _ = done_tx.send(AnswerDone {
                generation,
                question,
                answer,
            });
        }));
    }

    fn on_done(&mut self, done: AnswerDone) {
        let current = self.pending.as_ref().and_then(|p| p.generation);
        if current != Some(done.generation) {
            // Superseded after it finished; it was never the answer shown.
            return;
        }
        self.task = None;
        if let Some(answer) = done.answer {
            self.history.push((done.generation, done.question, answer));
            if self.history.len() > MAX_HISTORY_TURNS {
                self.history.remove(0);
            }
        }
    }
}

#[derive(Clone, Copy)]
struct Timing {
    queued_at: Instant,
    detection_delay_ms: u64,
    stt_ms: u64,
}

impl Timing {
    /// Milliseconds since the interviewer stopped talking.
    fn since_speech_ended(&self) -> u64 {
        self.detection_delay_ms + self.queued_at.elapsed().as_millis() as u64
    }
}

async fn stream_answer(
    sink: &EventSink,
    client: &reqwest::Client,
    chat: &ChatEngine,
    prompt: &str,
    generation: u64,
    timing: Timing,
) -> Result<String> {
    let generation_started = Instant::now();
    let response = chat.open(client, prompt).await?;

    let mut answer = String::new();
    let mut first_token_ms = None;
    let mut push = |delta: &str, answer: &mut String| {
        answer.push_str(delta);
        let first = *first_token_ms.get_or_insert_with(|| timing.since_speech_ended());
        sink(
            "answer.delta",
            json!({
                "generation": generation,
                "delta": delta,
                "content": answer,
                "first_response_ms": first,
                "stt_ms": timing.stt_ms
            }),
        );
    };

    if chat.provider == Provider::Bedrock {
        // One non-streaming Converse response (see providers::build_chat_request).
        let value: Value = response.json().await?;
        let text = value["output"]["message"]["content"][0]["text"]
            .as_str()
            .unwrap_or_default()
            .to_string();
        if !text.is_empty() {
            push(&text, &mut answer);
        }
    } else {
        let mut stream = response.bytes_stream();
        // Bytes, not text, until a whole line is in: a network chunk can end
        // in the middle of a multi-byte character (’, é), and decoding each
        // chunk on its own turns that character into "�" on screen.
        let mut pending: Vec<u8> = Vec::new();
        'stream: while let Some(chunk) = stream.next().await {
            pending.extend_from_slice(&chunk?);
            while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                let line = String::from_utf8_lossy(&pending[..newline])
                    .trim()
                    .to_string();
                pending.drain(..=newline);
                let Some(data) = line.strip_prefix("data:").map(str::trim) else {
                    continue;
                };
                if data == "[DONE]" {
                    break 'stream;
                }
                let Ok(value) = serde_json::from_str::<Value>(data) else {
                    continue;
                };
                if let Some(delta) = providers::extract_delta_text(chat.provider, &value) {
                    if !delta.is_empty() {
                        push(&delta, &mut answer);
                    }
                }
            }
        }
    }

    let cleaned = strip_leaked_speaker_label(answer.trim());
    if cleaned.is_empty() {
        return Err(anyhow!(
            "{} returned an empty answer with {}. Try another answer model in Advanced settings.",
            chat.provider.label(),
            chat.model()
        ));
    }
    let (direction, key_points) = split_into_direction_and_points(cleaned);
    sink(
        "answer.complete",
        json!({
            "generation": generation,
            "content": {
                "answer_direction": direction,
                "key_points": key_points,
                "structure": ""
            },
            "provider": chat.provider.label(),
            "model": chat.model(),
            "first_response_ms": first_token_ms,
            "stt_ms": timing.stt_ms,
            "generation_ms": generation_started.elapsed().as_millis() as u64,
            "detection_ms": timing.detection_delay_ms,
            "total_ms": timing.since_speech_ended()
        }),
    );
    // History keeps the label-stripped text with its bullets: the
    // anti-repetition instruction needs to see the structure used last time.
    Ok(cleaned.to_string())
}

/// True exactly when accumulating onto `before` crosses a multiple of
/// `interval` — fire once per interval regardless of chunk size.
fn crosses_interval(before: u64, after: u64, interval: u64) -> bool {
    after / interval > before / interval
}

/// Defense in depth for the prompt's "never write a speaker label" rule:
/// verified live that a model still opened with a literal "You:" despite
/// it. Only known leak patterns, never a generic "word:" prefix — a
/// definitional answer may legitimately start "CI/CD: …".
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

/// A short lead-in plus bullet points, per the prompt's two formats. A
/// narrative answer has no bullets and returns no points — valid, not a
/// parse failure. Markers: "- ", "• ", "* " followed by content.
fn split_into_direction_and_points(answer: &str) -> (String, Vec<String>) {
    let mut direction_lines = Vec::new();
    let mut points = Vec::new();
    for line in answer.lines() {
        let trimmed = line.trim();
        match bullet_content(trimmed) {
            Some(point) if !point.is_empty() => points.push(point.to_string()),
            Some(_) => {}
            None if !trimmed.is_empty() => direction_lines.push(trimmed.to_string()),
            None => {}
        }
    }
    (direction_lines.join(" "), points)
}

/// Requires the marker be followed by whitespace or be the whole line, so
/// "-5" and "*emphasis*" stay ordinary text.
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
    // Bracketed structural labels, not "Interviewer:"/"You:" — verified live
    // that those made a model continue the dialogue pattern.
    history
        .iter()
        .map(|(q, a)| format!("[Previously asked] {q}\n[Previous answer] {a}"))
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn pcm_duration_ms(pcm: &[u8]) -> u64 {
    (pcm.len() as u64 / 2) * 1000 / TARGET_RATE as u64
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

/// The mean of the quietest quarter of the window, scaled above the floor
/// and clamped: speech has pauses, and those low samples are the true floor.
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

pub fn wav_bytes(pcm: &[u8]) -> Vec<u8> {
    let mut wav = Vec::with_capacity(44 + pcm.len());
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16_u32.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&TARGET_RATE.to_le_bytes());
    wav.extend_from_slice(&(TARGET_RATE * 2).to_le_bytes());
    wav.extend_from_slice(&2_u16.to_le_bytes());
    wav.extend_from_slice(&16_u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&(pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(pcm);
    wav
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
    fn wav_header_describes_pcm_payload() {
        let wav = wav_bytes(&[1, 2, 3, 4]);
        assert_eq!(&wav[..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[40..44], &4_u32.to_le_bytes());
        assert_eq!(&wav[44..], &[1, 2, 3, 4]);
    }

    #[test]
    fn a_plain_narrative_answer_has_no_points() {
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
        let (direction, points) = split_into_direction_and_points("- First point\n- Second point");
        assert_eq!(direction, "");
        assert_eq!(points, vec!["First point", "Second point"]);
    }

    #[test]
    fn every_common_bullet_marker_is_recognized() {
        let (_, points) = split_into_direction_and_points("- dash\n• dot\n* star");
        assert_eq!(points, vec!["dash", "dot", "star"]);
    }

    #[test]
    fn an_empty_bullet_line_is_dropped_not_kept_as_a_blank_point() {
        let (direction, points) = split_into_direction_and_points("Intro line\n- \n- real point");
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
        assert_eq!(strip_leaked_speaker_label("YOU:hello"), "hello");
    }

    #[test]
    fn a_definitional_answer_starting_with_a_term_and_colon_is_left_alone() {
        let answer = "CI/CD: it automates getting code from commit to production.";
        assert_eq!(strip_leaked_speaker_label(answer), answer);
    }

    #[test]
    fn a_dash_not_followed_by_whitespace_is_not_mistaken_for_a_bullet() {
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
        let quiet_noise_floor: Vec<f32> = vec![0.0008, 0.0009, 0.0007, 0.0010, 0.0006];
        let threshold = calibrate_threshold(&quiet_noise_floor);
        assert!(threshold < FALLBACK_VOICE_RMS_THRESHOLD, "got {threshold}");
        assert!(threshold >= MIN_VOICE_RMS_THRESHOLD);
    }

    #[test]
    fn calibration_ignores_loud_samples_mixed_into_the_window() {
        let mixed: Vec<f32> = vec![0.001, 0.0009, 0.15, 0.18, 0.001, 0.0011, 0.2, 0.001];
        assert!(calibrate_threshold(&mixed) < 0.01);
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
    fn silence_alert_fires_once_per_interval_regardless_of_chunk_size() {
        assert!(crosses_interval(0, 12_000, 12_000));
        assert!(crosses_interval(11_999, 12_001, 12_000));
        assert!(!crosses_interval(100, 200, 12_000));
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

    /// PCM16 samples of `text` spoken by macOS `say`, at the capture rate.
    #[cfg(target_os = "macos")]
    fn speech(text: &str) -> Vec<u8> {
        let path = std::env::temp_dir().join(format!(
            "verity-live-{}-{}.wav",
            std::process::id(),
            text.len()
        ));
        let status = std::process::Command::new("say")
            .args(["-o"])
            .arg(&path)
            .args(["--file-format=WAVE", "--data-format=LEI16@16000", text])
            .status()
            .expect("macOS `say` is needed to synthesize the interviewer");
        assert!(status.success());
        let wav = std::fs::read(&path).unwrap();
        let _ = std::fs::remove_file(&path);
        // Walk the RIFF chunks: `say` writes an FLLR padding chunk before data.
        let mut offset = 12;
        while offset + 8 <= wav.len() {
            let id = &wav[offset..offset + 4];
            let size = u32::from_le_bytes(wav[offset + 4..offset + 8].try_into().unwrap()) as usize;
            if id == b"data" {
                return wav[offset + 8..(offset + 8 + size).min(wav.len())].to_vec();
            }
            offset += 8 + size + (size & 1);
        }
        panic!("no data chunk in the synthesized speech");
    }

    fn silence(ms: u64) -> Vec<u8> {
        vec![0; (TARGET_RATE as u64 * 2 * ms / 1000) as usize]
    }

    /// Every provider's real endpoint, with a fake key: each must answer
    /// with an auth rejection, never a 404 — proving the URL, auth header and
    /// body shape reach the provider even where no real key is available.
    /// `cargo test live_every -- --ignored --nocapture`
    #[tokio::test]
    #[ignore = "calls every provider's real API with a fake key"]
    async fn live_every_provider_endpoint_rejects_a_fake_key_as_auth_not_404() {
        for provider in Provider::ALL {
            let error = test_provider(provider, vec!["fake-key-123".into()], "", TestKind::Answers)
                .await
                .unwrap_err()
                .to_string();
            eprintln!("answers  {:<16} {error}", provider.label());
            assert!(
                !error.contains("404"),
                "{provider:?} endpoint not found: {error}"
            );
            assert!(
                ["400", "401", "403"]
                    .iter()
                    .any(|code| error.contains(code)),
                "{provider:?}: {error}"
            );
        }
        for provider in [Provider::Groq, Provider::OpenAi, Provider::Gemini] {
            let error = test_provider(
                provider,
                vec!["fake-key-123".into()],
                "",
                TestKind::Transcription,
            )
            .await
            .unwrap_err()
            .to_string();
            eprintln!("transcribe {:<14} {error}", provider.label());
            assert!(
                !error.contains("404"),
                "{provider:?} endpoint not found: {error}"
            );
        }
    }

    /// The setup screen's Test button, against the real Groq API.
    #[tokio::test]
    #[ignore = "calls the real Groq API; needs VERITY_TEST_GROQ_KEY"]
    async fn live_groq_passes_both_setup_tests() {
        let Ok(key) = std::env::var("VERITY_TEST_GROQ_KEY") else {
            return;
        };
        for kind in [TestKind::Answers, TestKind::Transcription] {
            let result = test_provider(Provider::Groq, vec![key.clone()], "", kind)
                .await
                .unwrap();
            eprintln!("{kind:?}: {} in {} ms", result.detail, result.latency_ms);
        }
    }

    /// The real pipeline against the real Groq API, fed synthesized speech in
    /// real time: a question split by a mid-sentence pause, a question asked
    /// in one go, then a backchannel. Run with:
    /// `VERITY_TEST_GROQ_KEY=gsk_… cargo test live_ -- --ignored --nocapture`
    #[cfg(target_os = "macos")]
    #[tokio::test]
    #[ignore = "calls the real Groq API; needs VERITY_TEST_GROQ_KEY"]
    async fn live_pipeline_waits_for_a_split_question_and_answers_it_whole() {
        let Ok(key) = std::env::var("VERITY_TEST_GROQ_KEY") else {
            eprintln!("VERITY_TEST_GROQ_KEY not set; skipping");
            return;
        };
        let started = Instant::now();
        let events: Arc<std::sync::Mutex<Vec<(u64, String, Value)>>> = Default::default();
        let log = events.clone();
        let sink: EventSink = Arc::new(move |kind: &str, payload: Value| {
            if kind != "audio.level" {
                log.lock().unwrap().push((
                    started.elapsed().as_millis() as u64,
                    kind.to_string(),
                    payload,
                ));
            }
        });

        let timeline = [
            silence(1_600),
            speech("Tell me about a time when you"),
            silence(1_200), // the mid-sentence pause
            speech("had to push back on your manager."),
            silence(6_000),
            speech("Okay, so what is the difference between a process and a thread?"),
            silence(900),
            speech("Mm-hmm."),
            silence(6_000),
        ];
        let audio: Vec<u8> = timeline.concat();

        let (audio_tx, audio_rx) = mpsc::channel(64);
        let (_stop_tx, stop_rx) = mpsc::channel(1);
        let settings = settings(&[(Provider::Groq, &key)], Provider::Groq, None);
        let session = tokio::spawn(run_session(sink, settings, audio_rx, stop_rx, None));
        // 20 ms chunks at real-time pace, like the capture callback.
        let chunk = (TARGET_RATE as usize * 2) / 50;
        let mut next = tokio::time::Instant::now();
        for piece in audio.chunks(chunk) {
            audio_tx
                .send(CaptureMessage::Pcm(piece.to_vec()))
                .await
                .unwrap();
            next += Duration::from_millis(20);
            tokio::time::sleep_until(next).await;
        }
        drop(audio_tx);
        session.await.unwrap().unwrap();

        let events = events.lock().unwrap().clone();
        for (at, kind, payload) in &events {
            if kind == "answer.delta" {
                continue;
            }
            eprintln!("{at:>6} ms  {kind:<20} {payload}");
        }
        let finalized: Vec<&Value> = events
            .iter()
            .filter(|(_, kind, _)| kind == "question.finalized")
            .map(|(_, _, payload)| payload)
            .collect();
        let completes: Vec<&Value> = events
            .iter()
            .filter(|(_, kind, _)| kind == "answer.complete")
            .map(|(_, _, payload)| payload)
            .collect();
        let content = |v: &Value| v["content"].as_str().unwrap_or_default().to_lowercase();

        // The split question was answered once, as a whole, never as its half.
        let whole = finalized
            .iter()
            .find(|q| content(q).contains("push back"))
            .expect("the split question was never finalized");
        assert!(
            content(whole).contains("time when"),
            "halves not merged: {whole}"
        );
        assert!(
            !finalized
                .iter()
                .any(|q| content(q).contains("time when") && !content(q).contains("push back")),
            "the first half was answered on its own"
        );
        let whole_answer = completes
            .iter()
            .find(|c| c["generation"] == whole["generation"])
            .expect("no answer for the whole question");

        // The one-go question was answered, and the backchannel did not
        // replace or re-trigger it.
        let direct = finalized
            .iter()
            .find(|q| content(q).contains("thread"))
            .expect("the direct question was never finalized");
        assert!(
            !content(direct).contains("hmm"),
            "backchannel merged: {direct}"
        );
        let direct_answer = completes
            .iter()
            .find(|c| c["generation"] == direct["generation"])
            .expect("no answer for the direct question");
        assert_eq!(
            finalized.len(),
            2,
            "unexpected extra questions: {finalized:?}"
        );

        for (label, answer) in [("split", whole_answer), ("direct", direct_answer)] {
            eprintln!(
                "{label}: transcription {} ms, first word {} ms after the interviewer stopped, complete {} ms ({} {})",
                answer["stt_ms"], answer["first_response_ms"], answer["total_ms"], answer["provider"], answer["model"]
            );
        }
    }

    type Events = Arc<std::sync::Mutex<Vec<(String, Value)>>>;

    /// An assembler whose answers fail instantly without touching the
    /// network (no keys), so only its decisions are observed.
    fn assembler() -> (Assembler, Events) {
        let events: Events = Default::default();
        let log = events.clone();
        let sink: EventSink = Arc::new(move |kind: &str, payload: Value| {
            log.lock().unwrap().push((kind.to_string(), payload))
        });
        let chat = ChatEngine::new(Provider::Groq, "", Vec::new());
        let context = Arc::new(AnswerContext::from(&settings(&[], Provider::Groq, None)));
        (
            Assembler::new(sink, reqwest::Client::new(), chat, context),
            events,
        )
    }

    fn heard(text: &str, start_ms: u64, end_ms: u64) -> Transcript {
        Transcript {
            text: text.to_string(),
            start_ms,
            end_ms,
            queued_at: Instant::now(),
            detection_delay_ms: 360,
            stt_ms: 200,
        }
    }

    fn of_kind(events: &Events, kind: &str) -> Vec<Value> {
        events
            .lock()
            .unwrap()
            .iter()
            .filter(|(k, _)| k == kind)
            .map(|(_, v)| v.clone())
            .collect()
    }

    #[tokio::test]
    async fn a_question_cut_mid_sentence_waits_and_is_answered_whole() {
        let (mut a, events) = assembler();
        a.on_transcript(heard("Tell me about a time when you.", 0, 1_800));
        assert!(of_kind(&events, "question.finalized").is_empty());
        assert_eq!(of_kind(&events, "question.partial").len(), 1);
        a.on_transcript(heard("Had to push back on your manager.", 2_900, 4_700));
        let finalized = of_kind(&events, "question.finalized");
        assert_eq!(finalized.len(), 1);
        assert_eq!(
            finalized[0]["content"],
            "Tell me about a time when you had to push back on your manager."
        );
        assert_eq!(finalized[0]["replaces"], Value::Null);
    }

    #[tokio::test]
    async fn a_finished_question_is_answered_at_once_and_rewritten_if_extended() {
        let (mut a, events) = assembler();
        a.on_transcript(heard("What's your name?", 0, 1_000));
        let first = of_kind(&events, "question.finalized");
        assert_eq!(first.len(), 1, "a finished question must not wait");
        a.on_transcript(heard("And where did you study?", 1_600, 2_800));
        let finalized = of_kind(&events, "question.finalized");
        assert_eq!(finalized.len(), 2);
        assert_eq!(
            finalized[1]["content"],
            "What's your name? And where did you study?"
        );
        assert_eq!(finalized[1]["replaces"], first[0]["generation"]);
    }

    #[tokio::test]
    async fn a_backchannel_after_a_question_changes_nothing() {
        let (mut a, events) = assembler();
        a.on_transcript(heard("What is a process?", 0, 1_000));
        a.on_transcript(heard("Mm-hmm, take your time.", 1_500, 2_300));
        assert_eq!(of_kind(&events, "question.finalized").len(), 1);
        assert_eq!(of_kind(&events, "speech.ignored").len(), 1);
    }

    #[tokio::test]
    async fn speech_after_the_merge_gap_is_a_new_question() {
        let (mut a, events) = assembler();
        a.on_transcript(heard("What is a process?", 0, 1_000));
        a.on_transcript(heard(
            "How do you handle conflict on a team?",
            9_000,
            11_000,
        ));
        let finalized = of_kind(&events, "question.finalized");
        assert_eq!(finalized.len(), 2);
        assert_eq!(
            finalized[1]["content"],
            "How do you handle conflict on a team?"
        );
        assert_eq!(finalized[1]["replaces"], Value::Null);
    }

    #[tokio::test]
    async fn a_question_is_held_while_the_interviewer_is_already_talking_again() {
        let (mut a, events) = assembler();
        // Voice from the next utterance arrives before this transcript does.
        a.on_voice(1_400);
        a.on_transcript(heard("Okay, so what's going on?", 0, 1_000));
        assert!(of_kind(&events, "question.finalized").is_empty());
        a.on_transcript(heard(
            "What is the difference between a process and a thread?",
            1_400,
            4_000,
        ));
        let finalized = of_kind(&events, "question.finalized");
        assert_eq!(finalized.len(), 1);
        assert!(finalized[0]["content"]
            .as_str()
            .unwrap()
            .ends_with("a process and a thread?"));
    }

    #[tokio::test]
    async fn context_said_just_before_a_question_is_kept_with_it() {
        let (mut a, events) = assembler();
        a.on_transcript(heard("We use Kafka for event sourcing here.", 0, 2_000));
        a.on_transcript(heard(
            "How would you guarantee exactly-once processing?",
            2_800,
            5_000,
        ));
        let finalized = of_kind(&events, "question.finalized");
        assert_eq!(
            finalized[0]["content"],
            "We use Kafka for event sourcing here. How would you guarantee exactly-once processing?"
        );
        // Context from long before is not.
        a.on_transcript(heard("Our office is in Berlin.", 20_000, 21_000));
        a.on_transcript(heard("Why do you want to work here?", 40_000, 41_000));
        assert_eq!(
            of_kind(&events, "question.finalized")[1]["content"],
            "Why do you want to work here?"
        );
    }

    #[test]
    fn kept_context_is_bounded_at_a_word_boundary() {
        let long = "word ".repeat(300);
        let tail = keep_tail(long.trim(), 50);
        assert!(tail.chars().count() <= 50);
        assert!(tail.starts_with("word"));
        assert_eq!(keep_tail("short", 50), "short");
    }

    fn settings(keys: &[(Provider, &str)], chat: Provider, stt: Option<Provider>) -> Settings {
        Settings {
            keys: keys
                .iter()
                .map(|(p, k)| (*p, vec![k.to_string()]))
                .collect(),
            chat_provider: chat,
            chat_model: String::new(),
            stt_provider: stt,
            role_title: String::new(),
            company_name: String::new(),
            resume_text: String::new(),
            job_description: String::new(),
            language: "en".to_string(),
        }
    }

    #[test]
    fn any_answer_provider_pairs_with_any_transcription_provider() {
        for chat in Provider::ALL {
            for stt in [Provider::Groq, Provider::OpenAi, Provider::Gemini] {
                let s = settings(&[(chat, "chat-key"), (stt, "stt-key")], chat, Some(stt));
                let (stt_engine, chat_engine) = s.engines().unwrap();
                assert_eq!(stt_engine.provider, stt);
                assert_eq!(chat_engine.provider, chat);
            }
        }
    }

    #[test]
    fn a_session_without_answer_keys_or_a_way_to_hear_is_refused_up_front() {
        let no_chat_keys = settings(&[(Provider::Groq, "g")], Provider::Anthropic, None);
        assert!(no_chat_keys.engines().is_err());
        let cannot_hear = settings(&[(Provider::Anthropic, "a")], Provider::Anthropic, None);
        let error = cannot_hear.engines().err().unwrap().to_string();
        assert!(error.contains("Groq, OpenAI or Gemini"), "{error}");
    }

    #[test]
    fn one_groq_key_still_runs_everything_like_before() {
        let s = settings(&[(Provider::Groq, "gsk")], Provider::Groq, None);
        let (stt, chat) = s.engines().unwrap();
        assert_eq!(
            (stt.provider, chat.provider),
            (Provider::Groq, Provider::Groq)
        );
        assert_eq!(chat.model(), "qwen/qwen3.8-27b");
        assert_eq!(stt.model(), "whisper-large-v3-turbo");
    }
}
