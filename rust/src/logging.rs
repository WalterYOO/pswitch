//! 轻量文件日志：与 Python 版格式一致，1 MB 轮转、保留 3 份。

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

const MAX_BYTES: u64 = 1_000_000;
const BACKUPS: u32 = 3;

struct Sink {
    file: Option<File>,
    size: u64,
    path: PathBuf,
    foreground: bool,
    debug: bool,
}

static SINK: OnceLock<Mutex<Sink>> = OnceLock::new();

pub fn init(path: PathBuf, foreground: bool, debug: bool) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = OpenOptions::new().create(true).append(true).open(&path).ok();
    let size = file
        .as_ref()
        .and_then(|f| f.metadata().ok())
        .map(|m| m.len())
        .unwrap_or(0);
    let _ = SINK.set(Mutex::new(Sink {
        file,
        size,
        path,
        foreground,
        debug,
    }));
}

impl Sink {
    fn rotate(&mut self) {
        let base = self.path.clone();
        let _ = self.file.take();
        for i in (1..BACKUPS).rev() {
            let from = base.with_extension(format!("log.{}", i));
            let to = base.with_extension(format!("log.{}", i + 1));
            let _ = std::fs::rename(from, to);
        }
        let _ = std::fs::rename(&base, base.with_extension("log.1"));
        self.file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&base)
            .ok();
        self.size = 0;
    }
}

pub fn write(level: &str, args: std::fmt::Arguments) {
    let msg = format!(
        "{} {:<7} {}",
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S"),
        level,
        args
    );
    let Some(lock) = SINK.get() else {
        eprintln!("{}", msg);
        return;
    };
    let mut sink = lock.lock().unwrap_or_else(|e| e.into_inner());
    if level == "DEBUG" && !sink.debug {
        return;
    }
    if sink.size >= MAX_BYTES {
        sink.rotate();
    }
    match sink.file.as_mut() {
        Some(f) => {
            let _ = f.write_all(msg.as_bytes());
            let _ = f.write_all(b"\n");
            sink.size += msg.len() as u64 + 1;
        }
        None => eprintln!("{}", msg),
    }
    if sink.foreground {
        eprintln!("{}", msg);
    }
}

macro_rules! log_info {
    ($($arg:tt)*) => { $crate::logging::write("INFO", format_args!($($arg)*)) };
}

macro_rules! log_warn {
    ($($arg:tt)*) => { $crate::logging::write("WARNING", format_args!($($arg)*)) };
}

macro_rules! log_error {
    ($($arg:tt)*) => { $crate::logging::write("ERROR", format_args!($($arg)*)) };
}

macro_rules! log_debug {
    ($($arg:tt)*) => { $crate::logging::write("DEBUG", format_args!($($arg)*)) };
}
