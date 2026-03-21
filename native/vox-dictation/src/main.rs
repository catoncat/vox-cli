use std::collections::VecDeque;
use std::ptr::{self, NonNull};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{LazyLock, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use block2::RcBlock;
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, NSObject};
use objc2::{define_class, sel, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSAutoresizingMaskOptions, NSBackingStoreType,
    NSColor, NSFont, NSGlassEffectView, NSGlassEffectViewStyle, NSImage, NSLineBreakMode, NSMenu,
    NSMenuItem, NSScreen, NSStatusBar, NSStatusItem, NSStatusWindowLevel, NSTextAlignment,
    NSTextField, NSView, NSWindow, NSWindowAnimationBehavior, NSWindowCollectionBehavior,
    NSWindowStyleMask, NSWorkspace,
};
use objc2_avf_audio::{AVAudioEngine, AVAudioPCMBuffer, AVAudioTime};
use objc2_core_foundation::{kCFRunLoopCommonModes, CFMachPort, CFRunLoop};
use objc2_core_graphics::{
    CGEvent, CGEventFlags, CGEventMask, CGEventSource, CGEventSourceStateID, CGEventTapLocation,
    CGEventTapOptions, CGEventTapPlacement, CGEventTapProxy, CGEventType,
};
use objc2_foundation::{NSPoint, NSRect, NSRunLoop, NSSize, NSString};
use serde::Deserialize;
use tokio::runtime::Builder;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const NX_DEVICERCMDKEYMASK: u64 = 0x10;
const TARGET_SAMPLE_RATE: f64 = 16_000.0;
const HELPER_VERSION: &str = concat!(
    env!("CARGO_PKG_VERSION"),
    " (",
    env!("VOX_DICTATION_GIT_REV"),
    ", build ",
    env!("VOX_DICTATION_BUILD_STAMP"),
    ")"
);
const MIN_UTTERANCE_MS: u64 = 350;
const SHORT_TAP_CANCEL_MS: u64 = 220;
const SPEECH_PEAK_THRESHOLD: f32 = 0.015;
const SPEECH_RMS_THRESHOLD: f32 = 0.004;
const SPEECH_HANGOVER_MS: u64 = 180;
const PRE_SPEECH_ROLL_MS: u64 = 220;
const KEEPALIVE_WARMUP_INTERVAL_MS: u64 = 30_000;
const KEYCODE_DELETE: u16 = 0x33;
const SYNTHETIC_INPUT_GRACE_MS: u64 = 250;
const SUBTITLE_HIDE_DELAY_MS: u64 = 420;
const FLUSH_WATCHDOG_TIMEOUT_MS: u64 = 12_000;
const SUBTITLE_BOTTOM_MARGIN: f64 = 36.0;
const SUBTITLE_WIDTH_RATIO: f64 = 0.74;
const SUBTITLE_MIN_WIDTH: f64 = 220.0;
const SUBTITLE_MAX_WIDTH: f64 = 1_160.0;
const SUBTITLE_MIN_HEIGHT: f64 = 60.0;
const SUBTITLE_MAX_HEIGHT: f64 = 220.0;
const SUBTITLE_HORIZONTAL_PADDING: f64 = 20.0;
const SUBTITLE_VERTICAL_PADDING: f64 = 14.0;
const SUBTITLE_LINE_HEIGHT: f64 = 36.0;
const SUBTITLE_FONT_SIZE: f64 = 29.0;
const SUBTITLE_FALLBACK_FILL_ALPHA: f64 = 0.82;
const SUBTITLE_FALLBACK_BORDER_ALPHA: f64 = 0.34;
const SUBTITLE_FALLBACK_SHADOW_ALPHA: f32 = 0.16;
const SUBTITLE_FALLBACK_SHADOW_RADIUS: f64 = 18.0;

static BACKEND_READY: AtomicBool = AtomicBool::new(false);
static IS_RECORDING: AtomicBool = AtomicBool::new(false);
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);
static VERBOSE: AtomicBool = AtomicBool::new(false);
static TYPE_PARTIAL: AtomicBool = AtomicBool::new(false);
static SHOW_SUBTITLE_OVERLAY: AtomicBool = AtomicBool::new(false);
static RIGHT_CMD_HELD: AtomicBool = AtomicBool::new(false);
static RIGHT_CMD_CHORD_ACTIVE: AtomicBool = AtomicBool::new(false);
static VOICE_STARTED: AtomicBool = AtomicBool::new(false);
static SENT_SAMPLES: AtomicUsize = AtomicUsize::new(0);
static LAST_SPEECH_MS: AtomicU64 = AtomicU64::new(0);
static RIGHT_CMD_PRESS_STARTED_MS: AtomicU64 = AtomicU64::new(0);
static NEXT_UTTERANCE_ID: AtomicU64 = AtomicU64::new(1);
static LAST_RECORDING_STARTED_MS: AtomicU64 = AtomicU64::new(0);
static LAST_FLUSH_SENT_MS: AtomicU64 = AtomicU64::new(0);
static LAST_FLUSH_UTTERANCE_ID: AtomicU64 = AtomicU64::new(0);
static LAST_FINAL_UTTERANCE_ID: AtomicU64 = AtomicU64::new(0);
static SYNTHETIC_INPUT_UNTIL_MS: AtomicU64 = AtomicU64::new(0);
static SUBTITLE_UPDATE_SEQ: AtomicU64 = AtomicU64::new(0);
static LAST_TYPED_PARTIAL: LazyLock<Mutex<String>> = LazyLock::new(|| Mutex::new(String::new()));
static mut CONTROLLER: *mut Controller = std::ptr::null_mut();

macro_rules! verbose_log {
    ($($arg:tt)*) => {
        if VERBOSE.load(Ordering::SeqCst) {
            eprintln!($($arg)*);
        }
    };
}

#[derive(Parser, Debug)]
#[command(version = HELPER_VERSION)]
struct Args {
    #[arg(long)]
    server_url: String,

    #[arg(long, default_value_t = 0)]
    partial_interval_ms: u64,

    #[arg(long, default_value_t = false, help = "Print verbose helper logs")]
    verbose: bool,

    #[arg(
        long,
        default_value_t = false,
        help = "Type partial transcripts into the focused input"
    )]
    type_partial: bool,

    #[arg(
        long,
        default_value_t = false,
        help = "Show live dictation subtitles in a bottom overlay"
    )]
    subtitle_overlay: bool,
}

#[derive(Clone, Copy)]
enum MainAction {
    Start,
    Stop,
    Cancel,
}

#[derive(Debug)]
enum BackendCommand {
    Audio(Vec<i16>),
    Flush { utterance_id: u64 },
    Reset,
    CaptureContext { reason: &'static str },
    Partial,
    Warmup { force: bool },
    Close,
}

#[derive(Deserialize)]
struct ServerTimings {
    audio_ms: Option<u64>,
    warmup_ms: Option<u64>,
    warmup_reason: Option<String>,
    infer_ms: Option<u64>,
    context_capture_ms: Option<u64>,
    context_available: Option<bool>,
    context_source: Option<String>,
    postprocess_ms: Option<u64>,
    llm_ms: Option<u64>,
    llm_timeout_sec: Option<f64>,
    llm_used: Option<bool>,
    llm_provider: Option<String>,
    llm_model: Option<String>,
    total_ms: Option<u64>,
    elapsed_ms: Option<u64>,
    reason: Option<String>,
}

#[derive(Deserialize)]
struct ServerMessage {
    status: Option<String>,
    text: Option<String>,
    is_partial: Option<bool>,
    error: Option<String>,
    utterance_id: Option<u64>,
    timings: Option<ServerTimings>,
}

struct Controller {
    audio_engine: Retained<AVAudioEngine>,
    status_item: Retained<NSStatusItem>,
    backend_tx: UnboundedSender<BackendCommand>,
    subtitle_overlay: Option<SubtitleOverlay>,
}

struct SubtitleOverlay {
    window: Retained<NSWindow>,
    glass: Retained<NSGlassEffectView>,
    content: Retained<NSView>,
    label: Retained<NSTextField>,
}

impl SubtitleOverlay {
    fn new(mtm: MainThreadMarker) -> Self {
        let frame = subtitle_window_frame(mtm, "");
        let window = unsafe {
            NSWindow::initWithContentRect_styleMask_backing_defer(
                NSWindow::alloc(mtm),
                frame,
                NSWindowStyleMask::Borderless,
                NSBackingStoreType::Buffered,
                false,
            )
        };
        let transparent = NSColor::clearColor();
        window.setBackgroundColor(Some(&transparent));
        window.setOpaque(false);
        window.setHasShadow(false);
        window.setIgnoresMouseEvents(true);
        window.setMovable(false);
        window.setMovableByWindowBackground(false);
        window.setCanHide(false);
        window.setHidesOnDeactivate(false);
        window.setExcludedFromWindowsMenu(true);
        window.setAnimationBehavior(NSWindowAnimationBehavior::None);
        window.setLevel(NSStatusWindowLevel);
        window.setCollectionBehavior(
            NSWindowCollectionBehavior::CanJoinAllSpaces
                | NSWindowCollectionBehavior::FullScreenAuxiliary
                | NSWindowCollectionBehavior::Transient
                | NSWindowCollectionBehavior::IgnoresCycle,
        );
        unsafe {
            window.setReleasedWhenClosed(false);
        }

        let glass = NSGlassEffectView::initWithFrame(NSGlassEffectView::alloc(mtm), frame);
        glass.setAutoresizingMask(
            NSAutoresizingMaskOptions::ViewWidthSizable
                | NSAutoresizingMaskOptions::ViewHeightSizable,
        );
        glass.setStyle(NSGlassEffectViewStyle::Regular);
        glass.setCornerRadius(subtitle_corner_radius(frame));

        let content = NSView::initWithFrame(NSView::alloc(mtm), subtitle_content_frame(frame));
        content.setAutoresizingMask(
            NSAutoresizingMaskOptions::ViewWidthSizable
                | NSAutoresizingMaskOptions::ViewHeightSizable,
        );
        content.setWantsLayer(true);

        let label = NSTextField::wrappingLabelWithString(&NSString::from_str(""), mtm);
        let font = NSFont::boldSystemFontOfSize(SUBTITLE_FONT_SIZE);
        label.setFrame(subtitle_label_frame(frame));
        label.setAutoresizingMask(
            NSAutoresizingMaskOptions::ViewWidthSizable
                | NSAutoresizingMaskOptions::ViewHeightSizable,
        );
        label.setAlignment(NSTextAlignment::Center);
        label.setLineBreakMode(NSLineBreakMode::ByWordWrapping);
        label.setMaximumNumberOfLines(5);
        label.setAllowsDefaultTighteningForTruncation(false);
        label.setUsesSingleLineMode(false);
        label.setBordered(false);
        label.setBezeled(false);
        label.setEditable(false);
        label.setSelectable(false);
        label.setDrawsBackground(false);
        label.setBackgroundColor(Some(&transparent));
        label.setFont(Some(&font));

        content.addSubview(&label);
        glass.setContentView(Some(&content));
        window.setContentView(Some(&glass));
        window.orderOut(None);

        let overlay = Self {
            window,
            glass,
            content,
            label,
        };
        overlay.apply_visual_style(frame);
        overlay
    }

    fn show_text(&self, mtm: MainThreadMarker, text: &str) {
        let normalized = text.trim();
        if normalized.is_empty() {
            self.hide();
            return;
        }
        let frame = subtitle_window_frame(mtm, normalized);
        self.window.setFrame_display(frame, false);
        self.apply_visual_style(frame);
        self.label.setStringValue(&NSString::from_str(normalized));
        self.window.orderFrontRegardless();
        self.window.displayIfNeeded();
    }

    fn hide(&self) {
        self.window.orderOut(None);
    }

    fn apply_visual_style(&self, frame: NSRect) {
        let radius = subtitle_corner_radius(frame);
        self.glass.setFrame(subtitle_content_frame(frame));
        self.glass.setCornerRadius(radius);
        self.content.setFrame(subtitle_content_frame(frame));
        self.label.setFrame(subtitle_label_frame(frame));

        if subtitle_should_reduce_transparency() {
            self.glass.setAlphaValue(0.0);
            let foreground = NSColor::colorWithCalibratedWhite_alpha(0.08, 0.96);
            self.label.setTextColor(Some(&foreground));
            let fill = NSColor::colorWithCalibratedWhite_alpha(0.98, SUBTITLE_FALLBACK_FILL_ALPHA);
            let border =
                NSColor::colorWithCalibratedWhite_alpha(1.0, SUBTITLE_FALLBACK_BORDER_ALPHA);
            let shadow = NSColor::colorWithCalibratedWhite_alpha(0.0, 0.8);
            if let Some(layer) = self.content.layer() {
                let fill_cg = fill.CGColor();
                let border_cg = border.CGColor();
                let shadow_cg = shadow.CGColor();
                layer.setMasksToBounds(false);
                layer.setBackgroundColor(Some(&fill_cg));
                layer.setCornerRadius(radius);
                layer.setBorderWidth(1.0);
                layer.setBorderColor(Some(&border_cg));
                layer.setShadowColor(Some(&shadow_cg));
                layer.setShadowOpacity(SUBTITLE_FALLBACK_SHADOW_ALPHA);
                layer.setShadowRadius(SUBTITLE_FALLBACK_SHADOW_RADIUS);
            }
            return;
        }

        self.glass.setAlphaValue(1.0);
        let tint = NSColor::colorWithCalibratedWhite_alpha(1.0, 0.07);
        self.glass.setTintColor(Some(&tint));
        let foreground = NSColor::colorWithCalibratedWhite_alpha(1.0, 0.97);
        self.label.setTextColor(Some(&foreground));
        if let Some(layer) = self.content.layer() {
            let clear = NSColor::clearColor().CGColor();
            layer.setMasksToBounds(false);
            layer.setBackgroundColor(Some(&clear));
            layer.setCornerRadius(radius);
            layer.setBorderWidth(0.0);
            layer.setBorderColor(None);
            layer.setShadowColor(None);
            layer.setShadowOpacity(0.0);
            layer.setShadowRadius(0.0);
        }
    }
}

impl Controller {
    fn update_subtitle(&self, mtm: MainThreadMarker, text: &str) {
        if let Some(overlay) = &self.subtitle_overlay {
            overlay.show_text(mtm, text);
        }
    }

    fn hide_subtitle(&self) {
        if let Some(overlay) = &self.subtitle_overlay {
            overlay.hide();
        }
    }

    fn start_recording(&self, mtm: MainThreadMarker) {
        if IS_RECORDING.load(Ordering::SeqCst) {
            return;
        }
        if !BACKEND_READY.load(Ordering::SeqCst) {
            eprintln!("[vox-dictation] backend not ready yet");
            return;
        }

        verbose_log!("[vox-dictation] recording started...");
        IS_RECORDING.store(true, Ordering::SeqCst);
        VOICE_STARTED.store(false, Ordering::SeqCst);
        SENT_SAMPLES.store(0, Ordering::SeqCst);
        LAST_SPEECH_MS.store(now_millis(), Ordering::SeqCst);
        LAST_RECORDING_STARTED_MS.store(now_millis(), Ordering::SeqCst);
        clear_partial_typing_state();
        if SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) {
            dispatch_subtitle_update("正在听…".to_string(), false);
        }
        set_status_icon(&self.status_item, true, mtm);
        if !queue_backend_command(&self.backend_tx, BackendCommand::Reset, "reset")
            || !queue_backend_command(
                &self.backend_tx,
                BackendCommand::CaptureContext { reason: "start" },
                "capture_context",
            )
            || !queue_backend_command(
                &self.backend_tx,
                BackendCommand::Warmup { force: false },
                "warmup",
            )
        {
            IS_RECORDING.store(false, Ordering::SeqCst);
            set_status_icon(&self.status_item, false, mtm);
            dispatch_subtitle_hide();
            return;
        }

        let microphone = unsafe { self.audio_engine.inputNode() };
        let backend_audio_tx = self.backend_tx.clone();
        let native_format = unsafe { microphone.outputFormatForBus(0) };
        let native_sample_rate = unsafe { native_format.sampleRate() as u32 };
        let preroll_limit = (TARGET_SAMPLE_RATE as usize * PRE_SPEECH_ROLL_MS as usize) / 1000;
        let preroll_buffer = std::sync::Mutex::new(VecDeque::<i16>::with_capacity(preroll_limit));
        verbose_log!(
            "[vox-dictation] native sample rate: {}Hz",
            native_sample_rate
        );
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
                    let resampled =
                        resample_linear(slice, native_sample_rate, TARGET_SAMPLE_RATE as u32);
                    let (peak, rms) = audio_levels(&resampled);
                    let is_speech = peak >= SPEECH_PEAK_THRESHOLD || rms >= SPEECH_RMS_THRESHOLD;
                    let now = now_millis();
                    let pcm = float_to_i16(&resampled);
                    let voice_started = VOICE_STARTED.load(Ordering::SeqCst);

                    if !voice_started {
                        let mut preroll = preroll_buffer.lock().expect("preroll mutex poisoned");
                        preroll.extend(pcm.iter().copied());
                        while preroll.len() > preroll_limit {
                            let _ = preroll.pop_front();
                        }

                        if is_speech {
                            VOICE_STARTED.store(true, Ordering::SeqCst);
                            LAST_SPEECH_MS.store(now, Ordering::SeqCst);
                            let initial_pcm: Vec<i16> = preroll.drain(..).collect();
                            let initial_len = initial_pcm.len();
                            drop(preroll);
                            verbose_log!(
                                "[vox-dictation] voice detected; sending preroll_ms={} peak={:.4} rms={:.4}",
                                (initial_len as u64 * 1000) / TARGET_SAMPLE_RATE as u64,
                                peak,
                                rms
                            );
                            if queue_backend_command(
                                &backend_audio_tx,
                                BackendCommand::Audio(initial_pcm),
                                "audio_preroll",
                            ) {
                                SENT_SAMPLES.fetch_add(initial_len, Ordering::SeqCst);
                            }
                        }
                        return;
                    }

                    if is_speech {
                        LAST_SPEECH_MS.store(now, Ordering::SeqCst);
                    }

                    let in_hangover = voice_started
                        && now.saturating_sub(LAST_SPEECH_MS.load(Ordering::SeqCst))
                            <= SPEECH_HANGOVER_MS;

                    if voice_started && (is_speech || in_hangover) {
                        let pcm_len = pcm.len();
                        if queue_backend_command(
                            &backend_audio_tx,
                            BackendCommand::Audio(pcm),
                            "audio_chunk",
                        ) {
                            SENT_SAMPLES.fetch_add(pcm_len, Ordering::SeqCst);
                        }
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
        let engine_started_at_ms = now_millis();
        if let Err(error) = unsafe { self.audio_engine.startAndReturnError() } {
            eprintln!("[vox-dictation] audio engine start error: {:?}", error);
            IS_RECORDING.store(false, Ordering::SeqCst);
            set_status_icon(&self.status_item, false, mtm);
            unsafe { microphone.removeTapOnBus(0) };
        } else {
            verbose_log!(
                "[vox-dictation] engine_start_ms={}",
                now_millis().saturating_sub(engine_started_at_ms)
            );
        }
    }

    fn finish_recording(&self, mtm: MainThreadMarker, flush_result: bool) {
        if !IS_RECORDING.swap(false, Ordering::SeqCst) {
            return;
        }

        if flush_result {
            verbose_log!("[vox-dictation] recording stopped");
        } else {
            verbose_log!("[vox-dictation] recording cancelled");
        }
        set_status_icon(&self.status_item, false, mtm);

        let microphone = unsafe { self.audio_engine.inputNode() };
        unsafe { microphone.removeTapOnBus(0) };
        unsafe { self.audio_engine.stop() };

        if !flush_result {
            clear_partial_typing_state();
            dispatch_subtitle_hide();
            let _ = self.backend_tx.send(BackendCommand::Reset);
            return;
        }

        let sent_samples = SENT_SAMPLES.load(Ordering::SeqCst);
        let min_samples = (TARGET_SAMPLE_RATE as usize * MIN_UTTERANCE_MS as usize) / 1000;
        let voice_started = VOICE_STARTED.load(Ordering::SeqCst);
        verbose_log!(
            "[vox-dictation] finish_recording flush_result={} voice_started={} sent_samples={} min_samples={}",
            flush_result,
            voice_started,
            sent_samples,
            min_samples
        );

        if !voice_started || sent_samples < min_samples {
            eprintln!("[vox-dictation] discarded short/quiet utterance");
            clear_partial_typing_state();
            dispatch_subtitle_hide();
            let _ = queue_backend_command(&self.backend_tx, BackendCommand::Reset, "reset");
        } else {
            if SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) || TYPE_PARTIAL.load(Ordering::SeqCst) {
                let _ = queue_backend_command(
                    &self.backend_tx,
                    BackendCommand::Partial,
                    "partial_on_stop",
                );
            }
            let utterance_id = NEXT_UTTERANCE_ID.fetch_add(1, Ordering::SeqCst);
            if queue_backend_command(
                &self.backend_tx,
                BackendCommand::Flush { utterance_id },
                "flush",
            ) {
                LAST_FLUSH_UTTERANCE_ID.store(utterance_id, Ordering::SeqCst);
                LAST_FLUSH_SENT_MS.store(now_millis(), Ordering::SeqCst);
                verbose_log!(
                    "[vox-dictation] flush queued utterance_id={} sent_samples={}",
                    utterance_id,
                    sent_samples
                );
                spawn_flush_watchdog(utterance_id);
            } else {
                clear_partial_typing_state();
                dispatch_subtitle_hide();
            }
        }
    }

    fn stop_recording(&self, mtm: MainThreadMarker) {
        self.finish_recording(mtm, true);
    }

    fn cancel_recording(&self, mtm: MainThreadMarker) {
        self.finish_recording(mtm, false);
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
        let was_down = RIGHT_CMD_HELD.load(Ordering::SeqCst);

        if right_cmd_pressed && !was_down {
            RIGHT_CMD_HELD.store(true, Ordering::SeqCst);
            RIGHT_CMD_CHORD_ACTIVE.store(false, Ordering::SeqCst);
            RIGHT_CMD_PRESS_STARTED_MS.store(now_millis(), Ordering::SeqCst);
            dispatch_action_on_main(MainAction::Start);
        } else if !right_cmd_pressed && was_down {
            RIGHT_CMD_HELD.store(false, Ordering::SeqCst);
            RIGHT_CMD_CHORD_ACTIVE.store(false, Ordering::SeqCst);
            if IS_RECORDING.load(Ordering::SeqCst) {
                let hold_ms =
                    now_millis().saturating_sub(RIGHT_CMD_PRESS_STARTED_MS.load(Ordering::SeqCst));
                if hold_ms < SHORT_TAP_CANCEL_MS && !VOICE_STARTED.load(Ordering::SeqCst) {
                    verbose_log!(
                        "[vox-dictation] short tap detected; cancelling recording hold_ms={}",
                        hold_ms
                    );
                    dispatch_action_on_main(MainAction::Cancel);
                } else {
                    dispatch_action_on_main(MainAction::Stop);
                }
            }
        }
    } else if event_type == CGEventType::KeyDown {
        if RIGHT_CMD_HELD.load(Ordering::SeqCst) {
            if now_millis() <= SYNTHETIC_INPUT_UNTIL_MS.load(Ordering::SeqCst) {
                return event.as_ptr();
            }
            RIGHT_CMD_CHORD_ACTIVE.store(true, Ordering::SeqCst);
            if IS_RECORDING.load(Ordering::SeqCst) {
                verbose_log!("[vox-dictation] right-command chord detected; cancelling dictation");
                dispatch_action_on_main(MainAction::Cancel);
            } else {
                verbose_log!("[vox-dictation] right-command chord detected; suppressing dictation");
            }
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
            MainAction::Cancel => controller.cancel_recording(mtm),
        }
    });
    unsafe {
        run_loop.performBlock(&block);
    }
    run_loop.getCFRunLoop().wake_up();
}

fn clear_partial_typing_state() {
    let mut state = LAST_TYPED_PARTIAL
        .lock()
        .expect("partial typing mutex poisoned");
    state.clear();
}

fn subtitle_text_units(text: &str) -> f64 {
    let units = text
        .chars()
        .map(|ch| {
            if ch.is_ascii_whitespace() {
                0.35
            } else if ch.is_ascii() {
                0.58
            } else {
                1.0
            }
        })
        .sum::<f64>();
    units.max(1.0)
}

fn subtitle_should_reduce_transparency() -> bool {
    NSWorkspace::sharedWorkspace().accessibilityDisplayShouldReduceTransparency()
}

fn subtitle_window_frame(mtm: MainThreadMarker, text: &str) -> NSRect {
    let default_rect = NSRect::new(
        NSPoint::new(120.0, 48.0),
        NSSize::new(320.0, SUBTITLE_MIN_HEIGHT),
    );
    let Some(screen) = NSScreen::mainScreen(mtm) else {
        return default_rect;
    };
    let visible = screen.visibleFrame();
    let max_available_width = (visible.size.width - 48.0).max(320.0);
    let max_width = (visible.size.width * SUBTITLE_WIDTH_RATIO)
        .max(SUBTITLE_MIN_WIDTH)
        .min(SUBTITLE_MAX_WIDTH)
        .min(max_available_width);
    let text_units = subtitle_text_units(text);
    let preferred_content_width = ((text_units + 8.0) * (SUBTITLE_FONT_SIZE * 0.94)).max(96.0);
    let width = (preferred_content_width + SUBTITLE_HORIZONTAL_PADDING * 2.0)
        .max(SUBTITLE_MIN_WIDTH)
        .min(max_width);
    let content_width = (width - SUBTITLE_HORIZONTAL_PADDING * 2.0).max(96.0);
    let units_per_line = (content_width / (SUBTITLE_FONT_SIZE * 0.62)).max(4.0);
    let line_count = ((text_units + 4.5) / units_per_line).ceil().clamp(1.0, 5.0);
    let height = (line_count * SUBTITLE_LINE_HEIGHT + SUBTITLE_VERTICAL_PADDING * 2.0)
        .max(SUBTITLE_MIN_HEIGHT)
        .min(SUBTITLE_MAX_HEIGHT);
    let x = visible.origin.x + ((visible.size.width - width).max(0.0) / 2.0);
    let y = visible.origin.y + SUBTITLE_BOTTOM_MARGIN;
    NSRect::new(NSPoint::new(x, y), NSSize::new(width, height))
}

fn subtitle_content_frame(window_frame: NSRect) -> NSRect {
    NSRect::new(
        NSPoint::new(0.0, 0.0),
        NSSize::new(window_frame.size.width, window_frame.size.height),
    )
}

fn subtitle_corner_radius(window_frame: NSRect) -> f64 {
    (window_frame.size.height * 0.5).clamp(22.0, 34.0)
}

fn subtitle_label_frame(window_frame: NSRect) -> NSRect {
    NSRect::new(
        NSPoint::new(SUBTITLE_HORIZONTAL_PADDING, SUBTITLE_VERTICAL_PADDING),
        NSSize::new(
            (window_frame.size.width - SUBTITLE_HORIZONTAL_PADDING * 2.0).max(120.0),
            (window_frame.size.height - SUBTITLE_VERTICAL_PADDING * 2.0).max(40.0),
        ),
    )
}

fn next_subtitle_sequence() -> u64 {
    SUBTITLE_UPDATE_SEQ
        .fetch_add(1, Ordering::SeqCst)
        .saturating_add(1)
}

fn dispatch_subtitle_update(text: String, final_result: bool) {
    if !SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) {
        return;
    }
    let sequence = next_subtitle_sequence();
    let run_loop = NSRunLoop::mainRunLoop();
    let block = RcBlock::new(move || unsafe {
        if CONTROLLER.is_null() {
            return;
        }
        let mtm = MainThreadMarker::new().expect("main thread marker");
        let controller = &*CONTROLLER;
        controller.update_subtitle(mtm, &text);
    });
    unsafe {
        run_loop.performBlock(&block);
    }
    run_loop.getCFRunLoop().wake_up();

    if final_result {
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(SUBTITLE_HIDE_DELAY_MS));
            if SHUTTING_DOWN.load(Ordering::SeqCst) {
                return;
            }
            if SUBTITLE_UPDATE_SEQ.load(Ordering::SeqCst) != sequence {
                return;
            }
            dispatch_subtitle_hide_if_current(sequence);
        });
    }
}

fn dispatch_subtitle_hide() {
    if !SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) {
        return;
    }
    let sequence = next_subtitle_sequence();
    dispatch_subtitle_hide_if_current(sequence);
}

fn dispatch_subtitle_hide_if_current(sequence: u64) {
    let run_loop = NSRunLoop::mainRunLoop();
    let block = RcBlock::new(move || unsafe {
        if CONTROLLER.is_null() {
            return;
        }
        if SUBTITLE_UPDATE_SEQ.load(Ordering::SeqCst) != sequence {
            return;
        }
        let controller = &*CONTROLLER;
        controller.hide_subtitle();
    });
    unsafe {
        run_loop.performBlock(&block);
    }
    run_loop.getCFRunLoop().wake_up();
}

fn queue_backend_command(
    tx: &UnboundedSender<BackendCommand>,
    command: BackendCommand,
    label: &str,
) -> bool {
    if let Err(error) = tx.send(command) {
        BACKEND_READY.store(false, Ordering::SeqCst);
        eprintln!(
            "[vox-dictation] backend command send failed action={} error={}",
            label, error
        );
        dispatch_subtitle_hide();
        return false;
    }
    true
}

fn spawn_flush_watchdog(utterance_id: u64) {
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(FLUSH_WATCHDOG_TIMEOUT_MS));
        if SHUTTING_DOWN.load(Ordering::SeqCst) {
            return;
        }
        if LAST_FLUSH_UTTERANCE_ID.load(Ordering::SeqCst) != utterance_id {
            return;
        }
        if LAST_FINAL_UTTERANCE_ID.load(Ordering::SeqCst) >= utterance_id {
            return;
        }
        eprintln!(
            "[vox-dictation] flush watchdog timeout utterance_id={} after_ms={}",
            utterance_id, FLUSH_WATCHDOG_TIMEOUT_MS
        );
        clear_partial_typing_state();
        dispatch_subtitle_hide();
    });
}

fn extend_synthetic_input_window(extra_ms: u64) {
    let deadline = now_millis().saturating_add(extra_ms);
    let mut current = SYNTHETIC_INPUT_UNTIL_MS.load(Ordering::SeqCst);
    while deadline > current {
        match SYNTHETIC_INPUT_UNTIL_MS.compare_exchange(
            current,
            deadline,
            Ordering::SeqCst,
            Ordering::SeqCst,
        ) {
            Ok(_) => break,
            Err(actual) => current = actual,
        }
    }
}

fn type_text(text: &str) {
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState);
    let utf16: Vec<u16> = text.encode_utf16().collect();
    let total_chunks = ((utf16.len() as u64) + 19) / 20;
    extend_synthetic_input_window(
        total_chunks
            .saturating_mul(20)
            .saturating_add(SYNTHETIC_INPUT_GRACE_MS),
    );
    for chunk in utf16.chunks(20) {
        let down = CGEvent::new_keyboard_event(source.as_deref(), 0, true);
        if let Some(ref ev) = down {
            CGEvent::set_flags(Some(ev), CGEventFlags(0));
            unsafe {
                CGEvent::keyboard_set_unicode_string(Some(ev), chunk.len() as _, chunk.as_ptr());
            }
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(5));
        let up = CGEvent::new_keyboard_event(source.as_deref(), 0, false);
        if let Some(ref ev) = up {
            CGEvent::set_flags(Some(ev), CGEventFlags(0));
            unsafe {
                CGEvent::keyboard_set_unicode_string(Some(ev), chunk.len() as _, chunk.as_ptr());
            }
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn press_backspace(count: usize) {
    if count == 0 {
        return;
    }
    let source = CGEventSource::new(CGEventSourceStateID::HIDSystemState);
    extend_synthetic_input_window(
        (count as u64)
            .saturating_mul(12)
            .saturating_add(SYNTHETIC_INPUT_GRACE_MS),
    );
    for _ in 0..count {
        let down = CGEvent::new_keyboard_event(source.as_deref(), KEYCODE_DELETE, true);
        if let Some(ref ev) = down {
            CGEvent::set_flags(Some(ev), CGEventFlags(0));
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(3));
        let up = CGEvent::new_keyboard_event(source.as_deref(), KEYCODE_DELETE, false);
        if let Some(ref ev) = up {
            CGEvent::set_flags(Some(ev), CGEventFlags(0));
            CGEvent::post(CGEventTapLocation::HIDEventTap, Some(ev));
        }
        thread::sleep(Duration::from_millis(6));
    }
}

fn shared_prefix_chars(left: &str, right: &str) -> usize {
    left.chars()
        .zip(right.chars())
        .take_while(|(a, b)| a == b)
        .count()
}

fn sync_partial_text(text: &str) -> (usize, usize, usize) {
    let normalized = text.trim();
    let mut state = LAST_TYPED_PARTIAL
        .lock()
        .expect("partial typing mutex poisoned");
    let prefix_chars = shared_prefix_chars(state.as_str(), normalized);
    let deleted_chars = state.chars().count().saturating_sub(prefix_chars);
    let appended_text: String = normalized.chars().skip(prefix_chars).collect();
    let appended_chars = appended_text.chars().count();

    if deleted_chars > 0 {
        press_backspace(deleted_chars);
    }
    if !appended_text.is_empty() {
        type_text(&appended_text);
    }

    state.clear();
    state.push_str(normalized);
    (prefix_chars, deleted_chars, appended_chars)
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

fn spawn_backend_worker(
    server_url: String,
    partial_interval_ms: u64,
) -> UnboundedSender<BackendCommand> {
    let (cmd_tx, mut cmd_rx): (
        UnboundedSender<BackendCommand>,
        UnboundedReceiver<BackendCommand>,
    ) = unbounded_channel();
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
                    verbose_log!("[vox-dictation] backend ready");

                    let writer_tx = thread_cmd_tx.clone();
                    let partial_thread = if partial_interval_ms > 0 {
                        Some(thread::spawn(move || {
                            let mut last = Instant::now();
                            while !SHUTTING_DOWN.load(Ordering::SeqCst) {
                                if IS_RECORDING.load(Ordering::SeqCst)
                                    && BACKEND_READY.load(Ordering::SeqCst)
                                    && last.elapsed() >= Duration::from_millis(partial_interval_ms)
                                {
                                    if !queue_backend_command(
                                        &writer_tx,
                                        BackendCommand::Partial,
                                        "partial",
                                    ) {
                                        break;
                                    }
                                    last = Instant::now();
                                }
                                thread::sleep(Duration::from_millis(50));
                            }
                        }))
                    } else {
                        None
                    };

                    let keepalive_tx = thread_cmd_tx.clone();
                    let keepalive_thread = thread::spawn(move || {
                        let mut last = Instant::now();
                        while !SHUTTING_DOWN.load(Ordering::SeqCst) {
                            if !IS_RECORDING.load(Ordering::SeqCst)
                                && BACKEND_READY.load(Ordering::SeqCst)
                                && last.elapsed() >= Duration::from_millis(KEEPALIVE_WARMUP_INTERVAL_MS)
                            {
                                if !queue_backend_command(
                                    &keepalive_tx,
                                    BackendCommand::Warmup { force: true },
                                    "warmup",
                                ) {
                                    break;
                                }
                                last = Instant::now();
                            }
                            thread::sleep(Duration::from_millis(250));
                        }
                    });

                    let reader = tokio::spawn(async move {
                        while let Some(message) = read.next().await {
                            match message {
                                Ok(Message::Text(text)) => {
                                    if let Ok(msg) = serde_json::from_str::<ServerMessage>(&text) {
                                        if let Some(error) = msg.error {
                                            eprintln!("[vox-dictation] backend error: {error}");
                                        } else if msg.status.as_deref() == Some("ready") {
                                            BACKEND_READY.store(true, Ordering::SeqCst);
                                        } else if matches!(msg.status.as_deref(), Some("warmed") | Some("noop")) {
                                            if let Some(timings) = msg.timings {
                                                verbose_log!(
                                                    "[vox-dictation] backend_warmup status={} elapsed_ms={} reason={}",
                                                    msg.status.unwrap_or_else(|| "-".to_string()),
                                                    timings.elapsed_ms.unwrap_or(0),
                                                    timings.reason.unwrap_or_else(|| "-".to_string()),
                                                );
                                            }
                                        } else if let Some(text) = msg.text {
                                            if msg.is_partial.unwrap_or(false) {
                                                if !text.is_empty() {
                                                    verbose_log!("[vox-dictation] partial: {text}");
                                                    if SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) {
                                                        let normalized = text.trim();
                                                        if !normalized.is_empty() {
                                                            dispatch_subtitle_update(normalized.to_string(), false);
                                                        }
                                                    }
                                                    if TYPE_PARTIAL.load(Ordering::SeqCst) {
                                                        let normalized = text.trim();
                                                        if !normalized.is_empty() {
                                                            let type_started_at_ms = now_millis();
                                                            let (prefix_chars, deleted_chars, appended_chars) =
                                                                sync_partial_text(normalized);
                                                            let type_elapsed_ms = now_millis()
                                                                .saturating_sub(type_started_at_ms);
                                                            if deleted_chars > 0 || appended_chars > 0 {
                                                                verbose_log!(
                                                                    "[vox-dictation] partial_typed chars={} prefix_chars={} deleted_chars={} appended_chars={} type_ms={}",
                                                                    normalized.chars().count(),
                                                                    prefix_chars,
                                                                    deleted_chars,
                                                                    appended_chars,
                                                                    type_elapsed_ms
                                                                );
                                                            }
                                                        }
                                                    }
                                                }
                                            } else if !text.trim().is_empty() {
                                                let received_at_ms = now_millis();
                                                let flush_utterance_id = LAST_FLUSH_UTTERANCE_ID.load(Ordering::SeqCst);
                                                let flush_sent_at_ms = LAST_FLUSH_SENT_MS.load(Ordering::SeqCst);
                                                let recording_started_at_ms =
                                                    LAST_RECORDING_STARTED_MS.load(Ordering::SeqCst);
                                                let utterance_id = msg.utterance_id.unwrap_or(0);
                                                let final_text = text.trim();
                                                if utterance_id > 0 {
                                                    LAST_FINAL_UTTERANCE_ID.store(
                                                        utterance_id,
                                                        Ordering::SeqCst,
                                                    );
                                                }
                                                if SHOW_SUBTITLE_OVERLAY.load(Ordering::SeqCst) {
                                                    dispatch_subtitle_update(final_text.to_string(), true);
                                                }
                                                verbose_log!("[vox-dictation] final: {text}");
                                                let type_started_at_ms = now_millis();
                                                type_text(final_text);
                                                let type_elapsed_ms =
                                                    now_millis().saturating_sub(type_started_at_ms);
                                                clear_partial_typing_state();
                                                if let Some(timings) = msg.timings {
                                                    let flush_roundtrip_ms = if utterance_id == flush_utterance_id {
                                                        received_at_ms.saturating_sub(flush_sent_at_ms)
                                                    } else {
                                                        0
                                                    };
                                                    let capture_ms = if utterance_id == flush_utterance_id {
                                                        flush_sent_at_ms.saturating_sub(recording_started_at_ms)
                                                    } else {
                                                        0
                                                    };
                                                    verbose_log!(
                                                        "[vox-dictation] timings utterance_id={} capture_ms={} flush_roundtrip_ms={} audio_ms={} warmup_ms={} infer_ms={} context_capture_ms={} context_available={} context_source={} postprocess_ms={} llm_ms={} llm_used={} llm_timeout_sec={} llm_provider={} llm_model={} backend_total_ms={} type_ms={} warmup_reason={}",
                                                        utterance_id,
                                                        capture_ms,
                                                        flush_roundtrip_ms,
                                                        timings.audio_ms.unwrap_or(0),
                                                        timings.warmup_ms.unwrap_or(0),
                                                        timings.infer_ms.unwrap_or(0),
                                                        timings.context_capture_ms.unwrap_or(0),
                                                        timings.context_available.unwrap_or(false),
                                                        timings.context_source.unwrap_or_else(|| "-".to_string()),
                                                        timings.postprocess_ms.unwrap_or(0),
                                                        timings.llm_ms.unwrap_or(0),
                                                        timings.llm_used.unwrap_or(false),
                                                        timings.llm_timeout_sec.unwrap_or(0.0),
                                                        timings.llm_provider.unwrap_or_else(|| "-".to_string()),
                                                        timings.llm_model.unwrap_or_else(|| "-".to_string()),
                                                        timings.total_ms.unwrap_or(0),
                                                        type_elapsed_ms,
                                                        timings.warmup_reason.unwrap_or_else(|| "-".to_string()),
                                                    );
                                                }
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
                        BACKEND_READY.store(false, Ordering::SeqCst);
                        clear_partial_typing_state();
                        dispatch_subtitle_hide();
                    });

                    while let Some(command) = cmd_rx.recv().await {
                        let send_result = match command {
                            BackendCommand::Audio(samples) => {
                                write.send(Message::Binary(encode_audio(samples).into())).await
                            }
                            BackendCommand::Flush { utterance_id } => {
                                let payload =
                                    format!("{{\"action\":\"flush\",\"utterance_id\":{}}}", utterance_id);
                                write.send(Message::Text(payload.into())).await
                            }
                            BackendCommand::Reset => {
                                write.send(Message::Text("{\"action\":\"reset\"}".into())).await
                            }
                            BackendCommand::CaptureContext { reason } => {
                                let payload = format!(
                                    "{{\"action\":\"capture_context\",\"reason\":\"{}\"}}",
                                    reason
                                );
                                write.send(Message::Text(payload.into())).await
                            }
                            BackendCommand::Partial => {
                                write.send(Message::Text("{\"action\":\"partial\"}".into())).await
                            }
                            BackendCommand::Warmup { force } => {
                                let payload = if force {
                                    "{\"action\":\"warmup\",\"force\":true}"
                                } else {
                                    "{\"action\":\"warmup\"}"
                                };
                                write.send(Message::Text(payload.into())).await
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
                    let _ = keepalive_thread.join();
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
    VERBOSE.store(args.verbose, Ordering::SeqCst);
    TYPE_PARTIAL.store(args.type_partial, Ordering::SeqCst);
    SHOW_SUBTITLE_OVERLAY.store(args.subtitle_overlay, Ordering::SeqCst);
    let mtm = MainThreadMarker::new().expect("must run on main thread");
    let app = NSApplication::sharedApplication(mtm);
    app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);

    let backend_tx = spawn_backend_worker(args.server_url.clone(), args.partial_interval_ms);

    let audio_engine = unsafe { AVAudioEngine::new() };
    let event_mask: CGEventMask =
        (1 << CGEventType::FlagsChanged.0) | (1 << CGEventType::KeyDown.0);
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

    let run_loop_source = CFMachPort::new_run_loop_source(None, Some(&tap), 0)
        .expect("failed to create run loop source");
    unsafe {
        let run_loop = CFRunLoop::current().expect("no current run loop");
        run_loop.add_source(Some(&run_loop_source), kCFRunLoopCommonModes);
    }

    let status_bar = NSStatusBar::systemStatusBar();
    let status_item = status_bar.statusItemWithLength(-1.0);
    set_status_icon(&status_item, false, mtm);

    let delegate: Retained<MenuDelegate> =
        unsafe { objc2::msg_send![MenuDelegate::alloc(mtm), init] };
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
        subtitle_overlay: if args.subtitle_overlay {
            Some(SubtitleOverlay::new(mtm))
        } else {
            None
        },
    });
    unsafe {
        CONTROLLER = Box::into_raw(controller);
    }

    if args.subtitle_overlay {
        if subtitle_should_reduce_transparency() {
            eprintln!(
                "[vox-dictation] subtitle overlay enabled (fallback style: macOS Reduce Transparency is on)"
            );
        } else {
            eprintln!("[vox-dictation] subtitle overlay enabled (glass style)");
        }
    }

    eprintln!(
        "[vox-dictation] ready v{} - hold right Command alone to dictate, release to type{}",
        HELPER_VERSION,
        if args.subtitle_overlay {
            "; live subtitles appear at the bottom"
        } else {
            ""
        },
    );

    app.run();

    SHUTTING_DOWN.store(true, Ordering::SeqCst);
    let _ = backend_tx.send(BackendCommand::Close);
}
