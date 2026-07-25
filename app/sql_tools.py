from __future__ import annotations


def rewrite_dollar_parameters(sql: str, hash_line_comments: bool = False) -> str:
    """Convert AnyDatas $name placeholders to DB-API named placeholders."""
    result: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            result.append(sql[start:index])
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            end = len(sql) if newline < 0 else newline
            result.append(sql[index:end])
            index = end
            continue
        if hash_line_comments and character == "#":
            newline = sql.find("\n", index + 1)
            end = len(sql) if newline < 0 else newline
            result.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            end = len(sql) if close < 0 else close + 2
            result.append(sql[index:end])
            index = end
            continue
        if character == "$":
            start = index + 1
            if start < len(sql) and (sql[start].isalpha() or sql[start] == "_"):
                end = start + 1
                while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                    end += 1
                if end < len(sql) and sql[end] == "$":
                    delimiter = sql[index : end + 1]
                    close = sql.find(delimiter, end + 1)
                    close_end = len(sql) if close < 0 else close + len(delimiter)
                    result.append(sql[index:close_end])
                    index = close_end
                    continue
                result.append(f"%({sql[start:end]})s")
                index = end
                continue
            if start < len(sql) and sql[start] == "$":
                close = sql.find("$$", start + 1)
                close_end = len(sql) if close < 0 else close + 2
                result.append(sql[index:close_end])
                index = close_end
                continue
        result.append(character)
        index += 1
    return "".join(result)


def mask_sql_literals_and_comments(sql: str, hash_line_comments: bool = False) -> str:
    """Replace non-executable SQL text with whitespace while preserving statement structure."""
    result: list[str] = []

    def mask(value: str) -> str:
        return "".join("\n" if character == "\n" else " " for character in value)

    index = 0
    while index < len(sql):
        character = sql[index]
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            result.append(mask(sql[start:index]))
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            end = len(sql) if newline < 0 else newline
            result.append(mask(sql[index:end]))
            index = end
            continue
        if hash_line_comments and character == "#":
            newline = sql.find("\n", index + 1)
            end = len(sql) if newline < 0 else newline
            result.append(mask(sql[index:end]))
            index = end
            continue
        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            end = len(sql) if close < 0 else close + 2
            result.append(mask(sql[index:end]))
            index = end
            continue
        if character == "$":
            start = index + 1
            if start < len(sql) and (sql[start].isalpha() or sql[start] == "_"):
                end = start + 1
                while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                    end += 1
                if end < len(sql) and sql[end] == "$":
                    delimiter = sql[index : end + 1]
                    close = sql.find(delimiter, end + 1)
                    close_end = len(sql) if close < 0 else close + len(delimiter)
                    result.append(mask(sql[index:close_end]))
                    index = close_end
                    continue
                result.append(mask(sql[index:end]))
                index = end
                continue
            if start < len(sql) and sql[start] == "$":
                close = sql.find("$$", start + 1)
                close_end = len(sql) if close < 0 else close + 2
                result.append(mask(sql[index:close_end]))
                index = close_end
                continue
        result.append(character)
        index += 1
    return "".join(result)
