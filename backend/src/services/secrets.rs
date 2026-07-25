use std::{
    env, fs,
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use base64::{Engine, engine::general_purpose::STANDARD};
use rand_core::{OsRng, RngCore};
use ring::aead::{AES_256_GCM, Aad, LessSafeKey, Nonce, UnboundKey};
use sha2::{Digest, Sha256};

const KEY_LENGTH: usize = 32;
const NONCE_LENGTH: usize = 12;
const KEY_FILE_NAME: &str = ".secret-key";

/// 从环境变量或数据卷加载主密钥；首次启动自动生成文件，可让单机部署无需额外密钥服务。
pub fn load_or_create(data_dir: &Path) -> Result<[u8; KEY_LENGTH]> {
    if let Ok(value) = env::var("ANYDATAS_SECRET_KEY") {
        let value = value.trim();
        if value.len() < 32 {
            bail!("ANYDATAS_SECRET_KEY must contain at least 32 characters");
        }
        return Ok(Sha256::digest(value.as_bytes()).into());
    }
    let path = data_dir.join(KEY_FILE_NAME);
    if path.exists() {
        return read_key(&path);
    }
    create_key(&path)
}

/// 使用 AES-256-GCM 加密工作区密钥，随机 Nonce 与密文合并后再进行 Base64 编码。
pub fn encrypt(master_key: &[u8; KEY_LENGTH], plaintext: &str) -> Result<String> {
    let key = LessSafeKey::new(
        UnboundKey::new(&AES_256_GCM, master_key).map_err(|_| anyhow::anyhow!("无法初始化密钥"))?,
    );
    let mut nonce_bytes = [0u8; NONCE_LENGTH];
    OsRng.fill_bytes(&mut nonce_bytes);
    let nonce = Nonce::assume_unique_for_key(nonce_bytes);
    let mut payload = plaintext.as_bytes().to_vec();
    key.seal_in_place_append_tag(nonce, Aad::empty(), &mut payload)
        .map_err(|_| anyhow::anyhow!("无法加密工作区密钥"))?;
    let mut encoded = nonce_bytes.to_vec();
    encoded.extend(payload);
    Ok(STANDARD.encode(encoded))
}

/// 解密工作区密钥并验证认证标签，数据库内容被修改时不会向上游发送伪造凭据。
pub fn decrypt(master_key: &[u8; KEY_LENGTH], ciphertext: &str) -> Result<String> {
    let mut payload = STANDARD.decode(ciphertext).context("工作区密钥编码无效")?;
    if payload.len() <= NONCE_LENGTH {
        bail!("工作区密钥密文无效");
    }
    let nonce_bytes: [u8; NONCE_LENGTH] = payload[..NONCE_LENGTH]
        .try_into()
        .expect("slice length is checked");
    let mut encrypted = payload.split_off(NONCE_LENGTH);
    let key = LessSafeKey::new(
        UnboundKey::new(&AES_256_GCM, master_key).map_err(|_| anyhow::anyhow!("无法初始化密钥"))?,
    );
    let plaintext = key
        .open_in_place(
            Nonce::assume_unique_for_key(nonce_bytes),
            Aad::empty(),
            &mut encrypted,
        )
        .map_err(|_| anyhow::anyhow!("工作区密钥无法解密"))?;
    String::from_utf8(plaintext.to_vec()).context("工作区密钥不是有效文本")
}

/// 读取并严格检查主密钥长度，避免用损坏的备份静默生成不可恢复的密文。
fn read_key(path: &Path) -> Result<[u8; KEY_LENGTH]> {
    let bytes = fs::read(path).with_context(|| format!("无法读取主密钥 {}", path.display()))?;
    bytes
        .try_into()
        .map_err(|_| anyhow::anyhow!("主密钥文件长度无效"))
}

/// 使用原子 `create_new` 生成本地主密钥，并限制权限以适配单机部署而不引入密钥服务。
fn create_key(path: &PathBuf) -> Result<[u8; KEY_LENGTH]> {
    let mut key = [0u8; KEY_LENGTH];
    OsRng.fill_bytes(&mut key);
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return read_key(path),
        Err(error) => return Err(error).context("无法创建主密钥"),
    };
    file.write_all(&key)?;
    file.sync_all()?;
    Ok(key)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encrypts_with_random_nonces_and_decrypts() {
        let key = [7u8; KEY_LENGTH];
        let first = encrypt(&key, "sk-test-secret").unwrap();
        let second = encrypt(&key, "sk-test-secret").unwrap();
        assert_ne!(first, second);
        assert_eq!(decrypt(&key, &first).unwrap(), "sk-test-secret");
    }

    #[test]
    fn rejects_modified_ciphertext() {
        let key = [9u8; KEY_LENGTH];
        let encrypted = encrypt(&key, "secret").unwrap();
        let mut bytes = STANDARD.decode(encrypted).unwrap();
        *bytes.last_mut().unwrap() ^= 1;
        assert!(decrypt(&key, &STANDARD.encode(bytes)).is_err());
    }
}
