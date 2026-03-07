use std::ptr::{self, NonNull};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use block2::RcBlock;
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, NSObject};
use objc2::{define_class, sel, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSImage, NSMenu, NSMenuItem, NSStatusBar,
    NSStatusItem,
};
use objc2_avf_audio::{AVAudioEngine, AVAudioPCMBuffer, AVAudioTime};
use objc2_core_foundation::{kCFRunLoopCommonModes, CFMachPort, CFRunLoop};
use objc2_core_graphics::{
    CGEvent, CGEventMask, CGEventSource, CGEventSourceStateID, CGEventTapLocation,
    CGEventTapOptions, CGEventTapPlacement, CGEventTapProxy, CGEventType,
};
use objc2_foundation::{NSRunLoop, NSString};
use serde::Deserialize;
use tokio::runtime::Builder;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const NX_DEVICERCMDKEYMASK: u64 = 0x10;
const TARGET_SAMPLE_RATE: f64 = 16_000.0;
const BUILD_GIT_REV: &str = env!("VOX_DICTATION_GIT_REV");
const BUILD_STAMP: &str = env!("VOX_DICTATION_BUILD_STAMP");
const MIN_UTTERANCE_MS: u64 = 350;
const SPEECH_PEAK_THRESHOLD: f32 = 0.015;
const SPEECH_RMS_THRESHOLD: f32 = 0.004;
const SPEECH_HANGOVER_MS: u64 = 180;

static BACKEND_READY: AtomicBool = AtomicBool::new(false);
static IS_RECORDING: AtomicBool = AtomicBool::new(false);
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);
static VOICE_STARTED: AtomicBool = AtomicBool::new(false);
static SENT_SAMPLES: AtomicUsize = AtomicUsize::new(0);
static LAST_SPEECH_MS: AtomicU64 = AtomicU64::new(0);
static mut CONTROLLER: *mut Controller = std::ptr::null_mut();

#[derive(Parser, Debug)]
struct Args {
    #[arg(long)]
    server_url: String,

    #[arg(long, default_value_t = 0)]
    partial_interval_ms: u64,
}

#[derive(Clone, Copy)]
enum MainAction {
    Start,
    Stop,
}

#[derive(Debug)]
enum BackendCommand {
    Audio(Vec<i16>),
    Flush,
    Reset,
    Partial,
    Close,
}

#[derive(Deserialize)]
struct ServerMessage {
    status: Option<String>,
    text: Option<String>,
    is_partial: Option<bool>,
    error: Option<String>,
}

struct Controller {
    audio_engine: Retained<AVAudioEngine>,
    status_item: Retained<NSStatusItem>,
    backend_tx: UnboundedSender<BackendCommand>,
}

impl Controller {
    fn start_recording(&self, mtm: MainThreadMarker) {
        if IS_RECORDING.load(Ordering::SeqCst) {
            return;
        }
        if !BACKEND_READY.load(Ordering::SeqCst) {
            eprintln!("[vox-dictation] backend not ready yet");
            return;
        }

        eprintln!("[vox-dictation] recording started...");
        IS_RECORDING.store(true, Ordering::SeqCst);
        VOICE_STARTED.store(false, Ordering::SeqCst);
        SENT_SAMPLES.store(0, Ordering::SeqCst);
        LAST_SPEECH_MS.store(now_millis(), Ordering::SeqCst);
        set_status_icon(&self.status_item, true, mtm);
        let _ = self.backend_tx.send(BackendCommand::Reset);

        let microphone = unsafe { self.audio_engine.inputNode() };
        let backend_audio_tx = self.backend_tx.clone();
        let native_format = unsafe { microphone.outputFormatForBus(0) };
        let native_sample_rate = unsafe { native_format.sampleRate() as u32 };
        eprintln!("[vox-dictation] native sample rate: {}Hz", native_sample_rate);
        let tap_block = RcBlock::new(
            move |buffer: NonNull<AVAudioPCMBuffer>, _time: NonNull<AVAudioTime>| {
                if !IS_RECORDING.load(Ordering::SeqCst) {
                    return;
                }
                let buffer = unsafe { buffer.as_ref() };
                let frames = unsafe { buffer.frameLength() as usize };
                if let Some(ch0) = unsafe { buffer.floatChannelData().as_ref() } {
                    let ptr = ch0.as_ptr();
                    let slice = unsafe { std::slice::from_raw_parts(ptr, frames) };
                    let resampled = resample_linear(slice, native_sample_rate, TARGET_SAMPLE_RATE as u32);
                    let (peak, rms) = audio_levels(&resampled);
                    let is_speech = peak >= SPEECH_PEAK_THRESHOLD || rms >= SPEECH_RMS_THRESHOLD;
                    let now = now_millis();
                    if is_speech {
                        VOICE_STARTED.store(true, Ordering::SeqCst);
                        LAST_SPEECH_MS.store(now, Ordering::SeqCst);
                    }

                    let voice_started = VOICE_STARTED.load(Ordering::SeqCst);
                    let in_hangover = voice_started
                        && now.saturating_sub(LAST_SPEECH_MS.load(Ordering::SeqCst)) <= SPEECH_HANGOVER_MS;

                    if voice_started && (is_speech || in_hangover) {
                        let pcm = float_to_i16(&resampled);
                        SENT_SAMPLES.fetch_add(pcm.len(), Ordering::SeqCst);
                        let _ = backend_audio_tx.send(BackendCommand::Audio(pcm));
                    }
                }
            },
        );
        unsafe {
            microphone.installTapOnBus_bufferSize_format_block(
                0,
                1024,
                Some(&native_format),
                &*tap_block as *const _ as *mut _,
            );
        }

        unsafe { self.audio_engine.prepare() };
        if let Err(error) = unsafe { self.audio_engine.startAndReturnError() } {
            eprintln!("[vox-dictation] audio engine start error: {:?}", error);
            IS_RECORDING.store(false, Ordering::SeqCst);
            set_status_icon(&self.status_item, false, mtm);
            unsafe { microphone.removeTapOnBus(0) };
        }
    }

    fn stop_recording(&self, mtm: MainThreadMarker) {
        if !IS_RECORDING.swap(false, Ordering::SeqCst) {
            return;
        }

        eprintln!("[vox-dictation] recording stopped");
        set_status_icon(&self.status_item, false, mtm);

        let microphone = unsafe { self.audio_engine.inputNode() };
        unsafe { microphone.removeTapOnBus(0) };
        unsafe { self.audio_engine.stop() };
        let sent_samples = SENT_SAMPLES.load(Ordering::SeqCst);
        let min_samples = (TARGET_SAMPLE_RATE as usize * MIN_UTTERANCE_MS as usize) / 1000;
        let voice_started = VOICE_STARTED.load(Ordering::SeqCst);

        if !voice_started || sent_samples < min_samples {
            eprintln!("[vox-dictation] discarded short/quiet utterance");
            let _ = self.backend_tx.send(BackendCommand::Reset);
        } else {
            let _ = self.backend_tx.send(BackendCommand::Flush);
        }
    }
}

unsafe extern "C-unwind" fn event_tap_callback(
    _proxy: CGEventTapProxy,
    event_type: CGEventType,
    event: NonNull<CGEvent>,
    _user_info: *mut std::ffi::c_void,
) -> *mut CGEvent {
    if event_type == CGEventType::FlagsChanged {
        let flags = CGEvent::flags(Some(event.as_ref()));
        let device_flags = flags.0 & 0xFFFF;
        let right_cmd_pressed = (device_flags & NX_DEVICERCMDKEYMASK) != 0;
        static WAS_DOWN: AtomicBool = AtomicBool::new(false);
        let was_down = WAS_DOWN.load(Ordering::SeqCst);

        if right_cmd_pressed && !was_down {
            WAS_DOWN.store(true, Ordering::SeqCst);
            dispatch_action_on_main(MainAction::Start);
        } else if !right_cmd_pressed && was_down {
            WAS_DOWN.store(false, Ordering::SeqCst);
            dispatch_action_on_main(MainAction::Stop);
        }
    }
    event.as_ptr()
}

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "VoxDictationMenuDelegate"]
    #[derive(Debug, PartialEq)]
    struct MenuDelegate;

    #[allow(non_snake_case)]
    impl MenuDelegate {
        #[unsafe(method(quit:))]
        fn quit(&self, _sender: &AnyObject) {
            SHUTTING_DOWN.store(true, Ordering::SeqCst);
            std::process::exit(0);
        }
    }
);

fn set_status_icon(item: &NSStatusItem, recording: bool, mtm: MainThreadMarker) {
    let name = if recording { "mic.fill" } else { "mic" };
    if let Some(button) = item.button(mtm) {
        if let Some(image) = NSImage::imageWithSystemSymbolName_accessibilityDescription(
            &NSString::from_str(name),
            Some(&NSString::from_str("Vox Dictation")),
        ) {
            image.setTemplate(true);
            button.setImage(Some(&image));
        } else {
            button.setTitle(&NSString::from_str(if recording { "●" } else { "🎤" }));
        }
    }
}

fn dispatch_action_on_main(action: MainAction) {
    let run_loop = NSRunLoop::mainRunLoop();
    let block = RcBlock::new(move || unsafe {
        if CONTROLLER.is_null() {
            return;
        }
        let mtm = MainThreadMarker::new().expect("main thread marker");
        let controller = &*CONTROLLER;
        match action {
            MainAction::Start => controller.start_recording(mtm),
            MainAction::Stop => controller.stop_recording(mtm),
        }
    });
    unsafe {
        run_loop.performBlock(&block);
    }
    run_loop.getCFRunLoop().wake_up();
}

fn type_text(text: &str) {
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState);
    let utf16: Vec<u16> = text.encode_utf16().collect();
    for chunk in utf16.chunks(20) {
        let down = CGEvent::new_keyboard_event(source.as_deref(), 0, true);
        if let Some(ref ev) = down {
            unsafe {
                CGEvent::keyboard_set_unicode_string(Some(ev), chunk.len() as _, chunk.as_ptr());
            }
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(5));
        let up = CGEvent::new_keyboard_event(source.as_deref(), 0, false);
        if let Some(ref ev) = up {
            unsafe {
                CGEvent::keyboard_set_unicode_string(Some(ev), chunk.len() as _, chunk.as_ptr());
            }
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn float_to_i16(samples: &[f32]) -> Vec<i16> {
    samples
        .iter()
        .map(|sample| (sample.clamp(-1.0, 1.0) * i16::MAX as f32).round() as i16)
        .collect()
}

fn audio_levels(samples: &[f32]) -> (f32, f32) {
    if samples.is_empty() {
        return (0.0, 0.0);
    }

    let mut peak = 0.0f32;
    let mut power = 0.0f32;
    for sample in samples {
        let abs = sample.abs();
        if abs > peak {
            peak = abs;
        }
        power += sample * sample;
    }
    let rms = (power / samples.len() as f32).sqrt();
    (peak, rms)
}

fn now_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn resample_linear(samples: &[f32], source_hz: u32, target_hz: u32) -> Vec<f32> {
    if samples.is_empty() || source_hz == 0 || source_hz == target_hz {
        return samples.to_vec();
    }

    let ratio = source_hz as f64 / target_hz as f64;
    let out_len = ((samples.len() as f64) / ratio).max(1.0) as usize;
    (0..out_len)
        .map(|i| {
            let src = i as f64 * ratio;
            let idx = src as usize;
            let frac = src - idx as f64;
            let a = samples.get(idx).copied().unwrap_or(0.0);
            let b = samples.get(idx + 1).copied().unwrap_or(a);
            a + (b - a) * frac as f32
        })
        .collect()
}

fn encode_audio(samples: Vec<i16>) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        bytes.extend_from_slice(&sample.to_le_bytes());
    }
    bytes
}

fn spawn_backend_worker(server_url: String, partial_interval_ms: u64) -> UnboundedSender<BackendCommand> {
    let (cmd_tx, mut cmd_rx): (UnboundedSender<BackendCommand>, UnboundedReceiver<BackendCommand>) =
        unbounded_channel();
    let thread_cmd_tx = cmd_tx.clone();

    thread::spawn(move || {
        let runtime = Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("failed to build tokio runtime");

        runtime.block_on(async move {
            match connect_async(&server_url).await {
                Ok((ws_stream, _)) => {
                    let (mut write, mut read) = ws_stream.split();
                    BACKEND_READY.store(true, Ordering::SeqCst);
                    eprintln!("[vox-dictation] backend ready");

                    let writer_tx = thread_cmd_tx.clone();
                    let partial_thread = if partial_interval_ms > 0 {
                        Some(thread::spawn(move || {
                            let mut last = Instant::now();
                            while !SHUTTING_DOWN.load(Ordering::SeqCst) {
                                if IS_RECORDING.load(Ordering::SeqCst)
                                    && BACKEND_READY.load(Ordering::SeqCst)
                                    && last.elapsed() >= Duration::from_millis(partial_interval_ms)
                                {
                                    let _ = writer_tx.send(BackendCommand::Partial);
                                    last = Instant::now();
                                }
                                thread::sleep(Duration::from_millis(50));
                            }
                        }))
                    } else {
                        None
                    };

                    let reader = tokio::spawn(async move {
                        while let Some(message) = read.next().await {
                            match message {
                                Ok(Message::Text(text)) => {
                                    if let Ok(msg) = serde_json::from_str::<ServerMessage>(&text) {
                                        if let Some(error) = msg.error {
                                            eprintln!("[vox-dictation] backend error: {error}");
                                        } else if msg.status.as_deref() == Some("ready") {
                                            BACKEND_READY.store(true, Ordering::SeqCst);
                                        } else if let Some(text) = msg.text {
                                            if msg.is_partial.unwrap_or(false) {
                                                if !text.is_empty() {
                                                    eprintln!("[vox-dictation] partial: {text}");
                                                }
                                            } else if !text.trim().is_empty() {
                                                eprintln!("[vox-dictation] final: {text}");
                                                type_text(text.trim());
                                            }
                                        }
                                    }
                                }
                                Ok(Message::Close(_)) => break,
                                Ok(_) => {}
                                Err(error) => {
                                    eprintln!("[vox-dictation] backend read error: {error}");
                                    break;
                                }
                            }
                        }
                    });

                    while let Some(command) = cmd_rx.recv().await {
                        let send_result = match command {
                            BackendCommand::Audio(samples) => {
                                write.send(Message::Binary(encode_audio(samples).into())).await
                            }
                            BackendCommand::Flush => {
                                write.send(Message::Text("{\"action\":\"flush\"}".into())).await
                            }
                            BackendCommand::Reset => {
                                write.send(Message::Text("{\"action\":\"reset\"}".into())).await
                            }
                            BackendCommand::Partial => {
                                write.send(Message::Text("{\"action\":\"partial\"}".into())).await
                            }
                            BackendCommand::Close => {
                                let _ = write.send(Message::Text("{\"action\":\"close\"}".into())).await;
                                break;
                            }
                        };

                        if let Err(error) = send_result {
                            eprintln!("[vox-dictation] backend write error: {error}");
                            break;
                        }
                    }

                    BACKEND_READY.store(false, Ordering::SeqCst);
                    let _ = write.close().await;
                    let _ = reader.await;
                    if let Some(handle) = partial_thread {
                        let _ = handle.join();
                    }
                }
                Err(error) => {
                    eprintln!("[vox-dictation] backend connect error: {error}");
                }
            }
        });
    });

    cmd_tx
}

fn main() {
    let args = Args::parse();
    let mtm = MainThreadMarker::new().expect("must run on main thread");
    let app = NSApplication::sharedApplication(mtm);
    app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);

    let backend_tx = spawn_backend_worker(args.server_url.clone(), args.partial_interval_ms);

    let audio_engine = unsafe { AVAudioEngine::new() };
    let event_mask: CGEventMask = 1 << CGEventType::FlagsChanged.0;
    let tap = unsafe {
        CGEvent::tap_create(
            CGEventTapLocation::HIDEventTap,
            CGEventTapPlacement::HeadInsertEventTap,
            CGEventTapOptions::ListenOnly,
            event_mask,
            Some(event_tap_callback),
            ptr::null_mut(),
        )
    }
    .expect("failed to create event tap — grant Accessibility permission");

    let run_loop_source =
        CFMachPort::new_run_loop_source(None, Some(&tap), 0).expect("failed to create run loop source");
    unsafe {
        let run_loop = CFRunLoop::current().expect("no current run loop");
        run_loop.add_source(Some(&run_loop_source), kCFRunLoopCommonModes);
    }

    let status_bar = NSStatusBar::systemStatusBar();
    let status_item = status_bar.statusItemWithLength(-1.0);
    set_status_icon(&status_item, false, mtm);

    let delegate: Retained<MenuDelegate> = unsafe { objc2::msg_send![MenuDelegate::alloc(mtm), init] };
    let menu = NSMenu::new(mtm);
    let quit_item = unsafe {
        NSMenuItem::initWithTitle_action_keyEquivalent(
            NSMenuItem::alloc(mtm),
            &NSString::from_str("Quit"),
            Some(sel!(quit:)),
            &NSString::from_str("q"),
        )
    };
    unsafe { quit_item.setTarget(Some(&delegate)) };
    menu.addItem(&quit_item);
    status_item.setMenu(Some(&menu));

    let controller = Box::new(Controller {
        audio_engine,
        status_item,
        backend_tx: backend_tx.clone(),
    });
    unsafe {
        CONTROLLER = Box::into_raw(controller);
    }

    eprintln!(
        "[vox-dictation] ready v{} ({}, build {}) — hold right Command to dictate, release to type",
        env!("CARGO_PKG_VERSION"),
        BUILD_GIT_REV,
        BUILD_STAMP
    );

    app.run();

    SHUTTING_DOWN.store(true, Ordering::SeqCst);
    let _ = backend_tx.send(BackendCommand::Close);
}
