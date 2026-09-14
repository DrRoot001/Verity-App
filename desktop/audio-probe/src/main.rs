//! Standalone capture diagnostic.
//!
//! Verity has one failure that looks identical to every other failure: the HUD
//! sits there and nothing is transcribed. That can mean the device was never
//! opened, or it opened and delivers silence, or audio arrives and something
//! downstream drops it. This binary answers only the first two, with no API
//! key, no network, and none of the app's own code in the way, so a result
//! here is evidence about the machine rather than about Verity.
//!
//! Run it, play the interviewer's audio, and read the RMS column.
//!
//!   audio-probe            # test the default output device (loopback)
//!   audio-probe --list     # only enumerate
//!   audio-probe 3          # test device number 3 from the list

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat};

const SECONDS: u64 = 15;

/// Everything the probe can open, in the order it prints them.
struct Candidate {
    device: Device,
    name: String,
    /// Render endpoints are the system-audio tap on Windows.
    is_output: bool,
    is_default: bool,
}

fn collect() -> Vec<Candidate> {
    let host = cpal::default_host();
    let default_in = host
        .default_input_device()
        .and_then(|d| d.name().ok())
        .unwrap_or_default();
    let default_out = host
        .default_output_device()
        .and_then(|d| d.name().ok())
        .unwrap_or_default();

    let mut candidates = Vec::new();
    if let Ok(devices) = host.input_devices() {
        for device in devices {
            let Ok(name) = device.name() else { continue };
            let is_default = name == default_in;
            candidates.push(Candidate {
                device,
                name,
                is_output: false,
                is_default,
            });
        }
    }
    if let Ok(devices) = host.output_devices() {
        for device in devices {
            let Ok(name) = device.name() else { continue };
            let is_default = name == default_out;
            candidates.push(Candidate {
                device,
                name,
                is_output: true,
                is_default,
            });
        }
    }
    candidates
}

fn print_list(candidates: &[Candidate]) {
    println!("\n  #  KIND     DEFAULT  NAME");
    println!("  -  -------  -------  ----------------------------------------");
    for (i, c) in candidates.iter().enumerate() {
        println!(
            "  {:<2} {:<8} {:<8} {}",
            i,
            if c.is_output { "output" } else { "input" },
            if c.is_default { "yes" } else { "" },
            c.name
        );
    }
    println!(
        "\n  On Windows an 'output' row is captured in loopback mode: that is\n  \
         the interviewer's voice. Pick the one your call plays through.\n"
    );
}

/// Open `candidate` and report loudness once a second.
///
/// Returns the peak sample seen, so the caller can distinguish "opened but
/// silent" from "never opened" — the two produce very different advice.
fn measure(candidate: &Candidate) -> Result<i64, String> {
    // A render endpoint reports no *input* config, but WASAPI still captures
    // it at its output format once the loopback flag is set.
    let config = candidate
        .device
        .default_input_config()
        .or_else(|_| candidate.device.default_output_config())
        .map_err(|e| format!("no usable stream config: {e}"))?;

    println!(
        "opening  : {} ({} ch, {} Hz, {:?})",
        candidate.name,
        config.channels(),
        config.sample_rate().0,
        config.sample_format()
    );
    if candidate.is_output {
        println!("mode     : loopback (system audio)");
    }

    let sample_format = config.sample_format();
    let stream_config: cpal::StreamConfig = config.into();

    let sum_sq = Arc::new(AtomicU64::new(0));
    let count = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicU64::new(0));
    let callbacks = Arc::new(AtomicUsize::new(0));

    let (s, c, p, cb) = (
        sum_sq.clone(),
        count.clone(),
        peak.clone(),
        callbacks.clone(),
    );
    let accumulate = move |samples: &[f32]| {
        cb.fetch_add(1, Ordering::Relaxed);
        for &sample in samples {
            let v = (sample.clamp(-1.0, 1.0) * 32767.0) as i64;
            s.fetch_add((v * v) as u64, Ordering::Relaxed);
            p.fetch_max(v.unsigned_abs(), Ordering::Relaxed);
        }
        c.fetch_add(samples.len(), Ordering::Relaxed);
    };

    let on_error = |err| eprintln!("!! stream error: {err}");
    let stream = match sample_format {
        SampleFormat::F32 => candidate.device.build_input_stream(
            &stream_config,
            move |data: &[f32], _| accumulate(data),
            on_error,
            None,
        ),
        SampleFormat::I16 => candidate.device.build_input_stream(
            &stream_config,
            move |data: &[i16], _| {
                let converted: Vec<f32> =
                    data.iter().map(|s| *s as f32 / i16::MAX as f32).collect();
                accumulate(&converted)
            },
            on_error,
            None,
        ),
        other => return Err(format!("unsupported sample format {other:?}")),
    }
    .map_err(|e| format!("could not open the device: {e}"))?;

    stream.play().map_err(|e| format!("could not start: {e}"))?;

    println!("\nPlay the interviewer's audio now. Listening {SECONDS}s.\n");
    println!("  SEC  CALLBACKS  SAMPLES   RMS     LEVEL");
    let (mut last_count, mut last_calls) = (0usize, 0usize);
    for second in 1..=SECONDS {
        std::thread::sleep(Duration::from_secs(1));
        let n = count.load(Ordering::Relaxed);
        let calls = callbacks.load(Ordering::Relaxed);
        let fresh = n - last_count;
        let rms = if fresh == 0 {
            0.0
        } else {
            // Only this second's energy, so a loud moment is not averaged away.
            (sum_sq.swap(0, Ordering::Relaxed) as f64 / fresh as f64).sqrt()
        };
        let bars = ((rms / 3000.0 * 40.0) as usize).min(40);
        println!(
            "  {:<4} {:<10} {:<9} {:<7.0} {}",
            second,
            calls - last_calls,
            fresh,
            rms,
            "#".repeat(bars)
        );
        last_count = n;
        last_calls = calls;
    }

    drop(stream);
    Ok(peak.load(Ordering::Relaxed) as i64)
}

fn main() {
    println!("Verity audio probe — checks capture only. No key or network needed.");
    let candidates = collect();
    if candidates.is_empty() {
        println!("\nNo audio devices at all. Windows sees no sound hardware.");
        return;
    }
    print_list(&candidates);

    let arg = std::env::args().nth(1);
    if arg.as_deref() == Some("--list") {
        return;
    }

    let chosen = match arg.as_deref().map(str::parse::<usize>) {
        Some(Ok(i)) if i < candidates.len() => i,
        Some(Ok(i)) => {
            println!("No device {i}. Pick a number from the list above.");
            return;
        }
        Some(Err(_)) => {
            println!("Usage: audio-probe [--list | <device number>]");
            return;
        }
        // Default to the default *output*, the system-audio tap.
        None => match candidates
            .iter()
            .position(|c| c.is_output && c.is_default)
            .or_else(|| candidates.iter().position(|c| c.is_output))
        {
            Some(i) => i,
            None => {
                println!("No output device to capture. Pass a device number instead.");
                return;
            }
        },
    };

    match measure(&candidates[chosen]) {
        Ok(peak) if peak > 300 => {
            println!("\nRESULT: capture WORKS. Peak {peak} of 32767.");
            println!("Audio reaches this machine's capture layer, so a failure inside");
            println!("Verity with this same device selected is a bug above capture.");
        }
        Ok(peak) => {
            println!("\nRESULT: the device opened but delivered SILENCE (peak {peak}).");
            println!("If the interviewer really was talking, either the call is playing");
            println!("through a different device than the one tested, or loopback is not");
            println!("delivering data on this machine. Re-run with another number from");
            println!("the list — especially the device your headphones are on.");
        }
        Err(message) => {
            println!("\nRESULT: could not capture this device.\n  {message}");
            println!("Re-run with a different number from the list.");
        }
    }
}
