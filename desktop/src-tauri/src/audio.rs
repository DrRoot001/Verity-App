//! Audio capture for the live interview assistant.
//!
//! The whole point of the desktop app is this file. A browser can only hear the
//! machine's microphone, which means the interviewer's voice has to travel out
//! of your speakers and back in — so it fails with headphones, picks up the
//! room, and mixes both sides into one channel. Capturing from an input device
//! directly avoids all three.
//!
//! macOS has no public API for tapping system output before macOS 13, so on
//! that range of the OS the supported route is an audio loopback device —
//! BlackHole or Loopback — which appears to the OS as an ordinary *input*.
//! `list_devices` surfaces those alongside real microphones and marks the
//! ones that look like loopbacks, so the setup screen can point at the right
//! one instead of asking the user to understand Core Audio.
//!
//! macOS 13+ gets a real native path instead: ScreenCaptureKit taps system
//! output directly, no virtual device required. That backend lives in
//! `native_audio.rs` (this module has no ScreenCaptureKit code in it) and is
//! selected by passing [`NATIVE_SYSTEM_AUDIO_DEVICE_ID`] as the device name
//! — `platform::detect()` decides whether that option is even offered.
//!
//! Windows needs none of that. cpal's WASAPI host sets
//! `AUDCLNT_STREAMFLAGS_LOOPBACK` automatically when an *output* device is
//! opened for input, so the speakers themselves are a system-audio tap: no
//! virtual driver, no multi-output device, and nothing for the user to route.
//! The catch is that `host.input_devices()` does not list output devices, so
//! [`list_devices`] enumerates them separately there and [`find_device`]
//! knows to look among them.
//!
//! Everything is normalised to 16 kHz mono PCM16 here, because that is what the
//! server's frame protocol and Whisper both expect, and doing it at the source
//! keeps the wire small enough to stream continuously.

use std::sync::mpsc::Sender;

use anyhow::{anyhow, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat, Stream, StreamConfig};
use serde::Serialize;

/// What the realtime callback hands to the session: audio, or a fatal stream
/// error. The error variant must reach the session promptly — the callback
/// may simply stop firing on failure, so the byte channel closing on its own
/// is not a reliable signal.
pub enum CaptureMessage {
    Pcm(Vec<u8>),
    Error(String),
}

/// What the server and Whisper both want.
pub const TARGET_RATE: u32 = 16_000;

/// The `device` value that routes `start_listening` to `native_audio::start`
/// instead of a real cpal device. Not a real device name, so it can never
/// collide with one.
pub const NATIVE_SYSTEM_AUDIO_DEVICE_ID: &str = "__verity_native_system_audio__";

/// Names that indicate a virtual loopback rather than a physical microphone.
/// Used only to *hint* in the UI — the user can still pick anything.
const LOOPBACK_HINTS: [&str; 5] = [
    "blackhole",
    "loopback",
    "soundflower",
    "aggregate",
    "multi-output",
];

#[derive(Debug, Clone, Serialize)]
pub struct AudioDevice {
    pub name: String,
    /// True when the name suggests it carries system output, not a mic.
    pub is_loopback: bool,
    pub is_default: bool,
    /// True for the single synthetic ScreenCaptureKit entry, never a real
    /// cpal device.
    pub is_native_system_audio: bool,
}

pub fn list_devices() -> Result<Vec<AudioDevice>> {
    let host = cpal::default_host();
    let default_name = host
        .default_input_device()
        .and_then(|d| d.name().ok())
        .unwrap_or_default();

    let mut devices = Vec::new();
    for device in host.input_devices()? {
        let name = match device.name() {
            Ok(name) => name,
            Err(_) => continue,
        };
        let lowered = name.to_lowercase();
        devices.push(AudioDevice {
            is_loopback: LOOPBACK_HINTS.iter().any(|hint| lowered.contains(hint)),
            is_default: name == default_name,
            is_native_system_audio: false,
            name,
        });
    }

    // On Windows every output device doubles as a system-audio tap, so offer
    // them as capture sources too.
    if cfg!(target_os = "windows") {
        let default_output = host
            .default_output_device()
            .and_then(|d| d.name().ok())
            .unwrap_or_default();
        let names = host
            .output_devices()?
            .filter_map(|device| device.name().ok())
            .collect();
        devices.extend(loopback_entries(&devices, names, &default_output));
    }

    Ok(devices)
}

/// Turn output-device names into loopback capture entries.
///
/// Two rules, both of which decide whether the user ends up recording silence:
/// a name already taken by an input endpoint is dropped, because the name is
/// the only device id the UI and preferences carry and a duplicate would make
/// the two indistinguishable; and the default output leads, because it is
/// where a call app is actually playing and the setup screen offers the first
/// loopback entry it finds.
fn loopback_entries(
    existing: &[AudioDevice],
    names: Vec<String>,
    default_output: &str,
) -> Vec<AudioDevice> {
    let mut entries: Vec<AudioDevice> = names
        .into_iter()
        .filter(|name| !existing.iter().any(|device| device.name == *name))
        .map(|name| AudioDevice {
            is_default: name == default_output,
            is_loopback: true,
            is_native_system_audio: false,
            name,
        })
        .collect();
    entries.sort_by_key(|device| !device.is_default);
    entries
}

fn find_device(name: &str) -> Result<Device> {
    let host = cpal::default_host();
    if name.is_empty() {
        return host
            .default_input_device()
            .ok_or_else(|| anyhow!("no input device available"));
    }
    for device in host.input_devices()? {
        if device.name().map(|n| n == name).unwrap_or(false) {
            return Ok(device);
        }
    }
    // Not a capture endpoint: on Windows it may be an output device, which
    // WASAPI opens in loopback mode. `list_devices` guarantees the name is
    // unique across both lists, so this cannot shadow a microphone.
    if cfg!(target_os = "windows") {
        for device in host.output_devices()? {
            if device.name().map(|n| n == name).unwrap_or(false) {
                return Ok(device);
            }
        }
    }
    Err(anyhow!("input device '{name}' was not found"))
}

/// Mix to mono and resample to 16 kHz.
///
/// Linear interpolation rather than a windowed filter: the input is speech
/// destined for a transcription model, the ratio is usually a clean 3:1 from
/// 48 kHz, and a proper resampler would add a dependency and latency to the one
/// path with a realtime budget.
fn to_mono_16k(samples: &[f32], channels: u16, source_rate: u32) -> Vec<i16> {
    let channels = channels.max(1) as usize;
    let frame_count = samples.len() / channels;
    if frame_count == 0 {
        return Vec::new();
    }

    let mut mono = Vec::with_capacity(frame_count);
    for frame in 0..frame_count {
        let mut sum = 0.0f32;
        for channel in 0..channels {
            sum += samples[frame * channels + channel];
        }
        mono.push(sum / channels as f32);
    }

    if source_rate == TARGET_RATE {
        return mono.iter().map(|s| to_i16(*s)).collect();
    }

    let ratio = source_rate as f32 / TARGET_RATE as f32;
    let out_len = (mono.len() as f32 / ratio).floor() as usize;
    let mut out = Vec::with_capacity(out_len);
    for i in 0..out_len {
        let position = i as f32 * ratio;
        let index = position.floor() as usize;
        let frac = position - index as f32;
        let a = mono.get(index).copied().unwrap_or(0.0);
        let b = mono.get(index + 1).copied().unwrap_or(a);
        out.push(to_i16(a + (b - a) * frac));
    }
    out
}

pub(crate) fn to_i16(sample: f32) -> i16 {
    (sample.clamp(-1.0, 1.0) * i16::MAX as f32) as i16
}

/// Handle to a running capture. Dropping it stops the stream and releases the
/// device.
///
/// The `cpal::Stream` itself is deliberately *not* stored here. It is `!Send`,
/// so holding it in shared application state makes that state non-`Send`, which
/// Tauri's `State` requires — the compiler rejects the whole app. Instead the
/// stream is owned by a thread that does nothing but keep it alive, and this
/// handle only carries a stop signal, which is trivially `Send`.
pub struct Capture {
    stop: std::sync::mpsc::Sender<()>,
}

impl Drop for Capture {
    fn drop(&mut self) {
        // Releasing the device is what turns off the OS recording indicator.
        let _ = self.stop.send(());
    }
}

/// Start capturing `device_name`, sending 16 kHz mono PCM16 chunks to `sink`.
///
/// The callback runs on a realtime audio thread, so it does no allocation
/// beyond the converted buffer and never blocks: it hands the bytes to a
/// channel and returns. Anything slower here is audible as a dropout.
pub fn start(device_name: &str, sink: Sender<CaptureMessage>) -> Result<Capture> {
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<(), String>>();
    let (stop_tx, stop_rx) = std::sync::mpsc::channel::<()>();
    let name = device_name.to_string();

    std::thread::spawn(move || match build_stream(&name, sink) {
        Ok(stream) => {
            let _ = ready_tx.send(Ok(()));
            // Park here so `stream` stays alive. It is dropped when the handle
            // is dropped or the app exits.
            let _ = stop_rx.recv();
            drop(stream);
        }
        Err(err) => {
            let _ = ready_tx.send(Err(err.to_string()));
        }
    });

    // Surface a bad device as an error the user can act on, rather than a
    // silent capture that never produces audio.
    match ready_rx.recv() {
        Ok(Ok(())) => Ok(Capture { stop: stop_tx }),
        Ok(Err(message)) => Err(anyhow!(message)),
        Err(_) => Err(anyhow!("the audio thread stopped before it started")),
    }
}

fn build_stream(device_name: &str, sink: Sender<CaptureMessage>) -> Result<Stream> {
    let device = find_device(device_name)?;
    // A Windows output device reports no *input* config — it is a render
    // endpoint — but WASAPI still captures it in loopback mode at its output
    // format. Falling back keeps one code path for both kinds of device.
    let config = match device.default_input_config() {
        Ok(config) => config,
        Err(err) => device.default_output_config().map_err(|_| err)?,
    };
    let sample_format = config.sample_format();
    let source_rate = config.sample_rate().0;
    let channels = config.channels();
    let stream_config: StreamConfig = config.into();

    let error_sink = sink.clone();
    let on_error = move |err: cpal::StreamError| {
        eprintln!("audio stream error: {err}");
        let _ = error_sink.send(CaptureMessage::Error(err.to_string()));
    };

    let emit = move |samples: &[f32], sink: &Sender<CaptureMessage>| {
        let pcm = to_mono_16k(samples, channels, source_rate);
        if pcm.is_empty() {
            return;
        }
        let mut bytes = Vec::with_capacity(pcm.len() * 2);
        for sample in pcm {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        // A closed receiver means the session ended; dropping is correct.
        let _ = sink.send(CaptureMessage::Pcm(bytes));
    };

    let stream = match sample_format {
        SampleFormat::F32 => device.build_input_stream(
            &stream_config,
            move |data: &[f32], _| emit(data, &sink),
            on_error,
            None,
        )?,
        SampleFormat::I16 => device.build_input_stream(
            &stream_config,
            move |data: &[i16], _| {
                let converted: Vec<f32> =
                    data.iter().map(|s| *s as f32 / i16::MAX as f32).collect();
                emit(&converted, &sink)
            },
            on_error,
            None,
        )?,
        SampleFormat::U16 => device.build_input_stream(
            &stream_config,
            move |data: &[u16], _| {
                let converted: Vec<f32> = data
                    .iter()
                    .map(|s| (*s as f32 / u16::MAX as f32) * 2.0 - 1.0)
                    .collect();
                emit(&converted, &sink)
            },
            on_error,
            None,
        )?,
        other => return Err(anyhow!("unsupported sample format: {other:?}")),
    };

    stream.play()?;
    Ok(stream)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stereo_is_mixed_to_mono() {
        // One channel hard left, one hard right: the mean is silence.
        let samples = vec![1.0, -1.0, 1.0, -1.0];
        assert!(to_mono_16k(&samples, 2, TARGET_RATE)
            .iter()
            .all(|s| *s == 0));
    }

    #[test]
    fn forty_eight_k_is_resampled_to_sixteen() {
        let samples = vec![0.5f32; 4800]; // 100 ms at 48 kHz mono
        let out = to_mono_16k(&samples, 1, 48_000);
        assert_eq!(out.len(), 1600); // 100 ms at 16 kHz
    }

    #[test]
    fn a_matching_rate_is_passed_through() {
        let samples = vec![0.25f32; 320];
        assert_eq!(to_mono_16k(&samples, 1, TARGET_RATE).len(), 320);
    }

    #[test]
    fn empty_input_yields_empty_output() {
        assert!(to_mono_16k(&[], 2, 48_000).is_empty());
    }

    #[test]
    fn default_output_leads_and_names_taken_by_an_input_are_dropped() {
        let existing = vec![AudioDevice {
            name: "Microphone (Realtek)".into(),
            is_loopback: false,
            is_default: true,
            is_native_system_audio: false,
        }];
        let entries = loopback_entries(
            &existing,
            vec![
                "Headphones (Realtek)".into(),
                "Microphone (Realtek)".into(), // collides with the input above
                "Speakers (Realtek)".into(),
            ],
            "Speakers (Realtek)",
        );

        let names: Vec<&str> = entries.iter().map(|d| d.name.as_str()).collect();
        assert_eq!(names, ["Speakers (Realtek)", "Headphones (Realtek)"]);
        // The setup screen offers the first loopback entry, so it must be the
        // device the call is actually playing through.
        assert!(entries[0].is_default);
        assert!(entries.iter().all(|d| d.is_loopback));
    }
}
