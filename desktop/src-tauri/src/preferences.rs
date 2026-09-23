//! Persisted desktop preferences.
//!
//! Capture protection defaults on. A missing or malformed file must fail
//! closed because the user may start a live session before opening settings.
//!
//! Keys are stored per provider, so any answer provider can be paired with
//! any transcription provider and switching between them never loses a key.
//! Files written by 0.2 and earlier (one Groq list plus one list for "the
//! answer provider") are migrated on load.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const FILE_NAME: &str = "desktop-preferences.json";

/// Models that were once this app's defaults and have since been shut down
/// by their provider. A saved copy of one would fail every answer, so it is
/// cleared back to "use the current default" on load.
const RETIRED_MODELS: &[&str] = &["gemini-2.0-flash", "gemini-2.0-flash-001"];

/// The shape `migrate` produces. Files without the field are version 0.
const CURRENT_VERSION: u32 = 3;

/// What the 0.2 setup screen wrote into the model field on its own whenever
/// a provider was picked. A 0.2 file holding one of these did not choose it;
/// clearing it moves that user onto today's default and its fallbacks
/// instead of pinning them to a model this build no longer prefers.
const AUTO_FILLED_0_2_DEFAULTS: &[&str] = &[
    "openai/gpt-oss-20b",
    "gpt-4o-mini",
    "claude-haiku-4-5-20251001",
    "gemini-2.0-flash",
    "amazon.nova-lite-v1:0",
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct Preferences {
    pub version: u32,
    pub protect_hud_from_screen_capture: bool,
    /// Provider id ("groq", "openai", "anthropic", "gemini", "bedrock") to
    /// that provider's keys, in failover order.
    pub provider_keys: BTreeMap<String, Vec<String>>,
    pub role_title: String,
    pub company_name: String,
    pub resume_text: String,
    pub job_description: String,
    pub language: String,
    /// Empty means the provider's current default (with fallbacks).
    pub chat_model: String,
    pub chat_provider: String,
    /// "auto" or a provider id that can transcribe (groq, openai, gemini).
    pub stt_provider: String,
    /// Set once the first-launch permission priming pass has run.
    pub permissions_primed: bool,

    // ── Read from 0.2 files, migrated, never written back ────────────
    #[serde(skip_serializing_if = "String::is_empty")]
    pub groq_api_key: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub groq_api_keys: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub chat_api_keys: Vec<String>,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            // A missing file is a fresh install: nothing to migrate. A file
            // without the field deserializes this default too, so version
            // is read with its own default below.
            version: CURRENT_VERSION,
            protect_hud_from_screen_capture: true,
            provider_keys: BTreeMap::new(),
            role_title: String::new(),
            company_name: String::new(),
            resume_text: String::new(),
            job_description: String::new(),
            language: "en".to_string(),
            chat_model: String::new(),
            chat_provider: "groq".to_string(),
            stt_provider: "auto".to_string(),
            permissions_primed: false,
            groq_api_key: String::new(),
            groq_api_keys: Vec::new(),
            chat_api_keys: Vec::new(),
        }
    }
}

impl Preferences {
    pub fn keys(&self, provider: &str) -> &[String] {
        self.provider_keys
            .get(provider)
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    pub fn add_keys(&mut self, provider: &str, keys: impl IntoIterator<Item = String>) {
        let list = self.provider_keys.entry(provider.to_string()).or_default();
        for key in keys {
            let key = key.trim().to_string();
            if !key.is_empty() && !list.contains(&key) {
                list.push(key);
            }
        }
        if list.is_empty() {
            self.provider_keys.remove(provider);
        }
    }

    /// Bring a file from any earlier version to the current shape.
    pub fn migrate(&mut self) {
        let legacy_groq: Vec<String> = std::mem::take(&mut self.groq_api_keys)
            .into_iter()
            .chain(Some(std::mem::take(&mut self.groq_api_key)))
            .collect();
        self.add_keys("groq", legacy_groq);

        // 0.2 kept one list for whichever non-Groq provider was selected.
        let legacy_chat = std::mem::take(&mut self.chat_api_keys);
        if self.chat_provider != "groq" {
            let provider = self.chat_provider.clone();
            self.add_keys(&provider, legacy_chat);
        }

        if RETIRED_MODELS.contains(&self.chat_model.trim())
            || (self.version < 3 && AUTO_FILLED_0_2_DEFAULTS.contains(&self.chat_model.trim()))
        {
            self.chat_model.clear();
        }
        self.version = CURRENT_VERSION;
        if self.chat_provider.trim().is_empty() {
            self.chat_provider = "groq".to_string();
        }
        if self.stt_provider.trim().is_empty() {
            self.stt_provider = "auto".to_string();
        }
    }
}

pub fn path(config_dir: &Path) -> PathBuf {
    config_dir.join(FILE_NAME)
}

pub fn load(path: &Path) -> Preferences {
    let mut preferences = fs::read_to_string(path)
        .ok()
        .and_then(|raw| parse(&raw))
        .unwrap_or_default();
    preferences.migrate();
    preferences
}

fn parse(raw: &str) -> Option<Preferences> {
    let value: serde_json::Value = serde_json::from_str(raw).ok()?;
    let version = value.get("version").and_then(|v| v.as_u64()).unwrap_or(0) as u32;
    let mut preferences: Preferences = serde_json::from_value(value).ok()?;
    preferences.version = version;
    Some(preferences)
}

pub fn save(path: &Path, preferences: &Preferences) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let temporary = path.with_extension("json.tmp");
    fs::write(&temporary, serde_json::to_vec_pretty(preferences)?)?;
    fs::rename(temporary, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `name` is unique per test. Not the thread name: the harness names
    /// threads `preferences::tests::…`, and `:` is illegal in Windows paths.
    fn test_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("verity-{name}-{}.json", std::process::id()))
    }

    #[test]
    fn protection_defaults_on_when_no_file_exists() {
        let file = test_path("missing");
        let _ = fs::remove_file(&file);
        assert!(load(&file).protect_hud_from_screen_capture);
    }

    #[test]
    fn malformed_preferences_fail_closed() {
        let file = test_path("malformed");
        fs::write(&file, "not-json").unwrap();
        assert!(load(&file).protect_hud_from_screen_capture);
        let _ = fs::remove_file(file);
    }

    #[test]
    fn preferences_round_trip() {
        let file = test_path("round-trip");
        let mut expected = Preferences {
            protect_hud_from_screen_capture: false,
            role_title: "Backend Engineer".to_string(),
            company_name: "Example".to_string(),
            resume_text: "Built reliable APIs.".to_string(),
            job_description: "Own backend services.".to_string(),
            chat_model: "claude-haiku-4-5-20251001".to_string(),
            chat_provider: "anthropic".to_string(),
            stt_provider: "gemini".to_string(),
            permissions_primed: true,
            ..Preferences::default()
        };
        expected.add_keys("anthropic", ["sk-ant-test".to_string()]);
        expected.add_keys("gemini", ["AIza-test".to_string()]);
        save(&file, &expected).unwrap();
        assert_eq!(load(&file), expected);
        let _ = fs::remove_file(file);
    }

    #[test]
    fn a_0_2_file_keeps_every_key_after_migration() {
        let file = test_path("legacy");
        fs::write(
            &file,
            r#"{
                "protect_hud_from_screen_capture": true,
                "groq_api_key": "gsk_oldest",
                "groq_api_keys": ["gsk_one", "gsk_two"],
                "chat_provider": "openai",
                "chat_api_keys": ["sk-openai"],
                "chat_model": "gpt-4o-mini",
                "permissions_primed": true
            }"#,
        )
        .unwrap();
        let loaded = load(&file);
        assert_eq!(loaded.keys("groq"), ["gsk_one", "gsk_two", "gsk_oldest"]);
        assert_eq!(loaded.keys("openai"), ["sk-openai"]);
        assert_eq!(loaded.stt_provider, "auto");
        // Auto-filled by 0.2, so not a real choice: back to the defaults.
        assert_eq!(loaded.chat_model, "");
        assert_eq!(loaded.version, CURRENT_VERSION);
        // Written back in the new shape only.
        save(&file, &loaded).unwrap();
        let raw = fs::read_to_string(&file).unwrap();
        assert!(!raw.contains("groq_api_keys"), "{raw}");
        assert!(!raw.contains("chat_api_keys"), "{raw}");
        let _ = fs::remove_file(file);
    }

    #[test]
    fn a_groq_only_0_2_file_does_not_copy_groq_keys_into_another_provider() {
        let mut preferences = Preferences {
            chat_provider: "groq".to_string(),
            groq_api_keys: vec!["gsk_one".to_string()],
            chat_api_keys: vec!["stale".to_string()],
            ..Preferences::default()
        };
        preferences.migrate();
        assert_eq!(preferences.keys("groq"), ["gsk_one"]);
        assert_eq!(preferences.provider_keys.len(), 1);
    }

    #[test]
    fn a_model_typed_in_0_3_survives_every_later_load() {
        let file = test_path("explicit-model");
        let mut chosen = Preferences {
            chat_model: "openai/gpt-oss-20b".to_string(),
            ..Preferences::default()
        };
        chosen.migrate();
        save(&file, &chosen).unwrap();
        assert_eq!(load(&file).chat_model, "openai/gpt-oss-20b");
        assert_eq!(load(&file).chat_model, "openai/gpt-oss-20b");
        let _ = fs::remove_file(file);
    }

    #[test]
    fn a_custom_model_in_a_0_2_file_is_kept() {
        let mut preferences =
            parse(r#"{"chat_provider":"openai","chat_model":"gpt-6-luna"}"#).unwrap();
        preferences.migrate();
        assert_eq!(preferences.chat_model, "gpt-6-luna");
    }

    #[test]
    fn a_saved_retired_model_is_reset_to_the_current_default() {
        let mut preferences = Preferences {
            chat_provider: "gemini".to_string(),
            chat_model: "gemini-2.0-flash".to_string(),
            ..Preferences::default()
        };
        preferences.migrate();
        assert_eq!(preferences.chat_model, "");
    }

    #[test]
    fn permissions_primed_defaults_false_for_missing_or_old_files() {
        let file = test_path("primed-default");
        let _ = fs::remove_file(&file);
        assert!(!load(&file).permissions_primed);
        fs::write(&file, r#"{"protect_hud_from_screen_capture": true}"#).unwrap();
        assert!(!load(&file).permissions_primed);
        let _ = fs::remove_file(file);
    }
}
