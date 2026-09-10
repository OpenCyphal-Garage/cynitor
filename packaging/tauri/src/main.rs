#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::collections::HashMap;
use std::io::Read;
use std::sync::Mutex;
use std::net::TcpStream;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const BACKEND_ADDR: &str = "127.0.0.1:8080";
// Must match the CSP connect-src in tauri.conf.json.
const BACKEND_ORIGIN: &str = "http://localhost:8080";
const MAX_STARTUP_WAIT: Duration = Duration::from_secs(15);
const POLL_INTERVAL: Duration = Duration::from_millis(200);

/// The spawned backend, so we can stop it ourselves on exit.
///
/// tauri-plugin-shell registers an exit handler that kills its children, but
/// it did not fire on window close here: the backend outlived the app and kept
/// holding port 8080, which is the exact failure the parent-death signal in
/// main.py was added to prevent. That signal cannot help either, because it
/// only fires when the PyInstaller bootloader dies — and nothing was killing
/// the bootloader. So the shell owns the lifetime explicitly.
struct Sidecar(Mutex<Option<CommandChild>>);

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

const USAGE: &str = "\
Cynitor — Cyphal network monitor

Usage: cynitor [OPTIONS]

Options:
      --gpu     Render through the graphics device instead of shared memory.
                Faster in principle, but produces a blank window wherever
                /dev/dri is unavailable: virtual machines, containers and
                remote desktops. Only worth trying on a local desktop.
  -h, --help    Show this message.

The backend is started automatically and stopped when the window closes.
Environment:
  WEBKIT_DISABLE_DMABUF_RENDERER   Set explicitly to override --gpu either way.
";

/// Choose how WebKit gets rendered frames onto the screen.
///
/// The default is shared memory. WebKit would otherwise pass frames between
/// its processes as graphics-memory handles, which needs /dev/dri. Where that
/// device is unavailable the allocation is refused and the renderer produces
/// no surface at all: the window opens and is never painted, with no error.
/// This dashboard is SVG shapes and text, so the graphics path accelerates
/// only compositing and buys nothing measurable — a poor trade for a blank
/// window.
///
/// An explicit environment variable always wins, so scripts and packagers can
/// override whatever the flag says.
fn configure_rendering(force_gpu: bool) {
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_some() {
        return;
    }
    std::env::set_var(
        "WEBKIT_DISABLE_DMABUF_RENDERER",
        if force_gpu { "0" } else { "1" },
    );
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "-h" || a == "--help") {
        print!("{}", USAGE);
        return;
    }
    if let Some(unknown) = args.iter().find(|a| a.as_str() != "--gpu") {
        eprintln!("cynitor: unrecognised option '{}'\n", unknown);
        eprint!("{}", USAGE);
        std::process::exit(2);
    }
    configure_rendering(args.iter().any(|a| a == "--gpu"));

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let auth_token = generate_auth_token();

            // Tauri registers every spawned sidecar and kills it when the app
            // exits, so the child handle does not need to be kept around.
            //
            // --no-frontend: this window serves the dashboard from its own
            // embedded copy over tauri://, so the backend's copy is never
            // reached. Serving it anyway would leave a second dashboard on
            // localhost:8080 that nobody can use — the page would load, but
            // the per-launch token is random and never shown, so no browser
            // could authenticate against it. The assets stay in the binary
            // regardless, because the same binary is what gets deployed
            // standalone to a server.
            let (mut rx, child) = app
                .shell()
                .sidecar("cynitor-server")
                .expect("failed to locate cynitor-server sidecar")
                .args(["--no-frontend"])
                .envs(HashMap::from([(
                    "CYNITOR_AUTH_TOKEN".to_string(),
                    auth_token.clone(),
                )]))
                .spawn()
                .expect("failed to spawn cynitor-server");

            app.manage(Sidecar(Mutex::new(Some(child))));

            // Log sidecar stdout/stderr in the background.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                            eprint!("[server] {}", String::from_utf8_lossy(&line));
                        }
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

            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                .title("Cynitor")
                .inner_size(1280.0, 800.0)
                .min_inner_size(900.0, 600.0)
                .initialization_script(&init_script)
                .build()?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(sidecar) = app.try_state::<Sidecar>() {
                    if let Some(child) = sidecar.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
