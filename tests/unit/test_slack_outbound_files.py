"""
Unit tests for Slack outbound file extraction (#282).

Pure logic tests — no backend imports, no mocking, no pydantic.

Module: src/backend/adapters/message_router.py
Issue: https://github.com/abilityai/trinity/issues/282
"""

import re


# ---------------------------------------------------------------------------
# Reproduce key logic from the codebase for testing
# ---------------------------------------------------------------------------

_OUTBOUND_MIN_BLOCK_CHARS = 100
_OUTBOUND_MAX_FILES = 5
_OUTBOUND_MAX_FILE_BYTES = 500 * 1024       # 500 KB per block
_OUTBOUND_MAX_TOTAL_BYTES = 2 * 1024 * 1024 # 2 MB total

_OUTBOUND_LANG_MAP = {
    "csv": "csv", "json": "json", "html": "html", "xml": "xml",
    "yaml": "yaml", "yml": "yaml", "sql": "sql",
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "txt": "txt", "text": "txt",
}

_CODE_BLOCK_RE = re.compile(r'^```(\w+)\s*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def extract_outbound_files(response_text):
    """Reproduces ChannelMessageRouter._extract_outbound_files logic."""
    files = []
    total_bytes = 0
    file_counter = 0

    def replace_block(match):
        nonlocal total_bytes, file_counter

        lang_hint = match.group(1).lower()
        content = match.group(2)

        ext = _OUTBOUND_LANG_MAP.get(lang_hint)
        if not ext:
            return match.group(0)

        if len(content) < _OUTBOUND_MIN_BLOCK_CHARS:
            return match.group(0)

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _OUTBOUND_MAX_FILE_BYTES:
            return match.group(0)

        if total_bytes + len(content_bytes) > _OUTBOUND_MAX_TOTAL_BYTES:
            return match.group(0)

        if file_counter >= _OUTBOUND_MAX_FILES:
            return match.group(0)

        file_counter += 1
        total_bytes += len(content_bytes)
        filename = f"response_{file_counter}.{ext}"

        files.append({
            "filename": filename,
            "content": content_bytes,
            "language": lang_hint,
        })

        return f"(see attached: {filename})"

    cleaned_text = _CODE_BLOCK_RE.sub(replace_block, response_text)
    return cleaned_text, files


# ---------------------------------------------------------------------------
# Helper to generate a block of a given size
# ---------------------------------------------------------------------------

def _make_csv_block(num_chars):
    """Generate a CSV code block with approximately num_chars of content."""
    row = "name,revenue,region,quarter\n"
    rows_needed = max(1, num_chars // len(row))
    content = row * rows_needed
    return f"```csv\n{content}```"


def _make_block(lang, content):
    """Wrap content in a fenced code block."""
    return f"```{lang}\n{content}\n```"


# ===========================================================================
# Tests: Code Block Extraction
# ===========================================================================

class TestCodeBlockExtraction:
    """Tests for extracting fenced code blocks from agent responses."""

    def test_csv_block_extracted(self):
        """CSV block > 100 chars is extracted."""
        csv_content = "name,revenue,region\n" * 10  # ~200 chars
        text = f"Here's the data:\n\n```csv\n{csv_content}```\n\nLet me know."
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 1
        assert files[0]["filename"] == "response_1.csv"
        assert files[0]["language"] == "csv"
        assert files[0]["content"] == csv_content.encode("utf-8")
        assert "(see attached: response_1.csv)" in cleaned
        assert "```csv" not in cleaned

    def test_json_block_extracted(self):
        """JSON block > 100 chars is extracted."""
        json_content = '{\n  "data": [\n' + '    {"id": 1, "name": "test"},\n' * 5 + '  ]\n}'
        text = f"Result:\n\n```json\n{json_content}\n```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 1
        assert files[0]["filename"] == "response_1.json"

    def test_block_under_threshold_kept(self):
        """Block < 100 chars stays in the text."""
        small_content = "a,b\n1,2\n3,4"  # ~15 chars
        text = f"Example:\n\n```csv\n{small_content}\n```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 0
        assert "```csv" in cleaned
        assert small_content in cleaned

    def test_unrecognized_language_kept(self):
        """Unrecognized language hint (bash, rust) stays in text."""
        long_content = "echo 'hello world'\n" * 10
        text = f"Run this:\n\n```bash\n{long_content}```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 0
        assert "```bash" in cleaned

    def test_no_language_hint_kept(self):
        """Bare ``` block without language hint is not extracted."""
        long_content = "some data here\n" * 10
        text = f"Output:\n\n```\n{long_content}```"
        cleaned, files = extract_outbound_files(text)

        # Regex requires \w+ for language hint, so bare ``` won't match
        assert len(files) == 0

    def test_multiple_blocks_extracted(self):
        """Multiple qualifying blocks extracted with sequential numbering."""
        csv1 = "name,value\n" * 15
        csv2 = "id,status\n" * 15
        text = f"Data 1:\n\n```csv\n{csv1}```\n\nData 2:\n\n```csv\n{csv2}```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 2
        assert files[0]["filename"] == "response_1.csv"
        assert files[1]["filename"] == "response_2.csv"
        assert "(see attached: response_1.csv)" in cleaned
        assert "(see attached: response_2.csv)" in cleaned

    def test_max_files_limit(self):
        """6th qualifying block stays in text (max 5 files)."""
        blocks = []
        for i in range(6):
            content = f"col_{i},value\n" * 15
            blocks.append(f"```csv\n{content}```")
        text = "\n\n".join(blocks)
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 5
        # 6th block should still be in the text as a code block
        assert cleaned.count("(see attached:") == 5
        assert "```csv" in cleaned  # the 6th block remains

    def test_per_file_size_limit(self):
        """Block > 500KB stays in text."""
        huge_content = "x" * (500 * 1024 + 1)
        text = f"```csv\n{huge_content}\n```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 0
        assert "```csv" in cleaned

    def test_total_size_limit(self):
        """Cumulative > 2MB stops extraction."""
        # Create 5 blocks of 500KB each (2.5MB total) — 5th should be skipped
        blocks = []
        for i in range(5):
            content = "x" * (499 * 1024)  # just under per-file limit
            blocks.append(f"```csv\n{content}\n```")
        text = "\n\n".join(blocks)
        cleaned, files = extract_outbound_files(text)

        # 2MB / 499KB ≈ 4 files fit
        assert len(files) == 4
        assert cleaned.count("(see attached:") == 4

    def test_surrounding_text_preserved(self):
        """Text before and after extracted block is preserved."""
        csv_content = "name,revenue\n" * 10
        text = f"BEFORE TEXT\n\n```csv\n{csv_content}```\n\nAFTER TEXT"
        cleaned, files = extract_outbound_files(text)

        assert "BEFORE TEXT" in cleaned
        assert "AFTER TEXT" in cleaned
        assert len(files) == 1

    def test_placeholder_format(self):
        """Placeholder matches expected format exactly."""
        csv_content = "name,revenue\n" * 10
        text = f"```csv\n{csv_content}```"
        cleaned, files = extract_outbound_files(text)

        assert cleaned.strip() == "(see attached: response_1.csv)"

    def test_empty_response(self):
        """Empty string produces no files and no crash."""
        cleaned, files = extract_outbound_files("")

        assert cleaned == ""
        assert files == []

    def test_no_code_blocks(self):
        """Plain text with no code blocks returns unchanged."""
        text = "Hello, here is your answer. No code blocks here."
        cleaned, files = extract_outbound_files(text)

        assert cleaned == text
        assert files == []

    def test_unclosed_code_fence(self):
        """Unclosed code fence does not match and stays in text."""
        text = "Here:\n\n```csv\nname,value\n" + "row,data\n" * 10
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 0
        assert "```csv" in cleaned

    def test_unicode_content(self):
        """Unicode content is encoded as UTF-8."""
        csv_content = "name,city\nAlice,Zürich\nBob,東京\nCarlos,São Paulo\n" * 5
        text = f"```csv\n{csv_content}```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 1
        assert files[0]["content"] == csv_content.encode("utf-8")

    def test_mixed_extractable_and_non(self):
        """Mix of extractable and non-extractable blocks."""
        big_csv = "name,value\n" * 15          # > 100 chars, extractable
        small_csv = "a,b\n1,2"                  # < 100 chars, kept
        big_bash = "echo hello\n" * 15          # > 100 chars but bash, kept

        text = (
            f"```csv\n{big_csv}```\n\n"
            f"```csv\n{small_csv}\n```\n\n"
            f"```bash\n{big_bash}```"
        )
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 1  # only the big CSV
        assert files[0]["filename"] == "response_1.csv"
        assert small_csv in cleaned
        assert "```bash" in cleaned

    def test_trailing_whitespace_on_fence(self):
        """Trailing whitespace after language hint is handled."""
        csv_content = "name,value\n" * 10
        # Note the trailing spaces after 'csv'
        text = f"```csv   \n{csv_content}```"
        cleaned, files = extract_outbound_files(text)

        assert len(files) == 1
        assert files[0]["filename"] == "response_1.csv"


# ===========================================================================
# Tests: Language Mapping
# ===========================================================================

class TestLanguageMapping:
    """Tests for language hint → file extension mapping."""

    def test_csv_extension(self):
        assert _OUTBOUND_LANG_MAP["csv"] == "csv"

    def test_json_extension(self):
        assert _OUTBOUND_LANG_MAP["json"] == "json"

    def test_python_extension(self):
        assert _OUTBOUND_LANG_MAP["python"] == "py"

    def test_py_alias(self):
        assert _OUTBOUND_LANG_MAP["py"] == "py"

    def test_yml_alias(self):
        assert _OUTBOUND_LANG_MAP["yml"] == "yaml"

    def test_javascript_alias(self):
        assert _OUTBOUND_LANG_MAP["javascript"] == "js"

    def test_js_alias(self):
        assert _OUTBOUND_LANG_MAP["js"] == "js"

    def test_typescript_extension(self):
        assert _OUTBOUND_LANG_MAP["ts"] == "ts"

    def test_all_supported_languages(self):
        """Every language in the map produces a non-empty extension."""
        for lang, ext in _OUTBOUND_LANG_MAP.items():
            assert ext, f"Empty extension for {lang}"
            assert isinstance(ext, str)

    def test_unsupported_not_in_map(self):
        """Common non-data languages are not in the map."""
        for lang in ["bash", "shell", "rust", "go", "c", "cpp", "diff", "makefile"]:
            assert lang not in _OUTBOUND_LANG_MAP
