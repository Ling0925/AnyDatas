use std::{sync::Arc, time::Duration};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::{
    error::{AppError, AppResult},
    models::SharedState,
};

/// 在有界时间内取得资源许可，避免高峰期请求无限堆积并持续占用连接。
///
/// 返回拥有型许可后可以把它移动进阻塞线程，调用方即使超时返回，真实工作结束前
/// 并发名额也不会被提前释放。
pub async fn acquire_permit(
    semaphore: Arc<Semaphore>,
    wait_seconds: u64,
    resource_name: &str,
) -> AppResult<OwnedSemaphorePermit> {
    tokio::time::timeout(Duration::from_secs(wait_seconds), semaphore.acquire_owned())
        .await
        .map_err(|_| {
            AppError::Unavailable(format!("{resource_name}当前繁忙，等待资源超时，请稍后重试"))
        })?
        .map_err(|_| AppError::Unavailable(format!("{resource_name}已停止接收新任务")))
}

/// 在文件解析并发池中运行阻塞任务，统一处理排队、线程异常和超时。
///
/// Calamine 解析大型 Excel 时无法安全强制终止，因此许可由阻塞线程持有；
/// 即便 HTTP 已超时返回，也不会因重试而突破单机配置的最大并发。
pub async fn run_file_task<T, F>(
    state: &SharedState,
    operation: &'static str,
    task: F,
) -> AppResult<T>
where
    T: Send + 'static,
    F: FnOnce() -> anyhow::Result<T> + Send + 'static,
{
    let permit = acquire_permit(
        state.file_parse_semaphore.clone(),
        state.resource_queue_timeout_seconds,
        "文件解析器",
    )
    .await?;
    let handle = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        task()
    });
    tokio::time::timeout(
        Duration::from_secs(state.file_parse_timeout_seconds),
        handle,
    )
    .await
    .map_err(|_| AppError::Timeout(format!("{operation}超时，服务器已限制后续并发")))?
    .map_err(|error| AppError::Internal(format!("{operation}线程异常: {error}")))?
    .map_err(|error| AppError::BadRequest(error.to_string()))
}
