use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn main() {
    let git_rev = Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|out| out.status.success())
        .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".to_string());

    let build_stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    println!("cargo:rustc-env=VOX_DICTATION_GIT_REV={git_rev}");
    println!("cargo:rustc-env=VOX_DICTATION_BUILD_STAMP={build_stamp}");
}
