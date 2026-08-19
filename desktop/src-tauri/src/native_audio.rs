//! Native macOS 13+ system-audio capture via ScreenCaptureKit.
//!
//! No virtual loopback device needed here: ScreenCaptureKit taps system
//! audio directly. It's asked for 16 kHz mono up front — a value the
//! framework natively supports (see its `AudioSampleRate`/`AudioChannelCount`
//! docs) — so this path needs no resampler, unlike the cpal/loopback path in
//! `audio.rs`.
//!
//! Mirrors `audio::start`'s [`CaptureMessage`] contract exactly (PCM or a
//! fatal error, "drop the handle to stop") so `session.rs` and `main.rs`
//! don't need to know which backend produced the bytes. Only entry point
//! this module needs to expose is [`start`]; only caller is
//! `main.rs::start_listening`, gated on `platform::detect().native_system_audio`.
//!
//! Requires macOS 13+ (the `screencapturekit` crate's `macos_13_0` feature)
//! and Screen Recording permission — ScreenCaptureKit is how Apple gates
//! system-audio capture, there is no separate microphone-style API for it.

use std::sync::mpsc::Sender;

use anyhow::{anyhow, Result};
use screencapturekit::prelude::*;

use crate::audio::{to_i16, CaptureMessage, TARGET_RATE};

/// Handle to a running native capture. Dropping it stops the stream.
pub struct NativeCapture {
    stream: SCStream,
}

impl Drop for NativeCapture {
    fn drop(&mut self) {
        // Best-effort: the process may already be tearing down.
        let _ = self.stream.stop_capture();
    }
}

struct AudioHandler {
    sink: Sender<CaptureMessage>,
}

impl SCStreamOutputTrait for AudioHandler {
    fn did_output_sample_buffer(&self, sample: CMSampleBuffer, of_type: SCStreamOutputType) {
        if of_type != SCStreamOutputType::Audio {
            return;
        }
        let Some(buffers) = sample.audio_buffer_list() else {
            return;
        };
        let Some(format) = sample.format_description() else {
            return;
        };
        let is_float = format.audio_is_float();
        let bits_per_channel = format.audio_bits_per_channel().unwrap_or(16);
        for buffer in &buffers {
            let pcm16 = to_pcm16(buffer.data(), is_float, bits_per_channel);
            if !pcm16.is_empty() {
                let _ = self.sink.send(CaptureMessage::Pcm(pcm16));
            }
        }
    }
}

/// Converts one delivered audio buffer to little-endian PCM16.
///
/// Apple documents Float32 as ScreenCaptureKit's typical delivery format,
/// but the actual format is read from the sample's own
/// `CMFormatDescription` rather than assumed, so an OS change to this
/// degrades to silently dropping frames (caught by the empty-vec check at
/// the call site) instead of emitting corrupted audio.
fn to_pcm16(bytes: &[u8], is_float: bool, bits_per_channel: u32) -> Vec<u8> {
    if is_float && bits_per_channel == 32 {
        bytes
            .chunks_exact(4)
            .flat_map(|chunk| {
                let sample = f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
                to_i16(sample).to_le_bytes()
            })
            .collect()
    } else if !is_float && bits_per_channel == 16 {
        bytes.to_vec()
    } else {
        Vec::new()
    }
}

/// Start native system-audio capture, sending 16 kHz mono PCM16 chunks to
/// `sink`. Synchronous like `audio::start`: `SCShareableContent::get()` is a
/// blocking FFI call, not an async one.
pub fn start(sink: Sender<CaptureMessage>) -> Result<NativeCapture> {
    let content = SCShareableContent::get().map_err(|error| {
        anyhow!("Could not access system audio (Screen Recording permission required): {error}")
    })?;
    let display = content
        .displays()
        .first()
        .ok_or_else(|| anyhow!("No display is available to anchor system audio capture."))?;

    let filter = SCContentFilter::create()
        .with_display(display)
        .with_excluding_windows(&[])
        .build();

    let config = SCStreamConfiguration::new()
        .with_captures_audio(true)
        .with_sample_rate(TARGET_RATE as i32)
        .with_channel_count(1)
        .with_excludes_current_process_audio(true);

    let error_sink = sink.clone();
    let delegate = ErrorHandler::new(move |error| {
        let _ = error_sink.send(CaptureMessage::Error(error.to_string()));
    });

    let mut stream = SCStream::new_with_delegate(&filter, &config, delegate);
    stream.add_output_handler(AudioHandler { sink }, SCStreamOutputType::Audio);
    stream
        .start_capture()
        .map_err(|error| anyhow!("Could not start system audio capture: {error}"))?;

    Ok(NativeCapture { stream })
}

/// Makes one read-only ScreenCaptureKit call so the OS shows the Screen
/// Recording permission prompt at first launch, without opening a stream.
/// Used only by `main.rs`'s first-run permission priming.
pub fn prime_permission() -> Result<()> {
    SCShareableContent::get()
        .map(|_| ())
        .map_err(|error| anyhow!("Could not prime screen-recording permission: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float32_samples_convert_to_pcm16() {
        let samples: [f32; 2] = [1.0, -1.0];
        let bytes: Vec<u8> = samples.iter().flat_map(|s| s.to_le_bytes()).collect();
        let pcm16 = to_pcm16(&bytes, true, 32);
        assert_eq!(pcm16.len(), 4);
        assert_eq!(i16::from_le_bytes([pcm16[0], pcm16[1]]), i16::MAX);
    }

    #[test]
    fn int16_samples_pass_through_unchanged() {
        let bytes = [1u8, 2, 3, 4];
        assert_eq!(to_pcm16(&bytes, false, 16), bytes.to_vec());
    }

    #[test]
    fn unrecognised_format_drops_the_buffer_instead_of_emitting_garbage() {
        assert!(to_pcm16(&[1, 2, 3, 4], true, 64).is_empty());
    }
}
