#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::collections::HashMap;
use std::io::Read;
use std::net::TcpStream;
use std::time::Duration;
use tauri::api::process::Command;

const BACKEND_ADDR: &str = "127.0.0.1:8080";
// Must match the CSP connect-src in tauri.conf.json.
const BACKEND_ORIGIN: &str = "http://localhost:8080";
const MAX_STARTUP_WAIT: Duration = Duration::from_secs(15);
const POLL_INTERVAL: Duration = Duration::from_millis(200);

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

            // Tauri registers every spawned sidecar and kills it when the app
            // exits, so the child handle does not need to be kept around.
            let (mut rx, _child) = Command::new_sidecar("cynitor-server")
                .expect("failed to locate cynitor-server sidecar")
                .envs(HashMap::from([(
                    "CYNITOR_AUTH_TOKEN".to_string(),
                    auth_token.clone(),
                )]))
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

            // Block briefly until the backend is listening, so the webview
            // doesn't flash a connection error on first load.
            wait_for_backend();

            // Hand the frontend everything it needs to know about the shell as
            // one object, via an initialization script rather than a post-load
            // eval: it runs before any page script, so the very first request
            // is already authenticated and the user never sees a token prompt.
            let shell_config = serde_json::json!({
                "apiBase": BACKEND_ORIGIN,
                "authToken": auth_token,
            });
            let init_script = format!("window.__CYNITOR = {};", shell_config);

            tauri::WindowBuilder::new(app, "main", tauri::WindowUrl::App("index.html".into()))
                .title("Cynitor")
                .inner_size(1280.0, 800.0)
                .min_inner_size(900.0, 600.0)
                .initialization_script(&init_script)
                .build()?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
