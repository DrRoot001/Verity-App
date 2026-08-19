//! Persisted desktop preferences.
//!
//! Capture protection defaults on. A missing or malformed file must fail
//! closed because the user may start a live session before opening settings.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const FILE_NAME: &str = "desktop-preferences.json";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct Preferences {
    pub protect_hud_from_screen_capture: bool,
    /// Kept for one-way migration from the original single-key build.
    pub groq_api_key: String,
    pub groq_api_keys: Vec<String>,
    pub role_title: String,
    pub company_name: String,
    pub resume_text: String,
    pub job_description: String,
    pub language: String,
    pub chat_model: String,
    /// Set once the first-launch microphone/screen-recording priming pass
    /// has run, so returning users are never re-prompted by app logic.
    pub permissions_primed: bool,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            protect_hud_from_screen_capture: true,
            groq_api_key: String::new(),
            groq_api_keys: Vec::new(),
            role_title: String::new(),
            company_name: String::new(),
            resume_text: String::new(),
            job_description: String::new(),
            language: "en".to_string(),
            chat_model: "allam-2-7b".to_string(),
            permissions_primed: false,
        }
    }
}

pub fn path(config_dir: &Path) -> PathBuf {
    config_dir.join(FILE_NAME)
}

pub fn load(path: &Path) -> Preferences {
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
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

    fn test_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "verity-{name}-{}-{}.json",
            std::process::id(),
            std::thread::current().name().unwrap_or("thread")
        ))
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
    fn capture_protection_round_trips() {
        let file = test_path("round-trip");
        let expected = Preferences {
            protect_hud_from_screen_capture: false,
            groq_api_key: "gsk_test".to_string(),
            groq_api_keys: vec!["gsk_test".to_string(), "gsk_backup".to_string()],
            role_title: "Backend Engineer".to_string(),
            company_name: "Example".to_string(),
            resume_text: "Built reliable APIs.".to_string(),
            job_description: "Own backend services.".to_string(),
            language: "en".to_string(),
            chat_model: "openai/gpt-oss-20b".to_string(),
            permissions_primed: true,
        };
        save(&file, &expected).unwrap();
        assert_eq!(load(&file), expected);
        let _ = fs::remove_file(file);
    }

    #[test]
    fn permissions_primed_defaults_false_for_missing_or_old_files() {
        let file = test_path("primed-default");
        let _ = fs::remove_file(&file);
        assert!(!load(&file).permissions_primed);

        // A preferences file saved before this field existed must still
        // load, with priming defaulting to false rather than failing.
        fs::write(&file, r#"{"protect_hud_from_screen_capture": true}"#).unwrap();
        assert!(!load(&file).permissions_primed);
        let _ = fs::remove_file(file);
    }
}
