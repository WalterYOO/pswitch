//! 双向字节管道，转发模式和 direct 模式共用。
//!
//! 用 64 KiB 缓冲做双向拷贝：loopback 上大缓冲能显著减少 syscall 次数，
//! 同时不会增加交互式流量的延迟（有数据就立即写出）。

use tokio::io::AsyncWriteExt;
use tokio::net::TcpStream;

pub const BUF_SIZE: usize = 64 * 1024;

/// 把两端对拷，直到两个方向都结束。`leftover` 是握手阶段多读出来的字节。
pub async fn relay(
    mut client: TcpStream,
    mut remote: TcpStream,
    client_leftover: Vec<u8>,
    remote_leftover: Vec<u8>,
) {
    if !client_leftover.is_empty() && remote.write_all(&client_leftover).await.is_err() {
        return;
    }
    if !remote_leftover.is_empty() && client.write_all(&remote_leftover).await.is_err() {
        return;
    }
    let _ = tokio::io::copy_bidirectional_with_sizes(
        &mut client,
        &mut remote,
        BUF_SIZE,
        BUF_SIZE,
    )
    .await;
}
