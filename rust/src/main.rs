#[macro_use]
mod logging;

mod cli;
mod completion;
mod config;
mod direct;
mod launchd;
mod probe;
mod proxy;
mod relay;
mod system_proxy;
mod util;

fn main() -> std::process::ExitCode {
    std::process::ExitCode::from(cli::run())
}
