#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::collections::HashMap;
use std::io::Read;
use std::net::TcpStream;
use std::time::Duration;
use tauri::api::process::{Command, CommandChild};
use tauri::Manager;

const BACKEND_ADDR: &str = "127.0.0.1:8080";
// Must match the CSP connect-src in tauri.conf.json and the frontend's default.
const BACKEND_ORIGIN: &str = "http://localhost:8080";
const MAX_STARTUP_WAIT: Duration = Duration::from_secs(15);
const POLL_INTERVAL: Duration = Duration::from_millis(200);

struct Sidecar(std::sync::Mutex<Option<CommandChild>>);

/// 256 bits of OS entropy, hex-encoded.
///
/// The backend listens on localhost, so any page in the user's ordinary browser
/// can reach it while this app is open. Each launch therefore gets a fresh
/// bearer token shared only between the sidecar and this webview. Reading
/// entropy is required: a failure aborts startup rather than degrading to a
/// guessable token or running the API open.
fn generate_auth_token() -> String {
    let mut buf = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut buf))
        .expect("failed to read /dev/urandom for the backend auth token");
    buf.iter().map(|byte| format!("{:02x}", byte)).collect()
}

fn wait_for_backend() {
    let start = std::time::Instant::now();
    while start.elapsed() < MAX_STARTUP_WAIT {
        if TcpStream::connect_timeout(
            &BACKEND_ADDR.parse().unwrap(),
            Duration::from_millis(100),
        )
        .is_ok()
        {
            return;
        }
        std::thread::sleep(POLL_INTERVAL);
    }
    eprintln!("Warning: backend did not respond within {:?}", MAX_STARTUP_WAIT);
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let auth_token = generate_auth_token();

            let mut env = HashMap::new();
            env.insert("CYNITOR_AUTH_TOKEN".to_string(), auth_token.clone());

            let (mut rx, child) = Command::new_sidecar("cynitor-server")
                .expect("failed to locate cynitor-server sidecar")
                .envs(env)
                .spawn()
                .expect("failed to spawn cynitor-server");

            // Log sidecar stdout/stderr in the background.
            tauri::async_runtime::spawn(async move {
                use tauri::api::process::CommandEvent;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => eprintln!("[server] {}", line),
                        CommandEvent::Stderr(line) => eprintln!("[server] {}", line),
                        _ => {}
                    }
                }
            });

            app.manage(Sidecar(std::sync::Mutex::new(Some(child))));

            // Block briefly until the backend is listening, so the webview
            // doesn't flash a connection error on first load.
            wait_for_backend();

            // Hand the frontend its API base and token as an initialization
            // script rather than a post-load eval: this runs before any page
            // script, so the very first request is already authenticated and
            // the user never sees the token prompt.
            let init_script = format!(
                "window.__CYNITOR_API_BASE = {}; window.__CYNITOR_AUTH_TOKEN = {};",
                serde_json::to_string(BACKEND_ORIGIN)?,
                serde_json::to_string(&auth_token)?,
            );

            tauri::WindowBuilder::new(app, "main", tauri::WindowUrl::App("index.html".into()))
                .title("Cynitor")
                .inner_size(1280.0, 800.0)
                .min_inner_size(900.0, 600.0)
                .resizable(true)
                .initialization_script(&init_script)
                .build()?;

            Ok(())
        })
        .on_window_event(|event| {
            if let tauri::WindowEvent::Destroyed = event.event() {
                if let Some(sidecar) = event.window().try_state::<Sidecar>() {
                    if let Some(child) = sidecar.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
