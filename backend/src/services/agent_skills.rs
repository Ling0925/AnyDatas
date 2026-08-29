use std::{fs, path::Path};

use serde::Serialize;

const MAX_SKILLS: usize = 32;
const MAX_BODY_CHARS: usize = 16_000;
const MAX_DESCRIPTION_CHARS: usize = 500;

/// 本地 Skill 目录：`{data_dir}/agent-skills/{name}/SKILL.md`。
pub fn skills_dir(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join("agent-skills")
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SkillSummary {
    pub name: String,
    pub description: String,
}

#[derive(Debug, Clone)]
pub struct SkillDocument {
    pub name: String,
    pub description: String,
    pub body: String,
}

/// 扫描本地 Skill 目录；损坏或命名不合规的条目直接跳过。
pub fn list_skills(data_dir: &Path) -> Vec<SkillSummary> {
    let root = skills_dir(data_dir);
    let entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(_) => return Vec::new(),
    };
    let mut skills = Vec::new();
    for entry in entries.flatten() {
        if skills.len() >= MAX_SKILLS {
            break;
        }
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let folder = entry.file_name().to_string_lossy().to_string();
        if !is_skill_name(&folder) {
            continue;
        }
        let Ok(raw) = fs::read_to_string(path.join("SKILL.md")) else {
            continue;
        };
        let Some(document) = parse_skill(&folder, &raw) else {
            continue;
        };
        skills.push(SkillSummary {
            name: document.name,
            description: document.description,
        });
    }
    skills.sort_by(|left, right| left.name.cmp(&right.name));
    skills
}

/// 按目录名加载完整 Skill 正文，供模型在需要时再展开。
pub fn load_skill(data_dir: &Path, name: &str) -> Result<SkillDocument, String> {
    let name = name.trim();
    if !is_skill_name(name) {
        return Err("Skill 名称无效".to_owned());
    }
    let path = skills_dir(data_dir).join(name).join("SKILL.md");
    let raw = fs::read_to_string(&path).map_err(|_| format!("Skill 不存在: {name}"))?;
    parse_skill(name, &raw).ok_or_else(|| format!("Skill 文件无效: {name}"))
}

pub fn is_skill_name(name: &str) -> bool {
    let chars = name.chars().count();
    (1..=64).contains(&chars)
        && name
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
}

/// 最小 frontmatter：只认 `---` 包围的 `name` / `description`，不引入 YAML 依赖。
pub fn parse_skill(folder: &str, raw: &str) -> Option<SkillDocument> {
    let (frontmatter, body) = split_frontmatter(raw);
    let mut name = folder.to_owned();
    let mut description = String::new();
    if let Some(frontmatter) = frontmatter {
        for line in frontmatter.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let Some((key, value)) = line.split_once(':') else {
                continue;
            };
            let value = unquote(value.trim());
            match key.trim() {
                "name" if is_skill_name(&value) => name = value,
                "description" => description = truncate_chars(&value, MAX_DESCRIPTION_CHARS),
                _ => {}
            }
        }
    }
    if name != folder {
        name = folder.to_owned();
    }
    Some(SkillDocument {
        name,
        description,
        body: truncate_chars(body.trim(), MAX_BODY_CHARS),
    })
}

fn split_frontmatter(raw: &str) -> (Option<&str>, &str) {
    let text = raw.trim_start_matches('\u{feff}');
    let Some(rest) = text.strip_prefix("---") else {
        return (None, text);
    };
    let rest = rest.strip_prefix('\r').unwrap_or(rest);
    let rest = rest.strip_prefix('\n').unwrap_or(rest);
    let Some(closing) = rest.find("\n---").or_else(|| rest.find("\r\n---")) else {
        return (None, text);
    };
    let frontmatter = &rest[..closing];
    let after = rest[closing..]
        .split_once("---")
        .map(|(_, body)| body)
        .unwrap_or("");
    let after = after.strip_prefix('\r').unwrap_or(after);
    let after = after.strip_prefix('\n').unwrap_or(after);
    (Some(frontmatter), after)
}

fn unquote(value: &str) -> String {
    let value = value.trim();
    if (value.starts_with('"') && value.ends_with('"') && value.len() >= 2)
        || (value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2)
    {
        value[1..value.len() - 1].to_owned()
    } else {
        value.to_owned()
    }
}

fn truncate_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        value.to_owned()
    } else {
        value.chars().take(max_chars).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_frontmatter_and_keeps_folder_name() {
        let document = parse_skill(
            "join-keys",
            "---\nname: other\ndescription: \"识别 JOIN 键\"\n---\n# 用法\n先看字段。\n",
        )
        .unwrap();
        assert_eq!(document.name, "join-keys");
        assert_eq!(document.description, "识别 JOIN 键");
        assert!(document.body.contains("先看字段"));
    }

    #[test]
    fn accepts_skill_without_frontmatter() {
        let document = parse_skill("plain", "只写正文").unwrap();
        assert_eq!(document.name, "plain");
        assert_eq!(document.body, "只写正文");
    }
}
