use std::{collections::HashMap, path::Path};

use anyhow::{Context, Result, bail};
use calamine::{Data, Reader, open_workbook_auto};
use chrono::{NaiveDate, NaiveDateTime};
use serde::Serialize;
use serde_json::{Number, Value};

use crate::models::{FieldDefinition, TableData};

pub const SUPPORTED_FIELD_TYPES: [&str; 6] = ["文本", "整数", "小数", "布尔", "日期", "日期时间"];

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SheetSummary {
    pub name: String,
    pub row_count: usize,
    pub column_count: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkbookInspection {
    pub sheets: Vec<SheetSummary>,
}

#[derive(Debug, Clone, Copy)]
pub struct TableRangeOptions<'a> {
    pub sheet: &'a str,
    pub start_cell: &'a str,
    pub end_cell: Option<&'a str>,
    pub first_row_as_header: bool,
}

#[derive(Debug, Clone, Copy)]
struct ParsedCellRange {
    start_row: usize,
    start_col: usize,
    end_row: Option<usize>,
    end_col: Option<usize>,
}

pub fn inspect_file(path: &Path, file_kind: &str) -> Result<WorkbookInspection> {
    match file_kind {
        "csv" => inspect_csv(path),
        "excel" => inspect_excel(path),
        _ => bail!("不支持的文件类型"),
    }
}

pub fn read_table(
    path: &Path,
    file_kind: &str,
    sheet: &str,
    start_cell: &str,
    first_row_as_header: bool,
    max_rows: Option<usize>,
) -> Result<TableData> {
    read_table_range(
        path,
        file_kind,
        sheet,
        start_cell,
        None,
        first_row_as_header,
        max_rows,
    )
}

/// 将用户选择的字段类型覆盖到服务端推断结构上，同时保留真实空值属性和字段顺序。
pub fn apply_field_overrides(
    inferred: &[FieldDefinition],
    requested: Option<&[FieldDefinition]>,
) -> Result<Vec<FieldDefinition>> {
    let Some(requested) = requested else {
        return Ok(inferred.to_vec());
    };
    if inferred.len() != requested.len() {
        bail!("字段数量已变化，请重新预检文件");
    }
    inferred
        .iter()
        .zip(requested)
        .map(|(detected, selected)| {
            if detected.name != selected.name {
                bail!("字段结构已变化，请重新预检文件");
            }
            if !SUPPORTED_FIELD_TYPES.contains(&selected.data_type.as_str()) {
                bail!("字段 {} 的数据类型不受支持", selected.name);
            }
            Ok(FieldDefinition {
                name: detected.name.clone(),
                data_type: selected.data_type.clone(),
                nullable: detected.nullable,
            })
        })
        .collect()
}

/// 按明确的单元格范围读取表格，统一预览与查询的边界语义，避免两处解析结果不一致。
pub fn read_table_range(
    path: &Path,
    file_kind: &str,
    sheet: &str,
    start_cell: &str,
    end_cell: Option<&str>,
    first_row_as_header: bool,
    max_rows: Option<usize>,
) -> Result<TableData> {
    let range = parse_cell_range(start_cell, end_cell)?;
    match file_kind {
        "csv" => read_csv(path, range, first_row_as_header, max_rows),
        "excel" => read_excel(path, sheet, range, first_row_as_header, max_rows),
        _ => bail!("不支持的文件类型"),
    }
}

/// 流式读取指定范围，缓存构建可以在固定内存下处理大文件，同时严格复用预览边界。
pub fn stream_table_rows_range<F>(
    path: &Path,
    file_kind: &str,
    options: TableRangeOptions<'_>,
    width: usize,
    visitor: F,
) -> Result<usize>
where
    F: FnMut(Vec<Value>) -> Result<()>,
{
    let range = parse_cell_range(options.start_cell, options.end_cell)?;
    match file_kind {
        "csv" => stream_csv_rows(path, range, options.first_row_as_header, width, visitor),
        "excel" => stream_excel_rows(
            path,
            options.sheet,
            range,
            options.first_row_as_header,
            width,
            visitor,
        ),
        _ => bail!("不支持的文件类型"),
    }
}

/// 校验起止单元格，提前拒绝倒置范围，调用方因此无需各自处理边界错误。
fn parse_cell_range(start_cell: &str, end_cell: Option<&str>) -> Result<ParsedCellRange> {
    let start = parse_cell_reference(start_cell)?;
    let end = end_cell
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(parse_cell_reference)
        .transpose()?;
    if let Some((end_row, end_col)) = end
        && (end_row < start.0 || end_col < start.1)
    {
        bail!("结束单元格必须位于起始单元格的右下方");
    }
    Ok(ParsedCellRange {
        start_row: start.0,
        start_col: start.1,
        end_row: end.map(|value| value.0),
        end_col: end.map(|value| value.1),
    })
}

pub fn parse_cell_reference(value: &str) -> Result<(usize, usize)> {
    let value = value.trim().to_ascii_uppercase();
    if value.is_empty() {
        bail!("起始单元格不能为空");
    }
    let split = value
        .find(|character: char| character.is_ascii_digit())
        .context("起始单元格格式应类似 A1")?;
    let (letters, digits) = value.split_at(split);
    if letters.is_empty()
        || digits.is_empty()
        || !letters
            .chars()
            .all(|character| character.is_ascii_alphabetic())
        || !digits.chars().all(|character| character.is_ascii_digit())
    {
        bail!("起始单元格格式应类似 A1");
    }
    let row = digits.parse::<usize>().context("起始行无效")?;
    if row == 0 {
        bail!("起始行必须从 1 开始");
    }
    let mut column = 0usize;
    for character in letters.bytes() {
        column = column
            .checked_mul(26)
            .and_then(|value| value.checked_add((character - b'A' + 1) as usize))
            .context("起始列过大")?;
    }
    Ok((row - 1, column - 1))
}

fn inspect_excel(path: &Path) -> Result<WorkbookInspection> {
    let mut workbook = open_workbook_auto(path)
        .with_context(|| format!("无法打开 Excel 文件 {}", path.display()))?;
    let names = workbook.sheet_names();
    let mut sheets = Vec::with_capacity(names.len());
    for name in names {
        let range = workbook
            .worksheet_range(&name)
            .with_context(|| format!("无法读取工作表 {name}"))?;
        sheets.push(SheetSummary {
            name,
            row_count: range.height(),
            column_count: range.width(),
        });
    }
    if sheets.is_empty() {
        bail!("Excel 文件中没有可读取的工作表");
    }
    Ok(WorkbookInspection { sheets })
}

fn inspect_csv(path: &Path) -> Result<WorkbookInspection> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .from_path(path)?;
    let mut row_count = 0usize;
    let mut column_count = 0usize;
    for record in reader.records() {
        let record = record?;
        row_count += 1;
        column_count = column_count.max(record.len());
    }
    Ok(WorkbookInspection {
        sheets: vec![SheetSummary {
            name: "数据".to_owned(),
            row_count,
            column_count,
        }],
    })
}

fn read_excel(
    path: &Path,
    sheet: &str,
    configured: ParsedCellRange,
    first_row_as_header: bool,
    max_rows: Option<usize>,
) -> Result<TableData> {
    let mut workbook = open_workbook_auto(path)
        .with_context(|| format!("无法打开 Excel 文件 {}", path.display()))?;
    let range = workbook
        .worksheet_range(sheet)
        .with_context(|| format!("无法读取工作表 {sheet}"))?;
    let Some((sheet_end_row, sheet_end_col)) = range.end() else {
        return Ok(TableData {
            columns: Vec::new(),
            rows: Vec::new(),
            total_rows: 0,
        });
    };
    let end_row = configured
        .end_row
        .map(|value| value as u32)
        .unwrap_or(sheet_end_row)
        .min(sheet_end_row);
    let end_col = configured
        .end_col
        .map(|value| value as u32)
        .unwrap_or(sheet_end_col)
        .min(sheet_end_col);
    if configured.start_row as u32 > end_row || configured.start_col as u32 > end_col {
        return Ok(TableData {
            columns: Vec::new(),
            rows: Vec::new(),
            total_rows: 0,
        });
    }

    let mut last_column = None;
    for column in (configured.start_col as u32)..=end_col {
        let has_value = (configured.start_row as u32..=end_row).any(|row| {
            range
                .get_value((row, column))
                .is_some_and(|cell| !matches!(cell, Data::Empty))
        });
        if has_value {
            last_column = Some(column as usize);
        }
    }
    let Some(last_column) = last_column else {
        return Ok(TableData {
            columns: Vec::new(),
            rows: Vec::new(),
            total_rows: 0,
        });
    };

    let width = last_column - configured.start_col + 1;
    let header_values = if first_row_as_header {
        (configured.start_col..=last_column)
            .map(|column| {
                range
                    .get_value((configured.start_row as u32, column as u32))
                    .map(cell_to_json)
                    .unwrap_or(Value::Null)
            })
            .collect()
    } else {
        vec![Value::Null; width]
    };
    let names = build_column_names(&header_values, configured.start_col, first_row_as_header);
    let data_start = configured.start_row + usize::from(first_row_as_header);
    let total_rows = if data_start as u32 > end_row {
        0
    } else {
        end_row as usize - data_start + 1
    };
    let take_rows = max_rows.unwrap_or(total_rows).min(total_rows);
    let mut rows = Vec::with_capacity(take_rows);
    for row_index in data_start..data_start + take_rows {
        let row = (configured.start_col..=last_column)
            .map(|column| {
                range
                    .get_value((row_index as u32, column as u32))
                    .map(cell_to_json)
                    .unwrap_or(Value::Null)
            })
            .collect();
        rows.push(row);
    }
    let columns = infer_fields(names, &rows);
    Ok(TableData {
        columns,
        rows,
        total_rows,
    })
}

fn read_csv(
    path: &Path,
    range: ParsedCellRange,
    first_row_as_header: bool,
    max_rows: Option<usize>,
) -> Result<TableData> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .from_path(path)?;
    let mut width = 0usize;
    let mut header_values = None;
    let mut rows = Vec::new();
    let mut total_rows = 0usize;
    let retain_rows = max_rows.unwrap_or(usize::MAX);
    let configured_width = range.end_col.map(|value| value - range.start_col + 1);

    for (row_index, record) in reader.records().enumerate() {
        let record = record?;
        if range.end_row.is_some_and(|end| row_index > end) {
            break;
        }
        if row_index < range.start_row {
            continue;
        }
        width = width.max(
            record
                .len()
                .saturating_sub(range.start_col)
                .min(configured_width.unwrap_or(usize::MAX)),
        );
        if first_row_as_header && row_index == range.start_row {
            header_values = Some(
                record
                    .iter()
                    .skip(range.start_col)
                    .take(configured_width.unwrap_or(usize::MAX))
                    .map(|value| Value::String(value.to_owned()))
                    .collect::<Vec<_>>(),
            );
            continue;
        }

        total_rows += 1;
        if rows.len() < retain_rows {
            rows.push(
                record
                    .iter()
                    .skip(range.start_col)
                    .take(configured_width.unwrap_or(usize::MAX))
                    .map(parse_csv_value)
                    .collect::<Vec<_>>(),
            );
        }
    }
    if width == 0 {
        return Ok(TableData {
            columns: Vec::new(),
            rows: Vec::new(),
            total_rows: 0,
        });
    }
    let mut header_values = header_values.unwrap_or_else(|| vec![Value::Null; width]);
    header_values.resize(width, Value::Null);
    let names = build_column_names(&header_values, range.start_col, first_row_as_header);
    for row in &mut rows {
        row.resize(width, Value::Null);
    }
    let columns = infer_fields(names, &rows);
    Ok(TableData {
        columns,
        rows,
        total_rows,
    })
}

fn stream_csv_rows<F>(
    path: &Path,
    range: ParsedCellRange,
    first_row_as_header: bool,
    width: usize,
    mut visitor: F,
) -> Result<usize>
where
    F: FnMut(Vec<Value>) -> Result<()>,
{
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .flexible(true)
        .from_path(path)?;
    let mut row_count = 0usize;

    for (row_index, record) in reader.records().enumerate() {
        let record = record?;
        if range.end_row.is_some_and(|end| row_index > end) {
            break;
        }
        if row_index < range.start_row || (first_row_as_header && row_index == range.start_row) {
            continue;
        }
        let mut row = record
            .iter()
            .skip(range.start_col)
            .take(width)
            .map(parse_csv_value)
            .collect::<Vec<_>>();
        row.resize(width, Value::Null);
        visitor(row)?;
        row_count += 1;
    }

    Ok(row_count)
}

/// 单张逻辑表在一次缓存构建中允许物化的单元格上限。用于把异常巨大的读取范围（可能来自
/// 高度压缩的 xlsx / 类 zip-bomb 表）挡在 DuckDB 缓存构建之前，避免拖垮共享进程。
///
/// 注意：这是范围级别的防御，calamine 的 worksheet_range 仍会一次性把整张工作表读入内存；
/// 更彻底的常量内存方案需要改用流式 xlsx 读取，属后续工作。
const MAX_MATERIALIZED_CELLS: u64 = 50_000_000;

fn stream_excel_rows<F>(
    path: &Path,
    sheet: &str,
    configured: ParsedCellRange,
    first_row_as_header: bool,
    width: usize,
    mut visitor: F,
) -> Result<usize>
where
    F: FnMut(Vec<Value>) -> Result<()>,
{
    if width == 0 {
        return Ok(0);
    }
    let mut workbook = open_workbook_auto(path)
        .with_context(|| format!("无法打开 Excel 文件 {}", path.display()))?;
    let range = workbook
        .worksheet_range(sheet)
        .with_context(|| format!("无法读取工作表 {sheet}"))?;
    let Some((sheet_end_row, _)) = range.end() else {
        return Ok(0);
    };
    let end_row = configured
        .end_row
        .map(|value| value as u32)
        .unwrap_or(sheet_end_row)
        .min(sheet_end_row);
    let data_start = configured.start_row + usize::from(first_row_as_header);
    if data_start as u32 > end_row {
        return Ok(0);
    }
    let last_column = configured
        .start_col
        .checked_add(width - 1)
        .context("读取范围列数过大")?;
    let effective_rows = (end_row as u64 - data_start as u64) + 1;
    let cells = effective_rows.saturating_mul(width as u64);
    if cells > MAX_MATERIALIZED_CELLS {
        bail!(
            "工作表 {sheet} 的读取范围约 {cells} 个单元格，超过单表 {MAX_MATERIALIZED_CELLS} 上限；请缩小起止行列范围后重试"
        );
    }
    let mut row_count = 0usize;

    for row_index in data_start..=end_row as usize {
        let row = (configured.start_col..=last_column)
            .map(|column| {
                range
                    .get_value((row_index as u32, column as u32))
                    .map(cell_to_json)
                    .unwrap_or(Value::Null)
            })
            .collect();
        visitor(row)?;
        row_count += 1;
    }

    Ok(row_count)
}

fn build_column_names(values: &[Value], start_col: usize, use_values: bool) -> Vec<String> {
    let mut occurrences: HashMap<String, usize> = HashMap::new();
    values
        .iter()
        .enumerate()
        .map(|(index, value)| {
            let base = if use_values {
                value_to_label(value).filter(|value| !value.is_empty())
            } else {
                None
            }
            .unwrap_or_else(|| format!("列{}", column_label(start_col + index)));
            let key = base.to_lowercase();
            let count = occurrences.entry(key).or_insert(0);
            *count += 1;
            if *count == 1 {
                base
            } else {
                format!("{base}_{}", *count)
            }
        })
        .collect()
}

fn infer_fields(names: Vec<String>, rows: &[Vec<Value>]) -> Vec<FieldDefinition> {
    names
        .into_iter()
        .enumerate()
        .map(|(index, name)| {
            let values = rows.iter().filter_map(|row| row.get(index));
            let mut has_string = false;
            let mut has_float = false;
            let mut has_integer = false;
            let mut has_boolean = false;
            let mut has_date = false;
            let mut has_datetime = false;
            let mut nullable = false;
            for value in values.take(2_000) {
                match value {
                    Value::Null => nullable = true,
                    Value::Bool(_) => has_boolean = true,
                    Value::Number(number) if number.is_i64() || number.is_u64() => {
                        has_integer = true
                    }
                    Value::Number(_) => has_float = true,
                    Value::String(value) if boolean_string(value).is_some() => has_boolean = true,
                    Value::String(value) if value.trim().parse::<i64>().is_ok() => {
                        has_integer = true
                    }
                    Value::String(value) if value.trim().parse::<f64>().is_ok() => has_float = true,
                    Value::String(value) if parse_datetime(value).is_some() => has_datetime = true,
                    Value::String(value) if parse_date(value).is_some() => has_date = true,
                    _ => has_string = true,
                }
            }
            let has_numeric = has_integer || has_float;
            let has_temporal = has_date || has_datetime;
            let data_type = if has_string
                || (has_boolean && (has_numeric || has_temporal))
                || (has_numeric && has_temporal)
            {
                "文本"
            } else if has_datetime {
                "日期时间"
            } else if has_date {
                "日期"
            } else if has_boolean {
                "布尔"
            } else if has_float {
                "小数"
            } else if has_integer {
                "整数"
            } else {
                "文本"
            };
            FieldDefinition {
                name,
                data_type: data_type.to_owned(),
                nullable,
            }
        })
        .collect()
}

fn parse_date(value: &str) -> Option<NaiveDate> {
    ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"]
        .iter()
        .find_map(|format| NaiveDate::parse_from_str(value.trim(), format).ok())
}

fn parse_datetime(value: &str) -> Option<NaiveDateTime> {
    [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ]
    .iter()
    .find_map(|format| NaiveDateTime::parse_from_str(value.trim(), format).ok())
}

fn boolean_string(value: &str) -> Option<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "y" | "是" => Some(true),
        "false" | "no" | "n" | "否" => Some(false),
        _ => None,
    }
}

fn parse_csv_value(value: &str) -> Value {
    let value = value.trim();
    if value.is_empty() {
        Value::Null
    } else {
        // CSV 没有原生类型，保留原始文本才能让用户把 00123 之类的编码改为文本而不丢前导零。
        Value::String(value.to_owned())
    }
}

fn cell_to_json(cell: &Data) -> Value {
    match cell {
        Data::Int(value) => Value::Number((*value).into()),
        Data::Float(value)
            if value.fract() == 0.0 && *value >= i64::MIN as f64 && *value <= i64::MAX as f64 =>
        {
            Value::Number((*value as i64).into())
        }
        Data::Float(value) => Number::from_f64(*value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        Data::String(value) => Value::String(value.clone()),
        Data::Bool(value) => Value::Bool(*value),
        Data::DateTime(value) => {
            let (year, month, day, hour, minute, second, millisecond) = value.to_ymd_hms_milli();
            if hour == 0 && minute == 0 && second == 0 && millisecond == 0 {
                Value::String(format!("{year:04}-{month:02}-{day:02}"))
            } else {
                Value::String(format!(
                    "{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}:{second:02}"
                ))
            }
        }
        Data::DateTimeIso(value) | Data::DurationIso(value) => Value::String(value.clone()),
        Data::Error(value) => Value::String(format!("{value:?}")),
        Data::Empty => Value::Null,
    }
}

fn value_to_label(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.trim().to_owned()),
        Value::Number(value) => Some(value.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        Value::Null | Value::Array(_) | Value::Object(_) => None,
    }
}

fn column_label(mut index: usize) -> String {
    let mut result = String::new();
    index += 1;
    while index > 0 {
        let remainder = (index - 1) % 26;
        result.insert(0, (b'A' + remainder as u8) as char);
        index = (index - 1) / 26;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_cell_references() {
        assert_eq!(parse_cell_reference("A1").unwrap(), (0, 0));
        assert_eq!(parse_cell_reference("AA12").unwrap(), (11, 26));
        assert!(parse_cell_reference("A0").is_err());
        assert!(parse_cell_reference("1A").is_err());
    }

    #[test]
    fn deduplicates_headers() {
        let names = build_column_names(
            &[
                Value::String("金额".into()),
                Value::String("金额".into()),
                Value::Null,
            ],
            0,
            true,
        );
        assert_eq!(names, vec!["金额", "金额_2", "列C"]);
    }

    #[test]
    fn applies_supported_field_overrides() {
        let inferred = vec![FieldDefinition {
            name: "订单日期".to_owned(),
            data_type: "文本".to_owned(),
            nullable: true,
        }];
        let selected = vec![FieldDefinition {
            name: "订单日期".to_owned(),
            data_type: "日期".to_owned(),
            nullable: false,
        }];
        let fields = apply_field_overrides(&inferred, Some(&selected)).unwrap();
        assert_eq!(fields[0].data_type, "日期");
        assert!(fields[0].nullable);
    }

    #[test]
    fn infers_date_and_datetime_strings() {
        let fields = infer_fields(
            vec!["日期".to_owned(), "时间".to_owned()],
            &[
                vec![
                    Value::String("2026-07-19".to_owned()),
                    Value::String("2026-07-19 08:30:00".to_owned()),
                ],
                vec![
                    Value::String("2026/07/20".to_owned()),
                    Value::String("2026/07/20 09:45".to_owned()),
                ],
            ],
        );
        assert_eq!(fields[0].data_type, "日期");
        assert_eq!(fields[1].data_type, "日期时间");
    }

    #[test]
    fn streams_csv_while_preserving_total_rows_and_width() {
        let path = std::env::temp_dir().join(format!("anydatas-{}.csv", uuid::Uuid::new_v4()));
        std::fs::write(&path, "a,b\n1,2\n3,4,5\n6,7\n").unwrap();

        let table = read_csv(
            &path,
            ParsedCellRange {
                start_row: 0,
                start_col: 0,
                end_row: None,
                end_col: None,
            },
            true,
            Some(1),
        )
        .unwrap();
        std::fs::remove_file(path).unwrap();

        assert_eq!(table.total_rows, 3);
        assert_eq!(table.columns.len(), 3);
        assert_eq!(
            table.rows,
            vec![vec![Value::from("1"), Value::from("2"), Value::Null]]
        );
    }

    #[test]
    fn reads_only_the_configured_csv_range() {
        let path = std::env::temp_dir().join(format!("anydatas-{}.csv", uuid::Uuid::new_v4()));
        std::fs::write(
            &path,
            "说明,忽略,忽略\n编号,金额,备注\n1,20,A\n2,30,B\n3,40,C\n",
        )
        .unwrap();

        let table = read_table_range(&path, "csv", "数据", "A2", Some("B4"), true, None).unwrap();
        std::fs::remove_file(path).unwrap();

        assert_eq!(table.total_rows, 2);
        assert_eq!(table.columns.len(), 2);
        assert_eq!(
            table.rows,
            vec![
                vec![Value::from("1"), Value::from("20")],
                vec![Value::from("2"), Value::from("30")]
            ]
        );
    }
}
