//! Platform capability detection.
//!
//! Behavior must not branch on `if macOS`/`if Windows` alone (PRD §7):
//! decide what the current OS *can actually do* once, here, and let every
//! other module read that instead of re-deriving it.

use serde::Serialize;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct PlatformCapabilities {
    pub microphone_capture: bool,
    pub loopback_audio: bool,
    /// ScreenCaptureKit system-audio tap. macOS 13+ only.
    pub native_system_audio: bool,
    pub content_protection: bool,
}

pub fn detect() -> PlatformCapabilities {
    PlatformCapabilities {
        microphone_capture: true,
        loopback_audio: true,
        native_system_audio: native_system_audio_available(),
        // Windows honours this through `SetWindowDisplayAffinity` with
        // `WDA_EXCLUDEFROMCAPTURE` (Windows 10 2004+), which Tauri sets for
        // `set_content_protected` — reliably, unlike the pre-macOS-13
        // window-exclusion path.
        content_protection: cfg!(any(target_os = "macos", target_os = "windows")),
    }
}

/// Gated on the `native-system-audio` Cargo feature, not just `target_os =
/// "macos"`: the ScreenCaptureKit backend needs Swift tools 5.9+ (Xcode
/// 15+), so a build made on an older toolchain has no native backend
/// compiled in and must report this capability as unavailable regardless of
/// the running OS version. `cfg!` (a runtime constant), not `#[cfg]`, so
/// this stays one function instead of two near-duplicates — the `&&`
/// short-circuits before `macos_major_version()` runs on any build where
/// the feature is off.
fn native_system_audio_available() -> bool {
    cfg!(all(target_os = "macos", feature = "native-system-audio"))
        && macos_major_version()
            .map(|major| major >= 13)
            .unwrap_or(false)
}

/// Parses the major version out of `sw_vers -productVersion` output
/// (e.g. "13.4.1" -> 13). Fails closed to `None` on anything unexpected —
/// including running on an OS with no `sw_vers` binary at all — so this
/// never crashes the app, it just falls back to the loopback path.
fn macos_major_version() -> Option<u32> {
    let output = std::process::Command::new("sw_vers")
        .arg("-productVersion")
        .output()
        .ok()?;
    parse_major_version(&String::from_utf8_lossy(&output.stdout))
}

fn parse_major_version(version: &str) -> Option<u32> {
    version.trim().split('.').next()?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_major_version_from_sw_vers_output() {
        assert_eq!(parse_major_version("13.4.1\n"), Some(13));
        assert_eq!(parse_major_version("12.7.6"), Some(12));
    }

    #[test]
    fn malformed_version_fails_closed_to_none() {
        assert_eq!(parse_major_version(""), None);
        assert_eq!(parse_major_version("not-a-version"), None);
    }
}
