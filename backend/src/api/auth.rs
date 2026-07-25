use std::sync::LazyLock;

use argon2::{
    Argon2, PasswordHash, PasswordHasher, PasswordVerifier,
    password_hash::{SaltString, rand_core::OsRng},
};
use axum::{
    Json, Router,
    extract::{FromRequestParts, State},
    http::{StatusCode, request::Parts},
    routing::{get, post},
};
use axum_extra::extract::cookie::{Cookie, CookieJar, SameSite};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::FromRow;
use time::Duration as CookieDuration;
use uuid::Uuid;

use crate::{
    error::{AppError, AppResult},
    models::SharedState,
};

const SESSION_COOKIE_NAME: &str = "anydatas_session";
const PASSWORD_MIN_LENGTH: usize = 12;
const PASSWORD_MAX_LENGTH: usize = 1_024;
const LOGIN_FAILURE_LIMIT: i64 = 5;
const LOGIN_WINDOW_MINUTES: i64 = 15;
const LOGIN_LOCK_MINUTES: i64 = 15;

static DUMMY_PASSWORD_HASH: LazyLock<String> = LazyLock::new(|| {
    hash_password("anydatas-dummy-password")
        .expect("the built-in dummy password satisfies validation")
});

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/auth/status", get(status))
        .route("/auth/setup", post(setup))
        .route("/auth/login", post(login))
        .route("/auth/logout", post(logout))
        .route("/auth/me", get(me))
}

#[derive(Debug, Clone, Serialize, FromRow)]
#[serde(rename_all = "camelCase")]
pub struct AuthContext {
    pub user_id: String,
    pub email: String,
    pub name: String,
    pub workspace_id: String,
    pub workspace_name: String,
    pub role: String,
}

impl AuthContext {
    /// 工作区级基础设施配置只允许所有者和管理员修改，分析员仍可使用已启用的能力。
    pub fn require_admin(&self) -> AppResult<()> {
        if matches!(self.role.as_str(), "owner" | "admin") {
            Ok(())
        } else {
            Err(AppError::Forbidden(
                "只有工作区管理员可以修改此设置".to_owned(),
            ))
        }
    }

    pub fn require_analyst(&self) -> AppResult<()> {
        if matches!(self.role.as_str(), "owner" | "admin" | "analyst") {
            Ok(())
        } else {
            Err(AppError::Forbidden("当前角色只有查看权限".to_owned()))
        }
    }
}

impl FromRequestParts<SharedState> for AuthContext {
    type Rejection = AppError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &SharedState,
    ) -> Result<Self, Self::Rejection> {
        let jar = CookieJar::from_headers(&parts.headers);
        let token = jar
            .get(SESSION_COOKIE_NAME)
            .map(Cookie::value)
            .ok_or_else(|| AppError::Unauthorized("请先登录".to_owned()))?;
        resolve_session(state, token)
            .await?
            .ok_or_else(|| AppError::Unauthorized("登录状态已失效，请重新登录".to_owned()))
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AuthStatus {
    setup_required: bool,
    authenticated: bool,
    user: Option<AuthContext>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SetupRequest {
    email: String,
    name: String,
    workspace_name: String,
    password: String,
}

#[derive(Debug, Deserialize)]
struct LoginRequest {
    email: String,
    password: String,
}

#[derive(Debug, FromRow)]
struct LoginUserRow {
    user_id: String,
    email: String,
    name: String,
    password_hash: String,
    workspace_id: String,
    workspace_name: String,
    role: String,
}

#[derive(Debug, FromRow)]
struct LoginAttemptRow {
    failed_count: i64,
    first_failed_at: String,
    locked_until: Option<String>,
}

async fn status(State(state): State<SharedState>, jar: CookieJar) -> AppResult<Json<AuthStatus>> {
    let user_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(&state.pool)
        .await?;
    let user = match jar.get(SESSION_COOKIE_NAME) {
        Some(cookie) => resolve_session(&state, cookie.value()).await?,
        None => None,
    };
    Ok(Json(AuthStatus {
        setup_required: user_count == 0,
        authenticated: user.is_some(),
        user,
    }))
}

async fn setup(
    State(state): State<SharedState>,
    jar: CookieJar,
    Json(request): Json<SetupRequest>,
) -> AppResult<(StatusCode, CookieJar, Json<AuthContext>)> {
    let email = normalize_email(&request.email)?;
    let name = validate_label(&request.name, "姓名")?;
    let workspace_name = validate_label(&request.workspace_name, "工作区名称")?;
    validate_password(&request.password)?;
    let password = request.password;
    let password_hash = tokio::task::spawn_blocking(move || hash_password(&password))
        .await
        .map_err(|error| AppError::Internal(format!("密码处理线程异常: {error}")))??;

    let mut transaction = state.pool.begin().await?;
    let user_count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(&mut *transaction)
        .await?;
    if user_count != 0 {
        return Err(AppError::Conflict("系统已经完成初始设置".to_owned()));
    }

    let user_id = Uuid::new_v4().to_string();
    let workspace_id = Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339();
    sqlx::query(
        "INSERT INTO users (id, email, name, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(&user_id)
    .bind(&email)
    .bind(&name)
    .bind(password_hash)
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    sqlx::query("INSERT INTO workspaces (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)")
        .bind(&workspace_id)
        .bind(&workspace_name)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await?;
    sqlx::query(
        "INSERT INTO workspace_memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'owner', ?)",
    )
    .bind(&user_id)
    .bind(&workspace_id)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        "UPDATE data_sources SET workspace_id = ?, created_by_user_id = ? WHERE workspace_id IS NULL",
    )
    .bind(&workspace_id)
    .bind(&user_id)
    .execute(&mut *transaction)
    .await?;

    let (token, token_hash, expires_at) = new_session_values(state.session_ttl_days);
    sqlx::query(
        "INSERT INTO auth_sessions (token_hash, user_id, workspace_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
    )
    .bind(token_hash)
    .bind(&user_id)
    .bind(&workspace_id)
    .bind(&now)
    .bind(expires_at)
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;

    let context = AuthContext {
        user_id,
        email,
        name,
        workspace_id,
        workspace_name,
        role: "owner".to_owned(),
    };
    let jar = jar.add(session_cookie(
        token,
        state.session_ttl_days,
        state.cookie_secure,
    ));
    Ok((StatusCode::CREATED, jar, Json(context)))
}

async fn login(
    State(state): State<SharedState>,
    jar: CookieJar,
    Json(request): Json<LoginRequest>,
) -> AppResult<(CookieJar, Json<AuthContext>)> {
    let email = normalize_email(&request.email)
        .map_err(|_| AppError::Unauthorized("邮箱或密码错误".to_owned()))?;
    let attempt_key = token_hash(&email);
    check_login_limit(&state, &attempt_key).await?;

    let row = sqlx::query_as::<_, LoginUserRow>(
        r#"
        SELECT u.id AS user_id, u.email, u.name, u.password_hash,
               w.id AS workspace_id, w.name AS workspace_name, m.role
        FROM users u
        JOIN workspace_memberships m ON m.user_id = u.id
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE u.email = ?
        ORDER BY CASE m.role
            WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
            WHEN 'analyst' THEN 2 ELSE 3 END, m.created_at
        LIMIT 1
        "#,
    )
    .bind(&email)
    .fetch_optional(&state.pool)
    .await?;
    let encoded = row
        .as_ref()
        .map(|user| user.password_hash.clone())
        .unwrap_or_else(|| DUMMY_PASSWORD_HASH.clone());
    let password = request.password;
    let password_matches =
        tokio::task::spawn_blocking(move || verify_password(&password, &encoded))
            .await
            .map_err(|error| AppError::Internal(format!("密码检查线程异常: {error}")))?;
    let Some(row) = row.filter(|_| password_matches) else {
        record_login_failure(&state, &attempt_key).await?;
        return Err(AppError::Unauthorized("邮箱或密码错误".to_owned()));
    };
    sqlx::query("DELETE FROM auth_login_attempts WHERE key_hash = ?")
        .bind(&attempt_key)
        .execute(&state.pool)
        .await?;

    let context = AuthContext {
        user_id: row.user_id,
        email: row.email,
        name: row.name,
        workspace_id: row.workspace_id,
        workspace_name: row.workspace_name,
        role: row.role,
    };
    let token = create_session(&state, &context.user_id, &context.workspace_id).await?;
    let jar = jar.add(session_cookie(
        token,
        state.session_ttl_days,
        state.cookie_secure,
    ));
    Ok((jar, Json(context)))
}

async fn logout(
    State(state): State<SharedState>,
    jar: CookieJar,
) -> AppResult<(CookieJar, StatusCode)> {
    if let Some(cookie) = jar.get(SESSION_COOKIE_NAME) {
        sqlx::query("DELETE FROM auth_sessions WHERE token_hash = ?")
            .bind(token_hash(cookie.value()))
            .execute(&state.pool)
            .await?;
    }
    Ok((jar.remove(removal_cookie()), StatusCode::NO_CONTENT))
}

async fn me(context: AuthContext) -> Json<AuthContext> {
    Json(context)
}

pub async fn resolve_session(state: &SharedState, token: &str) -> AppResult<Option<AuthContext>> {
    if token.is_empty() || token.len() > 256 {
        return Ok(None);
    }
    sqlx::query_as::<_, AuthContext>(
        r#"
        SELECT u.id AS user_id, u.email, u.name,
               w.id AS workspace_id, w.name AS workspace_name, m.role
        FROM auth_sessions s
        JOIN users u ON u.id = s.user_id
        JOIN workspaces w ON w.id = s.workspace_id
        JOIN workspace_memberships m
          ON m.user_id = s.user_id AND m.workspace_id = s.workspace_id
        WHERE s.token_hash = ? AND s.expires_at > ?
        "#,
    )
    .bind(token_hash(token))
    .bind(Utc::now().to_rfc3339())
    .fetch_optional(&state.pool)
    .await
    .map_err(Into::into)
}

async fn create_session(
    state: &SharedState,
    user_id: &str,
    workspace_id: &str,
) -> AppResult<String> {
    let now = Utc::now().to_rfc3339();
    sqlx::query("DELETE FROM auth_sessions WHERE expires_at <= ?")
        .bind(&now)
        .execute(&state.pool)
        .await?;
    let (token, token_hash, expires_at) = new_session_values(state.session_ttl_days);
    sqlx::query(
        "INSERT INTO auth_sessions (token_hash, user_id, workspace_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
    )
    .bind(token_hash)
    .bind(user_id)
    .bind(workspace_id)
    .bind(now)
    .bind(expires_at)
    .execute(&state.pool)
    .await?;
    Ok(token)
}

fn new_session_values(ttl_days: i64) -> (String, String, String) {
    let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let hash = token_hash(&token);
    let expires_at = (Utc::now() + Duration::days(ttl_days)).to_rfc3339();
    (token, hash, expires_at)
}

async fn check_login_limit(state: &SharedState, key: &str) -> AppResult<()> {
    let attempt = sqlx::query_as::<_, LoginAttemptRow>(
        "SELECT failed_count, first_failed_at, locked_until FROM auth_login_attempts WHERE key_hash = ?",
    )
    .bind(key)
    .fetch_optional(&state.pool)
    .await?;
    let Some(attempt) = attempt else {
        return Ok(());
    };
    let now = Utc::now();
    if let Some(locked_until) = parse_timestamp(attempt.locked_until.as_deref())
        && locked_until > now
    {
        return Err(AppError::RateLimited(
            "登录失败次数过多，请稍后再试".to_owned(),
        ));
    }
    if parse_timestamp(Some(&attempt.first_failed_at))
        .is_none_or(|first| first + Duration::minutes(LOGIN_WINDOW_MINUTES) <= now)
    {
        sqlx::query("DELETE FROM auth_login_attempts WHERE key_hash = ?")
            .bind(key)
            .execute(&state.pool)
            .await?;
    }
    Ok(())
}

async fn record_login_failure(state: &SharedState, key: &str) -> AppResult<()> {
    let existing = sqlx::query_as::<_, LoginAttemptRow>(
        "SELECT failed_count, first_failed_at, locked_until FROM auth_login_attempts WHERE key_hash = ?",
    )
    .bind(key)
    .fetch_optional(&state.pool)
    .await?;
    let now = Utc::now();
    let (first_failed_at, failed_count) = match existing {
        Some(attempt)
            if parse_timestamp(Some(&attempt.first_failed_at))
                .is_some_and(|first| first + Duration::minutes(LOGIN_WINDOW_MINUTES) > now) =>
        {
            (attempt.first_failed_at, attempt.failed_count + 1)
        }
        _ => (now.to_rfc3339(), 1),
    };
    let locked_until = (failed_count >= LOGIN_FAILURE_LIMIT)
        .then(|| (now + Duration::minutes(LOGIN_LOCK_MINUTES)).to_rfc3339());
    sqlx::query(
        r#"
        INSERT INTO auth_login_attempts (key_hash, failed_count, first_failed_at, locked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key_hash) DO UPDATE SET
            failed_count = excluded.failed_count,
            first_failed_at = excluded.first_failed_at,
            locked_until = excluded.locked_until
        "#,
    )
    .bind(key)
    .bind(failed_count)
    .bind(first_failed_at)
    .bind(locked_until)
    .execute(&state.pool)
    .await?;
    Ok(())
}

fn parse_timestamp(value: Option<&str>) -> Option<DateTime<Utc>> {
    value
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc))
}

fn normalize_email(value: &str) -> AppResult<String> {
    let email = value.trim().to_ascii_lowercase();
    let valid = email.len() <= 254
        && !email.contains(char::is_whitespace)
        && email.split_once('@').is_some_and(|(local, domain)| {
            !local.is_empty() && domain.contains('.') && !domain.ends_with('.')
        });
    if !valid {
        return Err(AppError::BadRequest("请输入有效的邮箱地址".to_owned()));
    }
    Ok(email)
}

fn validate_label(value: &str, label: &str) -> AppResult<String> {
    let value = value.trim();
    if value.is_empty() || value.chars().count() > 120 {
        return Err(AppError::BadRequest(format!(
            "{label}长度应在 1 到 120 个字符之间"
        )));
    }
    Ok(value.to_owned())
}

fn validate_password(password: &str) -> AppResult<()> {
    let length = password.chars().count();
    if !(PASSWORD_MIN_LENGTH..=PASSWORD_MAX_LENGTH).contains(&length) {
        return Err(AppError::BadRequest(format!(
            "密码长度应在 {PASSWORD_MIN_LENGTH} 到 {PASSWORD_MAX_LENGTH} 个字符之间"
        )));
    }
    Ok(())
}

fn hash_password(password: &str) -> AppResult<String> {
    validate_password(password)?;
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map(|hash| hash.to_string())
        .map_err(|error| AppError::Internal(format!("密码哈希失败: {error}")))
}

fn verify_password(password: &str, encoded: &str) -> bool {
    if password.chars().count() > PASSWORD_MAX_LENGTH {
        return false;
    }
    PasswordHash::new(encoded).ok().is_some_and(|hash| {
        Argon2::default()
            .verify_password(password.as_bytes(), &hash)
            .is_ok()
    })
}

fn token_hash(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

fn session_cookie(token: String, ttl_days: i64, secure: bool) -> Cookie<'static> {
    Cookie::build((SESSION_COOKIE_NAME, token))
        .path("/")
        .http_only(true)
        .same_site(SameSite::Lax)
        .secure(secure)
        .max_age(CookieDuration::days(ttl_days))
        .build()
}

fn removal_cookie() -> Cookie<'static> {
    Cookie::build((SESSION_COOKIE_NAME, ""))
        .path("/")
        .http_only(true)
        .same_site(SameSite::Lax)
        .max_age(CookieDuration::ZERO)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashes_and_verifies_passwords() {
        let encoded = hash_password("correct horse battery staple").unwrap();
        assert!(verify_password("correct horse battery staple", &encoded));
        assert!(!verify_password("incorrect password", &encoded));
    }

    #[test]
    fn validates_identity_fields() {
        assert_eq!(
            normalize_email(" User@Example.com ").unwrap(),
            "user@example.com"
        );
        assert!(normalize_email("invalid").is_err());
        assert!(validate_password("too-short").is_err());
        assert!(validate_label("", "工作区名称").is_err());
    }
}
