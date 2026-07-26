use std::{
    io::{self, BufWriter, Write},
    path::PathBuf,
    time::Duration,
};

use axum::body::{Body, Bytes};
use futures_util::stream;
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::{
    error::{AppError, AppResult},
    models::{JobResultPage, SharedState},
    services::{query_engine, resource_control},
};

/// 根据不可伪造的任务产物键构造数据卷路径，阻止数据库异常值形成目录穿越。
pub fn artifact_path(state: &SharedState, artifact_key: &str) -> AppResult<PathBuf> {
    Uuid::parse_str(artifact_key).map_err(|_| AppError::Internal("后台结果标识无效".to_owned()))?;
    Ok(state
        .data_dir
        .join("job-results")
        .join(format!("{artifact_key}.duckdb")))
}

/// 在查询并发池内读取一页持久化结果，避免多个大分页请求绕过单机资源限制。
pub async fn load_page(
    state: &SharedState,
    artifact_key: &str,
    offset: usize,
    limit: usize,
) -> AppResult<JobResultPage> {
    let path = artifact_path(state, artifact_key)?;
    if !path.exists() {
        return Err(AppError::NotFound("后台结果文件不存在或已过期".to_owned()));
    }
    let permit = resource_control::acquire_permit(
        state.query_semaphore.clone(),
        state.resource_queue_timeout_seconds,
        "结果读取器",
    )
    .await?;
    let runtime = state.query_runtime.clone();
    let work_root = state.data_dir.join("query-work");
    let handle = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        query_engine::read_artifact_page(&path, offset, limit, &runtime, &work_root)
    });
    let (response, total_rows) =
        tokio::time::timeout(Duration::from_secs(state.query_timeout_seconds), handle)
            .await
            .map_err(|_| AppError::Timeout("后台结果分页读取超时".to_owned()))?
            .map_err(|error| AppError::Internal(format!("结果读取线程异常: {error}")))?
            .map_err(|error| AppError::Internal(error.to_string()))?;
    Ok(JobResultPage {
        columns: response.columns,
        rows: response.rows,
        row_count: response.row_count,
        total_rows,
        offset,
        limit,
        elapsed_ms: response.elapsed_ms,
        truncated: response.truncated,
    })
}

/// 创建 CSV 响应体并在阻塞线程中逐批发送数据，客户端断开后写入会立即失败并停止扫描。
pub async fn csv_body(state: &SharedState, artifact_key: &str) -> AppResult<Body> {
    let path = artifact_path(state, artifact_key)?;
    if !path.exists() {
        return Err(AppError::NotFound("后台结果文件不存在或已过期".to_owned()));
    }
    let permit = resource_control::acquire_permit(
        state.query_semaphore.clone(),
        state.resource_queue_timeout_seconds,
        "结果导出器",
    )
    .await?;
    let runtime = state.query_runtime.clone();
    let (sender, receiver) = mpsc::channel::<Result<Bytes, io::Error>>(8);
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let sink = ChannelWriter {
            sender: sender.clone(),
        };
        let output = BufWriter::with_capacity(64 * 1024, sink);
        if let Err(error) = query_engine::write_artifact_csv(&path, &runtime, output) {
            let _ = sender.blocking_send(Err(io::Error::other(error.to_string())));
        }
    });
    let stream = stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|item| (item, receiver))
    });
    Ok(Body::from_stream(stream))
}

/// 删除任务产物；不存在视为成功，保证任务记录删除和过期清理可以安全重试。
pub async fn remove_artifact(state: &SharedState, artifact_key: &str) -> AppResult<()> {
    let path = artifact_path(state, artifact_key)?;
    match tokio::fs::remove_file(path).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

struct ChannelWriter {
    sender: mpsc::Sender<Result<Bytes, io::Error>>,
}

impl Write for ChannelWriter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        self.sender
            .blocking_send(Ok(Bytes::copy_from_slice(buffer)))
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "下载连接已关闭"))?;
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
