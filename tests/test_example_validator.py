"""
Unit and integration tests for Code Example Validator Agent (example_validator.py).
"""

import os
import tempfile
import pytest

from app.agents.example_validator import (
    extract_code_blocks,
    validate_python_code,
    validate_json_code,
    validate_yaml_code,
    auto_fix_block,
    run,
)


def test_extract_code_blocks():
    """Test extracting multiple code blocks with accurate language and line numbers."""
    md_text = """# Documentation Guide

Here is a bash install command:
```bash
pip install devdocs-ai==0.1.0
```

Here is a Python example:
```python
def add(a: int, b: int) -> int:
    return a + b

print(add(2, 3))
```

Here is a JSON configuration:
```json
{
  "setting": true,
  "threshold": 10
}
```
"""
    blocks = extract_code_blocks(md_text, file_path="guide.md")
    assert len(blocks) == 3
    
    assert blocks[0].lang == "bash"
    assert "pip install" in blocks[0].code
    assert blocks[0].start_line == 4

    assert blocks[1].lang == "python"
    assert "def add" in blocks[1].code

    assert blocks[2].lang == "json"
    assert '"setting": true' in blocks[2].code


def test_validate_python_code_valid():
    """Test validation of valid Python code."""
    valid_code = "a = 10\nb = 20\nassert a + b == 30\n"
    is_valid, err = validate_python_code(valid_code)
    assert is_valid is True
    assert err is None


def test_validate_python_code_syntax_error():
    """Test syntax error detection."""
    broken_code = "def invalid_func(\n    return 42\n"
    is_valid, err = validate_python_code(broken_code)
    assert is_valid is False
    assert "SyntaxError" in err


def test_validate_python_code_runtime_error():
    """Test runtime error detection."""
    runtime_err_code = "x = [1, 2]\nprint(x[10])\n"
    is_valid, err = validate_python_code(runtime_err_code)
    assert is_valid is False
    assert "IndexError" in err or "Runtime Error" in err


def test_validate_json_code():
    """Test JSON validation."""
    valid_json = '{"name": "test", "active": true}'
    assert validate_json_code(valid_json)[0] is True

    invalid_json = '{"name": "test", active: true}'
    is_valid, err = validate_json_code(invalid_json)
    assert is_valid is False
    assert "Invalid JSON" in err


def test_validate_yaml_code():
    """Test YAML validation."""
    valid_yaml = "version: '3.0'\nservices:\n  app:\n    image: test"
    assert validate_yaml_code(valid_yaml)[0] is True

    invalid_yaml = "version: '3.0'\n  services:\n app: invalid_indentation"
    is_valid, err = validate_yaml_code(invalid_yaml)
    assert is_valid is False
    assert "Invalid YAML" in err


def test_auto_fix_stale_pip_version():
    """Test auto-fixing outdated package version in pip install snippet."""
    md = "```bash\npip install devdocs-ai==0.1.0\n```"
    blocks = extract_code_blocks(md)
    fixed = auto_fix_block(blocks[0], target_version="2.0.0")
    assert fixed is not None
    assert "pip install devdocs-ai==2.0.0" in fixed


def test_auto_fix_renamed_functions():
    """Test auto-fixing renamed function call in code block."""
    md = "```python\nresult = legacy_generate_doc(repo_path)\n```"
    blocks = extract_code_blocks(md)
    renamed = {"legacy_generate_doc": "generate_openapi_spec"}
    fixed = auto_fix_block(blocks[0], renamed_functions=renamed)
    assert fixed is not None
    assert "result = generate_openapi_spec(repo_path)" in fixed


def test_example_validator_run_contract():
    """Test standard agent interface run(context) end-to-end with auto-fix and issue flagging."""
    with tempfile.TemporaryDirectory() as temp_dir:
        doc_path = os.path.join(temp_dir, "API_GUIDE.md")
        sample_doc = """# API Guide

## Installation
```bash
pip install devdocs-ai==0.1.0
```

## Valid Example
```python
def multiply(x, y):
    return x * y

assert multiply(3, 4) == 12
```

## Broken Syntax Example
```python
def broken_syntax(
    return "missing close paren"
```
"""
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(sample_doc)

        context = {
            "repo_path": temp_dir,
            "changed_files": ["API_GUIDE.md"],
            "package_version": "1.5.0",
        }

        result = run(context)

        # Verify contract
        assert result["agent"] == "example-validator"
        assert result["status"] == "success"
        assert isinstance(result["duration_ms"], int)
        assert result["duration_ms"] >= 0
        assert "API_GUIDE.md" in result["files_written"]
        assert "summary" in result
        assert len(result["issues"]) >= 1
        assert any("SyntaxError" in issue for issue in result["issues"])

        # Verify file on disk was updated with new version
        with open(doc_path, "r", encoding="utf-8") as f:
            updated_content = f.read()

        assert "pip install devdocs-ai==1.5.0" in updated_content
        # Ensure broken code was not corrupted or removed
        assert "def broken_syntax" in updated_content
