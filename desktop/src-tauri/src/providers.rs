//! Every AI provider the app can talk to, for both jobs it needs done:
//! turning interview audio into text (transcription) and turning a question
//! into a spoken answer (chat).
//!
//! Only three of the five can transcribe. Anthropic and Bedrock accept text
//! only, so when one of them writes the answers, the audio is transcribed by
//! a provider that can hear (Groq, OpenAI or Gemini) and just the transcript
//! is sent on. Which provider does which job is independent: any answer
//! provider works with any transcription provider, each with its own keys.
//!
//! Model rosters move under a shipped app — a default that worked at release
//! can be retired months later (gemini-2.0-flash was, on 2026-06-01, while it
//! was this app's Gemini default). Two mechanisms keep a released build
//! working through that instead of failing mid-interview:
//!
//! * default models are a short ordered list, not one name: a retired first
//!   choice falls through to the next, and the working one is remembered for
//!   the rest of the session;
//! * optional request settings (temperature, reasoning effort, thinking) are
//!   dropped or stepped down when a provider rejects them with a 400, so a
//!   newer model that refuses a setting still answers.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Result};
use base64::Engine as _;
use reqwest::multipart;
use serde_json::{json, Value};

/// Output cap for one answer. Thinking/reasoning models get headroom on top
/// (see `Tuning::initial`) because their hidden thinking tokens count against
/// the same limit — without it a thinking model can spend the whole budget
/// thinking and return an empty answer.
pub const MAX_ANSWER_TOKENS: u32 = 220;
const THINKING_HEADROOM_TOKENS: u32 = 2_048;

/// Whole-request timeout for one chat call, including streaming the body.
/// Answers are a few hundred tokens, so this is only hit by a stalled
/// connection, never by a normal answer.
pub const CHAT_TIMEOUT: Duration = Duration::from_secs(20);
pub const STT_TIMEOUT: Duration = Duration::from_secs(12);

/// Bedrock's region lives in the endpoint URL, not in the key. The model
/// field accepts `region/model` (e.g. `eu-west-1/amazon.nova-lite-v1:0`) for a
/// key from another region; a bare model id uses this.
const DEFAULT_BEDROCK_REGION: &str = "us-east-1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Provider {
    Groq,
    OpenAi,
    Anthropic,
    Gemini,
    Bedrock,
}

impl Provider {
    pub const ALL: [Provider; 5] = [
        Provider::Groq,
        Provider::OpenAi,
        Provider::Anthropic,
        Provider::Gemini,
        Provider::Bedrock,
    ];

    /// Unknown or empty input falls back to Groq rather than to a paid
    /// provider the user never picked.
    pub fn parse(value: &str) -> Self {
        Self::parse_strict(value).unwrap_or(Self::Groq)
    }

    pub fn parse_strict(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "groq" => Some(Self::Groq),
            "openai" => Some(Self::OpenAi),
            "anthropic" | "claude" => Some(Self::Anthropic),
            "gemini" | "google" => Some(Self::Gemini),
            "bedrock" | "amazon" | "amazon-bedrock" | "aws" => Some(Self::Bedrock),
            _ => None,
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

    pub fn label(self) -> &'static str {
        match self {
            Self::Groq => "Groq",
            Self::OpenAi => "OpenAI",
            Self::Anthropic => "Anthropic",
            Self::Gemini => "Gemini",
            Self::Bedrock => "Amazon Bedrock",
        }
    }

    /// Whether this provider can turn audio into text with an API key alone.
    pub fn can_transcribe(self) -> bool {
        matches!(self, Self::Groq | Self::OpenAi | Self::Gemini)
    }

    /// Chat models to try, in order, when the user has not named one.
    /// First entries are the fastest known-good choice per provider; later
    /// entries are successors that take over if the first is retired.
    /// Checked against each provider's published model and deprecation
    /// pages in September 2026 (none of these has an announced shutdown
    /// before 2027 except where a successor follows it).
    pub fn default_chat_models(self) -> &'static [&'static str] {
        match self {
            // Measured live 2026-09 with this app's exact prompt, 8 runs
            // each: qwen3.8-27b with reasoning off streams its first word in
            // ~140 ms (gpt-oss-20b: ~460 ms, since it reasons silently and
            // then sends the answer at once) with no format violations, and
            // did not invent metrics where gpt-oss-20b did.
            Self::Groq => &[
                "qwen/qwen3.8-27b",
                "openai/gpt-oss-20b",
                "openai/gpt-oss-120b",
            ],
            Self::OpenAi => &["gpt-4o-mini", "gpt-6-luna"],
            Self::Anthropic => &["claude-haiku-4-5-20251001", "claude-haiku-4-5"],
            // gemini-2.0-flash, the previous default, was shut down 2026-06-01.
            Self::Gemini => &["gemini-2.5-flash", "gemini-3.8-flash"],
            // Needs per-model access enabled once in the AWS console.
            Self::Bedrock => &["amazon.nova-lite-v1:0"],
        }
    }

    /// Transcription models to try, in order. Empty for providers that
    /// cannot transcribe.
    pub fn default_stt_models(self) -> &'static [&'static str] {
        match self {
            Self::Groq => &["whisper-large-v3-turbo", "whisper-large-v3"],
            // gpt-4o-mini-transcribe is deprecated (shutdown 2027-02-26) but
            // is the fallback while gpt-transcribe is the recommended model.
            Self::OpenAi => &["gpt-transcribe", "gpt-4o-mini-transcribe"],
            Self::Gemini => &["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"],
            Self::Anthropic | Self::Bedrock => &[],
        }
    }

    /// A cheap endpoint that proves a key and the network path work, with no
    /// generation cost. Also used to warm connections before a question.
    pub fn models_request(
        self,
        client: &reqwest::Client,
        api_key: &str,
    ) -> reqwest::RequestBuilder {
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
            Self::Bedrock => client
                .get(format!(
                    "https://bedrock.{DEFAULT_BEDROCK_REGION}.amazonaws.com/foundation-models"
                ))
                .bearer_auth(api_key),
        }
    }
}

/// The model list to try for a job: the user's explicit choice alone, or the
/// provider defaults when the field is empty or still holds the first
/// default (so a retired default can fall through to its successor).
pub fn model_candidates(configured: &str, defaults: &[&str]) -> Vec<String> {
    let configured = configured.trim();
    if configured.is_empty() || defaults.first() == Some(&configured) {
        defaults.iter().map(|m| m.to_string()).collect()
    } else {
        vec![configured.to_string()]
    }
}

/// Keys for one provider, tried in order with failover. The last key that
/// worked is tried first next time, so a rate-limited first key is not
/// retried on every question.
#[derive(Debug)]
pub struct KeyRing {
    keys: Vec<String>,
    start: AtomicUsize,
}

impl KeyRing {
    pub fn new(keys: Vec<String>) -> Self {
        Self {
            keys,
            start: AtomicUsize::new(0),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    /// (index, key) pairs starting from the last key that worked.
    pub fn order(&self) -> Vec<(usize, &str)> {
        let count = self.keys.len();
        let start = self.start.load(Ordering::Relaxed) % count.max(1);
        (0..count)
            .map(|offset| {
                let index = (start + offset) % count;
                (index, self.keys[index].as_str())
            })
            .collect()
    }

    pub fn mark_good(&self, index: usize) {
        self.start.store(index, Ordering::Relaxed);
    }

    /// Index of the key that last worked (0 before any has).
    pub fn current(&self) -> usize {
        self.start.load(Ordering::Relaxed)
    }
}

/// Keys per provider, as the user entered them.
pub type ProviderKeys = BTreeMap<Provider, Vec<String>>;

/// Optional request settings that some models reject. Starts from the best
/// known setting for the model, and `adapt` removes or steps down whichever
/// one a provider's 400 names.
#[derive(Debug, Clone, PartialEq)]
pub struct Tuning {
    pub temperature: Option<f32>,
    /// Remaining reasoning-effort values to try, first is current. Empty
    /// means the parameter is not sent at all.
    pub reasoning_effort: Vec<&'static str>,
    /// Groq's `include_reasoning: false` extension (gpt-oss only).
    pub hide_reasoning: bool,
    /// Gemini `generationConfig.thinkingConfig`.
    pub thinking: Option<Value>,
    pub max_tokens: u32,
}

impl Tuning {
    pub fn initial(provider: Provider, model: &str) -> Self {
        let model_lower = model.to_ascii_lowercase();
        let mut tuning = Tuning {
            temperature: Some(0.3),
            reasoning_effort: Vec::new(),
            hide_reasoning: false,
            thinking: None,
            max_tokens: MAX_ANSWER_TOKENS,
        };
        match provider {
            Provider::Groq => {
                if model_lower.starts_with("openai/gpt-oss") {
                    tuning.reasoning_effort = vec!["low"];
                    tuning.hide_reasoning = true;
                } else if model_lower.starts_with("qwen/") {
                    // Qwen3 thinks by default and would stream its <think>
                    // block into the answer; "none" turns that off.
                    tuning.reasoning_effort = vec!["none"];
                }
            }
            Provider::OpenAi => {
                if is_openai_reasoning_model(&model_lower) {
                    // Reasoning models reject a non-default temperature while
                    // reasoning, and count reasoning tokens against the cap.
                    // Newest accept "none"; gpt-5 accepted "minimal"; all
                    // accept "low" — try the fastest first.
                    tuning.temperature = None;
                    tuning.reasoning_effort = vec!["none", "minimal", "low"];
                    tuning.max_tokens = MAX_ANSWER_TOKENS + THINKING_HEADROOM_TOKENS;
                }
            }
            Provider::Gemini => {
                tuning.thinking = gemini_thinking(&model_lower);
                if tuning
                    .thinking
                    .as_ref()
                    .is_some_and(|t| t.get("thinkingBudget") != Some(&json!(0)))
                {
                    tuning.max_tokens = MAX_ANSWER_TOKENS + THINKING_HEADROOM_TOKENS;
                }
            }
            Provider::Anthropic | Provider::Bedrock => {}
        }
        tuning
    }

    /// Adjust after a provider rejected a request with `detail` as its error
    /// body. Returns true when something changed and the request is worth
    /// retrying; false when the error is not about an optional setting.
    pub fn adapt(&mut self, detail: &str) -> bool {
        let detail = detail.to_ascii_lowercase();
        if detail.contains("include_reasoning") && self.hide_reasoning {
            self.hide_reasoning = false;
            return true;
        }
        if detail.contains("reasoning") && !self.reasoning_effort.is_empty() {
            self.reasoning_effort.remove(0);
            return true;
        }
        if detail.contains("thinking") && self.thinking.is_some() {
            self.thinking = None;
            // Whatever the model thinks by default now counts against the cap.
            self.max_tokens = self
                .max_tokens
                .max(MAX_ANSWER_TOKENS + THINKING_HEADROOM_TOKENS);
            return true;
        }
        if detail.contains("temperature") && self.temperature.is_some() {
            self.temperature = None;
            return true;
        }
        false
    }
}

fn is_openai_reasoning_model(model: &str) -> bool {
    ["gpt-5", "gpt-6", "o1", "o3", "o4"]
        .iter()
        .any(|prefix| model.starts_with(prefix))
}

/// The lowest-latency thinking setting each Gemini generation accepts:
/// 2.5 Flash can switch thinking off entirely; 2.5 Pro cannot go below 128;
/// Gemini 3 uses levels instead of budgets, where Flash-Lite goes down to
/// "minimal" and the rest to "low".
fn gemini_thinking(model: &str) -> Option<Value> {
    if model.contains("gemini-2.5-flash") {
        Some(json!({ "thinkingBudget": 0 }))
    } else if model.contains("gemini-2.5-pro") {
        Some(json!({ "thinkingBudget": 128 }))
    } else if model.contains("gemini-3") {
        if model.contains("flash-lite") {
            Some(json!({ "thinkingLevel": "minimal" }))
        } else {
            Some(json!({ "thinkingLevel": "low" }))
        }
    } else {
        None
    }
}

/// `region/model` or a bare model id (default region).
pub fn bedrock_target(model: &str) -> (&str, &str) {
    match model.split_once('/') {
        Some((region, id)) if !region.is_empty() && !id.is_empty() && !region.contains('.') => {
            (region, id)
        }
        _ => (DEFAULT_BEDROCK_REGION, model),
    }
}

/// The one outbound chat request for `provider`. The prompt carries every
/// instruction as one block of text, so only transport differs by provider.
pub fn build_chat_request(
    client: &reqwest::Client,
    provider: Provider,
    api_key: &str,
    model: &str,
    prompt: &str,
    tuning: &Tuning,
) -> reqwest::RequestBuilder {
    match provider {
        Provider::Groq | Provider::OpenAi => {
            let mut body = json!({
                "model": model,
                "messages": [{ "role": "user", "content": prompt }],
                "stream": true,
                "max_completion_tokens": tuning.max_tokens
            });
            let object = body.as_object_mut().expect("object literal");
            if let Some(temperature) = tuning.temperature {
                object.insert("temperature".into(), json!(temperature));
            }
            if let Some(effort) = tuning.reasoning_effort.first() {
                object.insert("reasoning_effort".into(), json!(effort));
            }
            if tuning.hide_reasoning {
                object.insert("include_reasoning".into(), json!(false));
            }
            let url = match provider {
                Provider::Groq => "https://api.groq.com/openai/v1/chat/completions",
                _ => "https://api.openai.com/v1/chat/completions",
            };
            client.post(url).bearer_auth(api_key).json(&body)
        }
        Provider::Anthropic => {
            let mut body = json!({
                "model": model,
                "max_tokens": tuning.max_tokens,
                "stream": true,
                "messages": [{ "role": "user", "content": prompt }]
            });
            if let Some(temperature) = tuning.temperature {
                body["temperature"] = json!(temperature);
            }
            client
                .post("https://api.anthropic.com/v1/messages")
                .header("x-api-key", api_key)
                .header("anthropic-version", "2023-06-01")
                .json(&body)
        }
        Provider::Gemini => {
            let mut config = json!({ "maxOutputTokens": tuning.max_tokens });
            if let Some(temperature) = tuning.temperature {
                config["temperature"] = json!(temperature);
            }
            if let Some(thinking) = &tuning.thinking {
                config["thinkingConfig"] = thinking.clone();
            }
            let body = json!({
                "contents": [{ "role": "user", "parts": [{ "text": prompt }] }],
                "generationConfig": config
            });
            client
                .post(format!(
                    "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent"
                ))
                .query(&[("alt", "sse"), ("key", api_key)])
                .json(&body)
        }
        Provider::Bedrock => {
            // The non-streaming Converse endpoint: Bedrock streams in AWS's
            // binary eventstream framing, not text SSE, and that parser could
            // not be verified against a real success. Body shape is from
            // AWS's Converse API reference.
            let (region, model_id) = bedrock_target(model);
            let mut config = json!({ "maxTokens": tuning.max_tokens });
            if let Some(temperature) = tuning.temperature {
                config["temperature"] = json!(temperature);
            }
            let body = json!({
                "messages": [{ "role": "user", "content": [{ "text": prompt }] }],
                "inferenceConfig": config
            });
            client
                .post(format!(
                    "https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
                ))
                .bearer_auth(api_key)
                .json(&body)
        }
    }
}

/// The incremental answer text in one parsed SSE `data:` line. `None` for
/// events that carry no text (Anthropic's message_start and pings, Gemini
/// chunks holding only a thought signature) so the caller just moves on.
pub fn extract_delta_text(provider: Provider, value: &Value) -> Option<String> {
    match provider {
        Provider::Groq | Provider::OpenAi => value["choices"][0]["delta"]["content"]
            .as_str()
            .map(str::to_string),
        Provider::Anthropic => {
            if value.get("type").and_then(|t| t.as_str()) != Some("content_block_delta") {
                return None;
            }
            value["delta"]["text"].as_str().map(str::to_string)
        }
        Provider::Gemini => gemini_text(value),
        // Bedrock is answered by one non-streaming response, handled before
        // any SSE parsing; the arm exists to keep the match exhaustive.
        Provider::Bedrock => None,
    }
}

/// Text parts of a Gemini candidate, skipping thought parts. Gemini 3 can
/// put a thought signature in the first part with no text at all, so reading
/// only `parts[0].text` would drop real answer text in later parts.
fn gemini_text(value: &Value) -> Option<String> {
    let parts = value["candidates"][0]["content"]["parts"].as_array()?;
    let text: String = parts
        .iter()
        .filter(|part| part.get("thought").and_then(Value::as_bool) != Some(true))
        .filter_map(|part| part.get("text").and_then(Value::as_str))
        .collect();
    (!text.is_empty()).then_some(text)
}

/// A provider's error body reduced to its message.
pub fn concise_error(raw: &str) -> String {
    serde_json::from_str::<Value>(raw)
        .ok()
        .and_then(|value| {
            value
                .pointer("/error/message")
                .or_else(|| value.pointer("/message"))
                .or_else(|| value.pointer("/Message"))
                .and_then(|item| item.as_str())
                .map(str::to_string)
        })
        .unwrap_or_else(|| raw.chars().take(180).collect())
}

/// Auth, quota and server failures are worth trying another key for; a bad
/// request would fail identically on every key.
pub fn should_rotate_key(status: reqwest::StatusCode) -> bool {
    status == reqwest::StatusCode::UNAUTHORIZED
        || status == reqwest::StatusCode::FORBIDDEN
        || status == reqwest::StatusCode::TOO_MANY_REQUESTS
        || status.is_server_error()
}

/// A 404, or a 400 that names the model, means the model id itself is gone
/// or wrong — the cue to fall through to the next default model.
fn is_model_unavailable(status: reqwest::StatusCode, detail: &str) -> bool {
    let detail = detail.to_ascii_lowercase();
    // Auth and quota failures are about the key, whatever their text says;
    // they go to the next key, not the next model.
    if should_rotate_key(status) {
        return false;
    }
    status == reqwest::StatusCode::NOT_FOUND
        || (status.is_client_error()
            && detail.contains("model")
            && (detail.contains("not found")
                || detail.contains("does not exist")
                || detail.contains("not supported")
                || detail.contains("decommissioned")
                || detail.contains("deprecated")
                || detail.contains("invalid model")
                || detail.contains("unknown model")))
}

/// Chat for one session: provider, the model that works, its settings, and
/// its keys. Cheap to clone into each answer task; the learned model and
/// settings are shared so the second question never repeats a rejection the
/// first one already worked around.
#[derive(Clone)]
pub struct ChatEngine {
    pub provider: Provider,
    candidates: Arc<Mutex<Vec<String>>>,
    tuning: Arc<Mutex<Tuning>>,
    ring: Arc<KeyRing>,
}

impl ChatEngine {
    pub fn new(provider: Provider, configured_model: &str, keys: Vec<String>) -> Self {
        let candidates = model_candidates(configured_model, provider.default_chat_models());
        let tuning = Tuning::initial(provider, &candidates[0]);
        Self {
            provider,
            candidates: Arc::new(Mutex::new(candidates)),
            tuning: Arc::new(Mutex::new(tuning)),
            ring: Arc::new(KeyRing::new(keys)),
        }
    }

    pub fn model(&self) -> String {
        self.candidates.lock().unwrap()[0].clone()
    }

    pub fn ring(&self) -> &KeyRing {
        &self.ring
    }

    /// Opens the answer stream, recovering from rejected settings, retired
    /// models and failing keys along the way. Errors are written for the
    /// candidate to read mid-interview: what failed and what to change.
    pub async fn open(&self, client: &reqwest::Client, prompt: &str) -> Result<reqwest::Response> {
        if self.ring.is_empty() {
            return Err(anyhow!(
                "Add at least one {} API key before starting.",
                self.provider.label()
            ));
        }
        // Bounded: each adaptation removes a setting or a model, so this can
        // only loop as many times as there are things left to remove.
        for _ in 0..8 {
            let model = self.model();
            let tuning = self.tuning.lock().unwrap().clone();
            let mut last_error = format!("{} answer failed.", self.provider.label());
            let mut retry = false;
            for (index, key) in self.ring.order() {
                let sent = build_chat_request(client, self.provider, key, &model, prompt, &tuning)
                    .timeout(CHAT_TIMEOUT)
                    .send()
                    .await;
                let response = match sent {
                    Ok(response) => response,
                    Err(error) => {
                        last_error = format!(
                            "{} key {} network error: {error}",
                            self.provider.label(),
                            index + 1
                        );
                        continue;
                    }
                };
                let status = response.status();
                if status.is_success() {
                    self.ring.mark_good(index);
                    return Ok(response);
                }
                let detail = response.text().await.unwrap_or_default();
                last_error = format!(
                    "{} key {} answer failed ({status}): {}",
                    self.provider.label(),
                    index + 1,
                    concise_error(&detail)
                );
                if is_model_unavailable(status, &detail) {
                    let mut candidates = self.candidates.lock().unwrap();
                    if candidates.len() > 1 {
                        candidates.remove(0);
                        *self.tuning.lock().unwrap() =
                            Tuning::initial(self.provider, &candidates[0]);
                        retry = true;
                        break;
                    }
                    return Err(anyhow!(
                        "{last_error} — check the answer model name ({model}) in Advanced settings."
                    ));
                }
                if status.is_client_error() && !should_rotate_key(status) {
                    if self.tuning.lock().unwrap().adapt(&detail) {
                        retry = true;
                        break;
                    }
                    return Err(anyhow!(last_error));
                }
            }
            if !retry {
                return Err(anyhow!(last_error));
            }
        }
        Err(anyhow!(
            "{} kept rejecting the request settings; try a different answer model.",
            self.provider.label()
        ))
    }
}

/// Transcription for one session.
#[derive(Clone)]
pub struct SttEngine {
    pub provider: Provider,
    candidates: Arc<Mutex<Vec<String>>>,
    ring: Arc<KeyRing>,
    language: String,
}

impl SttEngine {
    pub fn new(provider: Provider, keys: Vec<String>, language: &str) -> Self {
        Self {
            provider,
            candidates: Arc::new(Mutex::new(
                provider
                    .default_stt_models()
                    .iter()
                    .map(|m| m.to_string())
                    .collect(),
            )),
            ring: Arc::new(KeyRing::new(keys)),
            language: language.trim().to_string(),
        }
    }

    pub fn model(&self) -> String {
        self.candidates
            .lock()
            .unwrap()
            .first()
            .cloned()
            .unwrap_or_default()
    }

    pub fn ring(&self) -> &KeyRing {
        &self.ring
    }

    fn request(
        &self,
        client: &reqwest::Client,
        key: &str,
        model: &str,
        wav: &[u8],
    ) -> Result<reqwest::RequestBuilder> {
        Ok(match self.provider {
            Provider::Groq | Provider::OpenAi => {
                let part = multipart::Part::bytes(wav.to_vec())
                    .file_name("interview.wav")
                    .mime_str("audio/wav")?;
                let mut form = multipart::Form::new()
                    .part("file", part)
                    .text("model", model.to_string())
                    .text("response_format", "json");
                if sends_language_field(model, &self.language) {
                    form = form.text("language", self.language.clone());
                }
                if self.provider == Provider::Groq {
                    form = form.text("temperature", "0");
                }
                let url = match self.provider {
                    Provider::Groq => "https://api.groq.com/openai/v1/audio/transcriptions",
                    _ => "https://api.openai.com/v1/audio/transcriptions",
                };
                client.post(url).bearer_auth(key).multipart(form)
            }
            Provider::Gemini => {
                let audio = base64::engine::general_purpose::STANDARD.encode(wav);
                let language = if self.language.is_empty() {
                    "the spoken language".to_string()
                } else {
                    format!("language code '{}'", self.language)
                };
                let mut config = json!({ "temperature": 0, "maxOutputTokens": 512 });
                if let Some(thinking) = gemini_thinking(&model.to_ascii_lowercase()) {
                    config["thinkingConfig"] = thinking;
                    if config["thinkingConfig"].get("thinkingBudget") != Some(&json!(0)) {
                        config["maxOutputTokens"] = json!(512 + THINKING_HEADROOM_TOKENS);
                    }
                }
                let body = json!({
                    "contents": [{ "role": "user", "parts": [
                        { "inlineData": { "mimeType": "audio/wav", "data": audio } },
                        { "text": format!(
                            "Transcribe the speech in this audio verbatim, in {language}. \
                             Output only the transcript with normal punctuation — no labels, \
                             no commentary. If there is no intelligible speech, output nothing."
                        ) }
                    ]}],
                    "generationConfig": config
                });
                client
                    .post(format!(
                        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    ))
                    .query(&[("key", key)])
                    .json(&body)
            }
            Provider::Anthropic | Provider::Bedrock => {
                return Err(anyhow!(
                    "{} cannot transcribe audio; pick Groq, OpenAI or Gemini for transcription.",
                    self.provider.label()
                ))
            }
        })
    }

    pub async fn transcribe(&self, client: &reqwest::Client, wav: &[u8]) -> Result<String> {
        if self.ring.is_empty() {
            return Err(anyhow!(
                "Add at least one {} API key for transcription.",
                self.provider.label()
            ));
        }
        for _ in 0..4 {
            let model = self.model();
            if model.is_empty() {
                return Err(anyhow!(
                    "{} cannot transcribe audio.",
                    self.provider.label()
                ));
            }
            let mut last_error = format!("{} transcription failed.", self.provider.label());
            let mut retry = false;
            for (index, key) in self.ring.order() {
                let sent = self
                    .request(client, key, &model, wav)?
                    .timeout(STT_TIMEOUT)
                    .send()
                    .await;
                let response = match sent {
                    Ok(response) => response,
                    Err(error) => {
                        last_error = format!(
                            "{} key {} network error: {error}",
                            self.provider.label(),
                            index + 1
                        );
                        continue;
                    }
                };
                let status = response.status();
                if status.is_success() {
                    self.ring.mark_good(index);
                    let body: Value = response.json().await?;
                    let text = match self.provider {
                        Provider::Gemini => gemini_text(&body).unwrap_or_default(),
                        _ => body["text"].as_str().unwrap_or_default().to_string(),
                    };
                    return Ok(text.trim().to_string());
                }
                let detail = response.text().await.unwrap_or_default();
                last_error = format!(
                    "{} key {} transcription failed ({status}): {}",
                    self.provider.label(),
                    index + 1,
                    concise_error(&detail)
                );
                if is_model_unavailable(status, &detail) {
                    let mut candidates = self.candidates.lock().unwrap();
                    if candidates.len() > 1 {
                        candidates.remove(0);
                        retry = true;
                        break;
                    }
                    return Err(anyhow!(last_error));
                }
                if !should_rotate_key(status) {
                    return Err(anyhow!(last_error));
                }
            }
            if !retry {
                return Err(anyhow!(last_error));
            }
        }
        Err(anyhow!("{} transcription failed.", self.provider.label()))
    }
}

/// gpt-transcribe takes a plural `languages` field and rejects the singular
/// one; it auto-detects well without either. Every other Whisper-style model
/// takes the singular field.
fn sends_language_field(model: &str, language: &str) -> bool {
    !language.is_empty() && !model.starts_with("gpt-transcribe")
}

/// Which provider transcribes: an explicit choice, or automatic — Groq
/// Whisper first when a Groq key exists (the fastest transcription here),
/// then the answer provider itself if it can hear (one key does both jobs),
/// then any other provider with keys that can.
pub fn resolve_stt_provider(
    choice: Option<Provider>,
    chat_provider: Provider,
    keys: &ProviderKeys,
) -> Result<Provider> {
    let has_keys = |p: Provider| keys.get(&p).is_some_and(|k| !k.is_empty());
    if let Some(provider) = choice {
        if !provider.can_transcribe() {
            return Err(anyhow!(
                "{} cannot transcribe audio. Choose Groq, OpenAI or Gemini for transcription.",
                provider.label()
            ));
        }
        if !has_keys(provider) {
            return Err(anyhow!(
                "Add a {} API key for transcription, or set transcription to Automatic.",
                provider.label()
            ));
        }
        return Ok(provider);
    }
    let order = [
        Provider::Groq,
        chat_provider,
        Provider::OpenAi,
        Provider::Gemini,
    ];
    order
        .into_iter()
        .find(|p| p.can_transcribe() && has_keys(*p))
        .ok_or_else(|| {
            anyhow!(
                "Transcription needs a Groq, OpenAI or Gemini API key — {} can write answers but cannot hear audio. Add one of those keys; only the transcript is sent to {}.",
                chat_provider.label(),
                chat_provider.label()
            )
        })
}

/// Warms the TLS connection to a provider so the first question after an
/// idle stretch does not pay the handshake. Failures are irrelevant here;
/// the real request reports its own.
pub async fn warm(client: &reqwest::Client, provider: Provider, key: &str) {
    if provider == Provider::Bedrock {
        // Answers go to bedrock-runtime, a different host from the models
        // endpoint, so warming the latter would not help.
        return;
    }
    let _ = provider
        .models_request(client, key)
        .timeout(Duration::from_secs(5))
        .send()
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_parse_round_trips_and_falls_back_to_groq() {
        for provider in Provider::ALL {
            assert_eq!(Provider::parse(provider.as_str()), provider);
        }
        assert_eq!(Provider::parse("Claude"), Provider::Anthropic);
        assert_eq!(Provider::parse("GOOGLE"), Provider::Gemini);
        assert_eq!(Provider::parse("AWS"), Provider::Bedrock);
        assert_eq!(Provider::parse(""), Provider::Groq);
        assert_eq!(Provider::parse("made-up"), Provider::Groq);
        assert_eq!(Provider::parse_strict("auto"), None);
    }

    #[test]
    fn only_audio_capable_providers_transcribe() {
        assert!(Provider::Groq.can_transcribe());
        assert!(Provider::OpenAi.can_transcribe());
        assert!(Provider::Gemini.can_transcribe());
        assert!(!Provider::Anthropic.can_transcribe());
        assert!(!Provider::Bedrock.can_transcribe());
        for provider in Provider::ALL {
            assert_eq!(
                provider.can_transcribe(),
                !provider.default_stt_models().is_empty(),
                "{provider:?}"
            );
        }
    }

    #[test]
    fn every_provider_has_chat_defaults_and_none_is_a_retired_model() {
        for provider in Provider::ALL {
            let models = provider.default_chat_models();
            assert!(!models.is_empty(), "{provider:?}");
            // Shut down 2026-06-01 while it was this app's Gemini default.
            assert!(!models.contains(&"gemini-2.0-flash"));
        }
    }

    #[test]
    fn an_explicit_model_is_used_alone_but_the_default_keeps_its_fallbacks() {
        let defaults = ["a", "b"];
        assert_eq!(model_candidates("", &defaults), vec!["a", "b"]);
        assert_eq!(model_candidates(" a ", &defaults), vec!["a", "b"]);
        assert_eq!(model_candidates("custom", &defaults), vec!["custom"]);
    }

    #[test]
    fn the_key_ring_starts_from_the_last_key_that_worked() {
        let ring = KeyRing::new(vec!["one".into(), "two".into(), "three".into()]);
        assert_eq!(ring.order()[0], (0, "one"));
        ring.mark_good(2);
        let order: Vec<usize> = ring.order().iter().map(|(i, _)| *i).collect();
        assert_eq!(order, vec![2, 0, 1]);
        assert!(KeyRing::new(Vec::new()).order().is_empty());
    }

    #[test]
    fn groq_reasoning_params_go_only_to_models_that_accept_them() {
        let oss = Tuning::initial(Provider::Groq, "openai/gpt-oss-20b");
        assert_eq!(oss.reasoning_effort, vec!["low"]);
        assert!(oss.hide_reasoning);
        let llama = Tuning::initial(Provider::Groq, "llama-3.3-70b-versatile");
        assert!(llama.reasoning_effort.is_empty());
        assert!(!llama.hide_reasoning);
        let qwen = Tuning::initial(Provider::Groq, "qwen/qwen3.8-27b");
        assert_eq!(qwen.reasoning_effort, vec!["none"]);
    }

    #[test]
    fn openai_reasoning_models_skip_temperature_and_get_headroom() {
        let mini = Tuning::initial(Provider::OpenAi, "gpt-4o-mini");
        assert_eq!(mini.temperature, Some(0.3));
        assert!(mini.reasoning_effort.is_empty());
        for model in ["gpt-6-luna", "gpt-5-mini", "o4-mini"] {
            let tuning = Tuning::initial(Provider::OpenAi, model);
            assert_eq!(tuning.temperature, None, "{model}");
            assert_eq!(tuning.reasoning_effort[0], "none", "{model}");
            assert!(tuning.max_tokens > MAX_ANSWER_TOKENS, "{model}");
        }
    }

    #[test]
    fn gemini_thinking_is_minimized_per_generation() {
        let flash = Tuning::initial(Provider::Gemini, "gemini-2.5-flash");
        assert_eq!(flash.thinking, Some(json!({ "thinkingBudget": 0 })));
        assert_eq!(flash.max_tokens, MAX_ANSWER_TOKENS);
        let three = Tuning::initial(Provider::Gemini, "gemini-3.8-flash");
        assert_eq!(three.thinking, Some(json!({ "thinkingLevel": "low" })));
        assert!(three.max_tokens > MAX_ANSWER_TOKENS);
        let lite = Tuning::initial(Provider::Gemini, "gemini-3.5-flash-lite");
        assert_eq!(lite.thinking, Some(json!({ "thinkingLevel": "minimal" })));
        let pro = Tuning::initial(Provider::Gemini, "gemini-2.5-pro");
        assert_eq!(pro.thinking, Some(json!({ "thinkingBudget": 128 })));
    }

    #[test]
    fn a_rejected_setting_is_dropped_or_stepped_down_and_nothing_else_retries() {
        let mut tuning = Tuning::initial(Provider::OpenAi, "gpt-5-mini");
        assert!(tuning.adapt(
            r#"{"error":{"message":"Unsupported value: 'reasoning_effort' does not support 'none' with this model."}}"#
        ));
        assert_eq!(tuning.reasoning_effort, vec!["minimal", "low"]);

        let mut tuning = Tuning::initial(Provider::Anthropic, "claude-some-future-model");
        assert!(tuning.adapt("temperature is not supported for this model"));
        assert_eq!(tuning.temperature, None);
        assert!(!tuning.adapt("temperature is not supported for this model"));

        let mut tuning = Tuning::initial(Provider::Gemini, "gemini-3.8-flash");
        assert!(tuning.adapt("Invalid value at 'generation_config.thinking_config'"));
        assert_eq!(tuning.thinking, None);

        let mut tuning = Tuning::initial(Provider::Groq, "openai/gpt-oss-20b");
        assert!(tuning.adapt("property 'include_reasoning' is unsupported"));
        assert!(!tuning.hide_reasoning);

        // A real bad request is not something a setting change can fix.
        let mut tuning = Tuning::initial(Provider::Groq, "openai/gpt-oss-20b");
        assert!(!tuning.adapt("messages: content must not be empty"));
    }

    #[test]
    fn a_missing_model_is_recognized_so_the_next_default_can_take_over() {
        use reqwest::StatusCode;
        assert!(is_model_unavailable(StatusCode::NOT_FOUND, "anything"));
        assert!(is_model_unavailable(
            StatusCode::BAD_REQUEST,
            "The model `gemini-2.0-flash` has been decommissioned"
        ));
        assert!(is_model_unavailable(
            StatusCode::BAD_REQUEST,
            "model_not_found: The model gpt-x does not exist"
        ));
        assert!(!is_model_unavailable(
            StatusCode::BAD_REQUEST,
            "temperature is not supported"
        ));
        assert!(!is_model_unavailable(
            StatusCode::UNAUTHORIZED,
            "model not found"
        ));
    }

    fn body_of(request: reqwest::Request) -> Value {
        serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap()
    }

    #[test]
    fn openai_compatible_requests_carry_only_the_settings_in_the_tuning() {
        let client = reqwest::Client::new();
        let oss = Tuning::initial(Provider::Groq, "openai/gpt-oss-20b");
        let request = build_chat_request(
            &client,
            Provider::Groq,
            "k",
            "openai/gpt-oss-20b",
            "hi",
            &oss,
        )
        .build()
        .unwrap();
        assert_eq!(
            request.url().as_str(),
            "https://api.groq.com/openai/v1/chat/completions"
        );
        let body = body_of(request);
        assert_eq!(body["reasoning_effort"], "low");
        assert_eq!(body["include_reasoning"], false);
        assert_eq!(body["stream"], true);

        let luna = Tuning::initial(Provider::OpenAi, "gpt-6-luna");
        let body = body_of(
            build_chat_request(&client, Provider::OpenAi, "k", "gpt-6-luna", "hi", &luna)
                .build()
                .unwrap(),
        );
        assert!(body.get("temperature").is_none());
        assert!(body.get("include_reasoning").is_none());
        assert_eq!(body["reasoning_effort"], "none");
    }

    #[test]
    fn gemini_request_sends_thinking_config_inside_generation_config() {
        let client = reqwest::Client::new();
        let tuning = Tuning::initial(Provider::Gemini, "gemini-2.5-flash");
        let request = build_chat_request(
            &client,
            Provider::Gemini,
            "k",
            "gemini-2.5-flash",
            "hi",
            &tuning,
        )
        .build()
        .unwrap();
        assert!(request
            .url()
            .path()
            .ends_with("gemini-2.5-flash:streamGenerateContent"));
        let body = body_of(request);
        assert_eq!(
            body["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            0
        );
        assert_eq!(body["contents"][0]["parts"][0]["text"], "hi");
    }

    #[test]
    fn bedrock_uses_the_documented_converse_shape_and_an_optional_region() {
        let client = reqwest::Client::new();
        let tuning = Tuning::initial(Provider::Bedrock, "amazon.nova-lite-v1:0");
        let request = build_chat_request(
            &client,
            Provider::Bedrock,
            "test-key",
            "amazon.nova-lite-v1:0",
            "hello",
            &tuning,
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
        let body = body_of(request);
        assert_eq!(body["messages"][0]["content"][0]["text"], "hello");
        assert_eq!(body["inferenceConfig"]["maxTokens"], MAX_ANSWER_TOKENS);

        assert_eq!(
            bedrock_target("eu-west-1/amazon.nova-lite-v1:0"),
            ("eu-west-1", "amazon.nova-lite-v1:0")
        );
        // Cross-region inference profile ids contain dots, not a region path.
        assert_eq!(
            bedrock_target("us.amazon.nova-lite-v1:0"),
            ("us-east-1", "us.amazon.nova-lite-v1:0")
        );
    }

    #[test]
    fn delta_text_is_read_from_each_providers_stream_shape() {
        let openai = json!({ "choices": [{ "delta": { "content": "Hello" } }] });
        assert_eq!(
            extract_delta_text(Provider::Groq, &openai),
            Some("Hello".into())
        );
        assert_eq!(
            extract_delta_text(Provider::OpenAi, &openai),
            Some("Hello".into())
        );

        let anthropic = json!({ "type": "content_block_delta", "delta": { "text": "Hi" } });
        assert_eq!(
            extract_delta_text(Provider::Anthropic, &anthropic),
            Some("Hi".into())
        );
        assert_eq!(
            extract_delta_text(Provider::Anthropic, &json!({ "type": "message_start" })),
            None
        );

        // A Gemini 3 chunk: thought signature first, answer text after, and
        // a thought part that must not reach the screen.
        let gemini = json!({ "candidates": [{ "content": { "parts": [
            { "thoughtSignature": "abc" },
            { "text": "thinking...", "thought": true },
            { "text": "Answer" }
        ] } }] });
        assert_eq!(
            extract_delta_text(Provider::Gemini, &gemini),
            Some("Answer".into())
        );

        for provider in Provider::ALL {
            assert_eq!(extract_delta_text(provider, &json!({})), None);
        }
    }

    #[test]
    fn automatic_transcription_prefers_groq_then_the_answer_provider() {
        let keys = |list: &[Provider]| -> ProviderKeys {
            list.iter().map(|p| (*p, vec!["k".to_string()])).collect()
        };
        // Groq present: fastest transcription wins regardless of answers.
        assert_eq!(
            resolve_stt_provider(
                None,
                Provider::OpenAi,
                &keys(&[Provider::Groq, Provider::OpenAi])
            )
            .unwrap(),
            Provider::Groq
        );
        // One OpenAI key does both jobs.
        assert_eq!(
            resolve_stt_provider(None, Provider::OpenAi, &keys(&[Provider::OpenAi])).unwrap(),
            Provider::OpenAi
        );
        // Claude answers, Gemini hears: only the transcript goes to Claude.
        assert_eq!(
            resolve_stt_provider(
                None,
                Provider::Anthropic,
                &keys(&[Provider::Anthropic, Provider::Gemini])
            )
            .unwrap(),
            Provider::Gemini
        );
        // Claude alone cannot hear, and the error says what to add.
        let error = resolve_stt_provider(None, Provider::Anthropic, &keys(&[Provider::Anthropic]))
            .unwrap_err()
            .to_string();
        assert!(error.contains("Groq, OpenAI or Gemini"), "{error}");
        // An explicit choice without keys, or one that cannot hear, is refused.
        assert!(resolve_stt_provider(
            Some(Provider::OpenAi),
            Provider::Groq,
            &keys(&[Provider::Groq])
        )
        .is_err());
        assert!(resolve_stt_provider(
            Some(Provider::Bedrock),
            Provider::Bedrock,
            &keys(&[Provider::Bedrock])
        )
        .is_err());
    }

    #[test]
    fn gpt_transcribe_is_not_sent_the_singular_language_field() {
        assert!(!sends_language_field("gpt-transcribe", "en"));
        assert!(sends_language_field("gpt-4o-mini-transcribe", "en"));
        assert!(sends_language_field("whisper-large-v3-turbo", "en"));
        assert!(!sends_language_field("whisper-large-v3-turbo", ""));

        let client = reqwest::Client::new();
        let request = SttEngine::new(Provider::OpenAi, vec!["k".into()], "en")
            .request(&client, "k", "gpt-transcribe", b"RIFF")
            .unwrap()
            .build()
            .unwrap();
        assert_eq!(
            request.url().as_str(),
            "https://api.openai.com/v1/audio/transcriptions"
        );
    }

    #[test]
    fn gemini_transcription_sends_inline_audio_and_reads_text_parts() {
        let client = reqwest::Client::new();
        let stt = SttEngine::new(Provider::Gemini, vec!["k".into()], "en");
        let request = stt
            .request(&client, "k", "gemini-2.5-flash-lite", b"RIFFDATA")
            .unwrap()
            .build()
            .unwrap();
        assert!(request
            .url()
            .path()
            .ends_with("gemini-2.5-flash-lite:generateContent"));
        let body = body_of(request);
        assert_eq!(
            body["contents"][0]["parts"][0]["inlineData"]["mimeType"],
            "audio/wav"
        );
        assert_eq!(
            body["contents"][0]["parts"][0]["inlineData"]["data"],
            base64::engine::general_purpose::STANDARD.encode(b"RIFFDATA")
        );
        assert_eq!(
            body["generationConfig"]["thinkingConfig"]["thinkingBudget"],
            0
        );

        assert!(SttEngine::new(Provider::Anthropic, vec!["k".into()], "en")
            .request(&client, "k", "", b"")
            .is_err());
    }

    #[test]
    fn provider_error_bodies_reduce_to_their_message() {
        assert_eq!(
            concise_error(r#"{"error":{"message":"bad key"}}"#),
            "bad key"
        );
        assert_eq!(
            concise_error(r#"{"message":"Operation not allowed"}"#),
            "Operation not allowed"
        );
        assert_eq!(concise_error("plain text"), "plain text");
    }

    #[test]
    fn key_rotation_is_limited_to_auth_capacity_and_server_failures() {
        use reqwest::StatusCode;
        assert!(should_rotate_key(StatusCode::UNAUTHORIZED));
        assert!(should_rotate_key(StatusCode::TOO_MANY_REQUESTS));
        assert!(should_rotate_key(StatusCode::BAD_GATEWAY));
        assert!(!should_rotate_key(StatusCode::BAD_REQUEST));
    }
}
