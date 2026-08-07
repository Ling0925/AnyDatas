//! Shared network guards for AI endpoints and post-process HTTP allowlists.
//!
//! Allowlist helpers are consumed by post-process HTTP in a later task; they are
//! public API surface and covered by unit tests today.

#![allow(dead_code)]

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use reqwest::Url;
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AllowlistEntry {
    Host(String),
    HostPort { host: String, port: u16 },
    UrlPrefix(String),
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum NetGuardError {
    #[error("仅允许 http 或 https 协议")]
    InvalidScheme,
    #[error("目标地址不在白名单内")]
    NotAllowlisted,
    #[error("目标解析到本机或私有网络")]
    RestrictedAddress,
}

/// Classify loopback, private, link-local, and other non-public addresses.
pub fn is_restricted_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_restricted_ipv4(address),
        IpAddr::V6(address) => is_restricted_ipv6(address),
    }
}

fn is_restricted_ipv4(address: Ipv4Addr) -> bool {
    let [a, b, c, _] = address.octets();
    address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_unspecified()
        || address.is_broadcast()
        || a >= 224
        || (a == 100 && (64..=127).contains(&b))
        || (a == 192 && b == 0 && c == 0)
        || (a == 192 && b == 0 && c == 2)
        || (a == 198 && (b == 18 || b == 19))
        || (a == 198 && b == 51 && c == 100)
        || (a == 203 && b == 0 && c == 113)
        || a == 0
}

fn is_restricted_ipv6(address: Ipv6Addr) -> bool {
    let first = address.segments()[0];
    address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || first & 0xfe00 == 0xfc00
        || first & 0xffc0 == 0xfe80
        || address.segments()[..2] == [0x2001, 0x0db8]
        || address.to_ipv4_mapped().is_some_and(is_restricted_ipv4)
}

/// Parse comma- or newline-separated allowlist text. Comments (`#`) and blanks are ignored.
/// Invalid entries fail fast.
pub fn parse_allowlist(text: &str) -> Result<Vec<AllowlistEntry>, String> {
    let mut entries = Vec::new();
    for raw in text.split([',', '\n', '\r']) {
        let token = raw.split('#').next().unwrap_or("").trim();
        if token.is_empty() {
            continue;
        }
        entries.push(parse_entry(token)?);
    }
    Ok(entries)
}

fn parse_entry(token: &str) -> Result<AllowlistEntry, String> {
    let lower = token.to_ascii_lowercase();
    if lower.starts_with("http://") || lower.starts_with("https://") {
        let url = Url::parse(token).map_err(|error| format!("白名单 URL 无效: {token} ({error})"))?;
        if url.scheme() != "http" && url.scheme() != "https" {
            return Err(format!("白名单 URL 协议无效: {token}"));
        }
        if url.host_str().is_none() {
            return Err(format!("白名单 URL 缺少主机: {token}"));
        }
        return Ok(AllowlistEntry::UrlPrefix(token.to_owned()));
    }

    if let Some((host_part, port_part)) = token.rsplit_once(':') {
        // Distinguish host:port from bare IPv6 (no brackets in our MVP format).
        if !host_part.contains("://") && port_part.chars().all(|c| c.is_ascii_digit()) {
            if host_part.is_empty() {
                return Err(format!("白名单主机端口无效: {token}"));
            }
            let port: u16 = port_part
                .parse()
                .map_err(|_| format!("白名单端口无效: {token}"))?;
            if port == 0 {
                return Err(format!("白名单端口无效: {token}"));
            }
            return Ok(AllowlistEntry::HostPort {
                host: host_part.to_ascii_lowercase(),
                port,
            });
        }
    }

    if token.contains('/') || token.contains(' ') {
        return Err(format!("白名单条目无效: {token}"));
    }
    Ok(AllowlistEntry::Host(token.to_ascii_lowercase()))
}

fn hostname_is_local(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host.to_ascii_lowercase().ends_with(".localhost")
        || host.to_ascii_lowercase().ends_with(".local")
}

fn entry_matches(url: &Url, entry: &AllowlistEntry) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let host_lc = host.to_ascii_lowercase();
    match entry {
        AllowlistEntry::Host(allowed) => host_lc == *allowed,
        AllowlistEntry::HostPort { host: allowed, port } => {
            host_lc == *allowed && url.port_or_known_default() == Some(*port)
        }
        AllowlistEntry::UrlPrefix(prefix) => url.as_str().starts_with(prefix.as_str()),
    }
}

/// Decide whether a resolved URL may be requested under the given allowlist policy.
pub fn url_allowed(
    url: &Url,
    allowlist: &[AllowlistEntry],
    resolved: &[IpAddr],
    allow_private_when_empty: bool,
) -> Result<(), NetGuardError> {
    if url.scheme() != "http" && url.scheme() != "https" {
        return Err(NetGuardError::InvalidScheme);
    }

    if !allowlist.is_empty() {
        if allowlist.iter().any(|entry| entry_matches(url, entry)) {
            return Ok(());
        }
        return Err(NetGuardError::NotAllowlisted);
    }

    let host = url.host_str().unwrap_or_default();
    let restricted = hostname_is_local(host) || resolved.iter().copied().any(is_restricted_address);
    if restricted && !allow_private_when_empty {
        return Err(NetGuardError::RestrictedAddress);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_private_and_public_addresses() {
        assert!(is_restricted_address("127.0.0.1".parse().unwrap()));
        assert!(is_restricted_address("169.254.169.254".parse().unwrap()));
        assert!(is_restricted_address("10.0.0.8".parse().unwrap()));
        assert!(is_restricted_address("::1".parse().unwrap()));
        assert!(is_restricted_address("fd00::1".parse().unwrap()));
        assert!(!is_restricted_address("8.8.8.8".parse().unwrap()));
        assert!(!is_restricted_address(
            "2606:4700:4700::1111".parse().unwrap()
        ));
    }

    #[test]
    fn parse_allowlist_supports_host_port_and_prefix() {
        let entries = parse_allowlist(
            r#"
            # comment
            api.example.com
            localhost:11434
            https://api.example.com/v1/
            "#,
        )
        .unwrap();
        assert_eq!(
            entries,
            vec![
                AllowlistEntry::Host("api.example.com".into()),
                AllowlistEntry::HostPort {
                    host: "localhost".into(),
                    port: 11434,
                },
                AllowlistEntry::UrlPrefix("https://api.example.com/v1/".into()),
            ]
        );
    }

    #[test]
    fn parse_allowlist_fails_on_invalid_entry() {
        let err = parse_allowlist("not a valid entry because spaces").unwrap_err();
        assert!(err.contains("无效"));
    }

    #[test]
    fn empty_allowlist_allows_public_ip() {
        let url = Url::parse("https://example.com/path").unwrap();
        let resolved = vec!["8.8.8.8".parse().unwrap()];
        assert!(url_allowed(&url, &[], &resolved, false).is_ok());
    }

    #[test]
    fn empty_allowlist_rejects_loopback() {
        let url = Url::parse("http://127.0.0.1:9/").unwrap();
        let resolved = vec!["127.0.0.1".parse().unwrap()];
        assert_eq!(
            url_allowed(&url, &[], &resolved, false).unwrap_err(),
            NetGuardError::RestrictedAddress
        );
    }

    #[test]
    fn host_port_allowlist_matches_localhost() {
        let entries = parse_allowlist("localhost:11434").unwrap();
        let url = Url::parse("http://localhost:11434/v1").unwrap();
        let resolved = vec!["127.0.0.1".parse().unwrap()];
        assert!(url_allowed(&url, &entries, &resolved, false).is_ok());
    }

    #[test]
    fn url_prefix_allowlist_matches_subpath_only() {
        let entries = parse_allowlist("https://api.example.com/v1/").unwrap();
        let ok = Url::parse("https://api.example.com/v1/rates").unwrap();
        let bad = Url::parse("https://other.example.com/v1/rates").unwrap();
        let resolved = vec!["8.8.8.8".parse().unwrap()];
        assert!(url_allowed(&ok, &entries, &resolved, false).is_ok());
        assert_eq!(
            url_allowed(&bad, &entries, &resolved, false).unwrap_err(),
            NetGuardError::NotAllowlisted
        );
    }

    #[test]
    fn rejects_non_http_schemes() {
        let url = Url::parse("ftp://example.com/file").unwrap();
        assert_eq!(
            url_allowed(&url, &[], &[], true).unwrap_err(),
            NetGuardError::InvalidScheme
        );
    }
}
