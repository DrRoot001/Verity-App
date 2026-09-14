//! A plain append-only log file for diagnosing a live capture/session
//! failure after the fact.
//!
//! Release builds set `windows_subsystem = "windows"` so the app has no
//! console at all — every `eprintln!` in the capture and session code goes
//! nowhere on a real Windows install, which is exactly the situation this
//! exists to fix. No crate: appending a timestamped line to a file the user
//! can open and paste back is the whole requirement.

use std::io::Write;
use std::path::{Path, PathBuf};

pub fn path(config_dir: &Path) -> PathBuf {
    config_dir.join("debug.log")
}

pub fn log(path: &Path, message: &str) {
    let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    else {
        return;
    };
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or_default();
    let _ = writeln!(file, "[{elapsed}] {message}");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appends_lines_in_order_across_calls() {
        let file = std::env::temp_dir().join(format!("verity-debuglog-{}.log", std::process::id()));
        let _ = std::fs::remove_file(&file);

        log(&file, "first");
        log(&file, "second");

        let contents = std::fs::read_to_string(&file).unwrap();
        let lines: Vec<&str> = contents.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].ends_with("] first"));
        assert!(lines[1].ends_with("] second"));

        let _ = std::fs::remove_file(&file);
    }
}
