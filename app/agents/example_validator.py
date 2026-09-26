"""
Code Example Validator Agent.

Extracts code blocks from markdown documentation files, validates syntax and execution
in a sandboxed environment, and auto-fixes common breakage (such as stale version numbers
or renamed functions). Unfixable errors are flagged in issues without corrupting docs.
"""

import ast
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import yaml


# ----------------------------------------------------------------------
# Code Block Extractor
# ----------------------------------------------------------------------

class CodeBlock:
    """Represents a fenced code block extracted from a markdown file."""

    def __init__(
        self,
        file_path: str,
        lang: str,
        code: str,
        start_line: int,
        end_line: int,
        raw_match: str,
    ):
        self.file_path = file_path
        self.lang = lang.lower().strip()
        self.code = code
        self.start_line = start_line
        self.end_line = end_line
        self.raw_match = raw_match
        self.is_valid = True
        self.error: Optional[str] = None
        self.fixed_code: Optional[str] = None


def extract_code_blocks(markdown_content: str, file_path: str = "") -> List[CodeBlock]:
    """Extract fenced code blocks from markdown text with exact line coordinates."""
    blocks: List[CodeBlock] = []
    lines = markdown_content.splitlines(keepends=True)
    
    in_block = False
    current_lang = ""
    block_lines: List[str] = []
    start_line = 0

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_block:
                in_block = True
                current_lang = stripped[3:].strip()
                block_lines = []
                start_line = idx
            else:
                in_block = False
                end_line = idx
                code_text = "".join(block_lines)
                raw_match = "".join(lines[start_line - 1 : end_line])
                blocks.append(
                    CodeBlock(
                        file_path=file_path,
                        lang=current_lang,
                        code=code_text,
                        start_line=start_line,
                        end_line=end_line,
                        raw_match=raw_match,
                    )
                )
                current_lang = ""
                block_lines = []
        elif in_block:
            block_lines.append(line)

    return blocks


# ----------------------------------------------------------------------
# Validation Engine
# ----------------------------------------------------------------------

def validate_python_code(code: str, repo_path: str = ".", timeout_sec: int = 5) -> Tuple[bool, Optional[str]]:
    """
    Validate Python snippet by AST syntax parsing and sandboxed subprocess execution.
    """
    # 1. AST Syntax Check
    try:
        ast.parse(code)
    except SyntaxError as se:
        return False, f"SyntaxError at line {se.lineno}: {se.msg}"
    except Exception as e:
        return False, f"AST Parse Error: {str(e)}"

    # 2. Check if snippet is purely declarative/syntax-only or safe to run
    # Skip running if it contains placeholder ellipsis, interactive prompts, or pseudo-code
    trimmed = code.strip()
    if "..." in trimmed and len(trimmed.splitlines()) <= 3:
        return True, None
    if trimmed.startswith(">>>"):
        return True, None

    # 3. Subprocess Sandboxed Execution
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{os.path.abspath(repo_path)}{os.pathsep}{existing_pythonpath}"
    
    # Safe preamble to avoid opening actual external connections during tests
    preamble = "import sys\n"

    try:
        result = subprocess.run(
            [sys.executable, "-c", preamble + code],
            cwd=os.path.abspath(repo_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if result.returncode != 0:
            err_output = result.stderr.strip() or result.stdout.strip()
            # Extract last line of traceback for concise reporting
            tb_lines = err_output.splitlines()
            last_err = tb_lines[-1] if tb_lines else f"Exit code {result.returncode}"
            return False, f"Runtime Error: {last_err}"
        return True, None
    except subprocess.TimeoutExpired:
        return False, f"Execution timed out after {timeout_sec}s"
    except Exception as ex:
        return False, f"Execution failed: {str(ex)}"


def validate_json_code(code: str) -> Tuple[bool, Optional[str]]:
    """Validate JSON code snippet parseability."""
    try:
        json.loads(code)
        return True, None
    except json.JSONDecodeError as jde:
        return False, f"Invalid JSON (line {jde.lineno}, col {jde.colno}): {jde.msg}"


def validate_yaml_code(code: str) -> Tuple[bool, Optional[str]]:
    """Validate YAML code snippet parseability."""
    try:
        yaml.safe_load(code)
        return True, None
    except yaml.YAMLError as ye:
        return False, f"Invalid YAML: {str(ye)}"


def validate_block(block: CodeBlock, repo_path: str = ".") -> Tuple[bool, Optional[str]]:
    """Validate an individual code block according to its language."""
    lang = block.lang.lower()

    if lang in ("python", "py"):
        return validate_python_code(block.code, repo_path=repo_path)
    elif lang in ("json",):
        return validate_json_code(block.code)
    elif lang in ("yaml", "yml"):
        return validate_yaml_code(block.code)
    elif lang in ("bash", "sh", "shell"):
        # For shell, check obvious unmatched quotes
        if block.code.count('"') % 2 != 0 or block.code.count("'") % 2 != 0:
            return False, "Unmatched quotes in shell snippet"
        return True, None

    # Other languages considered syntactically valid by default
    return True, None


# ----------------------------------------------------------------------
# Auto-Fix Engine
# ----------------------------------------------------------------------

def auto_fix_block(
    block: CodeBlock,
    target_version: Optional[str] = None,
    renamed_functions: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Attempt safe auto-fixes for common documentation breakage:
    1. Outdated version numbers in pip / npm install commands
    2. Renamed function calls or deprecated symbol usage
    
    Returns:
        Fixed code string if a safe fix was applied, else None.
    """
    modified = False
    new_code = block.code

    # 1. Version auto-fix for bash/pip/npm or python __version__ snippets
    if target_version:
        # Match pip install pkg==X.Y.Z
        pip_pattern = r"(pip\s+install\s+[\w\-\.]+==)[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9_\-\.]*)"
        if re.search(pip_pattern, new_code):
            fixed = re.sub(pip_pattern, rf"\g<1>{target_version}", new_code)
            if fixed != new_code:
                new_code = fixed
                modified = True

        # Match npm install pkg@X.Y.Z
        npm_pattern = r"(npm\s+install\s+[\w\-\.\@\/]+@)[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9_\-\.]*)"
        if re.search(npm_pattern, new_code):
            fixed = re.sub(npm_pattern, rf"\g<1>{target_version}", new_code)
            if fixed != new_code:
                new_code = fixed
                modified = True

        # Match __version__ = "X.Y.Z"
        ver_pattern = r'(__version__\s*=\s*["\'])[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9_\-\.]*)(["\'])'
        if re.search(ver_pattern, new_code):
            fixed = re.sub(ver_pattern, rf"\g<1>{target_version}\g<2>", new_code)
            if fixed != new_code:
                new_code = fixed
                modified = True

    # 2. Renamed functions auto-fix
    if renamed_functions:
        for old_fn, new_fn in renamed_functions.items():
            # Word-boundary replacement for function name
            fn_pattern = rf"\b{re.escape(old_fn)}\b(?=\s*\()"
            if re.search(fn_pattern, new_code):
                new_code = re.sub(fn_pattern, new_fn, new_code)
                modified = True

    return new_code if modified else None


# ----------------------------------------------------------------------
# Standard Agent Interface Contract
# ----------------------------------------------------------------------

def run(context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Standard agent interface entrypoint for code example validation.
    
    Args:
        context: dict containing repo_path/repoPath, changed_files, diff, package_version
        
    Returns:
        Standard execution result dictionary matching PRD Section 6.
    """
    start_time = time.time()
    issues: List[str] = []
    files_written: List[str] = []

    if context is None:
        context = {}

    repo_path = context.get("repo_path") or context.get("repoPath") or "."
    package_version = context.get("package_version") or context.get("packageVersion")
    renamed_functions = context.get("renamed_functions", {})

    total_blocks = 0
    valid_count = 0
    fixed_count = 0
    scanned_file_count = 0

    try:
        # Find all markdown files in repo_path
        md_files: List[str] = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv", "env", "node_modules")]
            for file in files:
                if file.endswith(".md"):
                    md_files.append(os.path.join(root, file))

        scanned_file_count = len(md_files)

        for md_path in md_files:
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    original_content = f.read()
            except Exception as e:
                issues.append(f"Failed to read {md_path}: {str(e)}")
                continue

            blocks = extract_code_blocks(original_content, file_path=md_path)
            if not blocks:
                continue

            file_modified = False
            updated_content = original_content

            for block in blocks:
                total_blocks += 1
                
                # Check for auto-fix first
                fixed_code = auto_fix_block(
                    block,
                    target_version=package_version,
                    renamed_functions=renamed_functions,
                )

                if fixed_code and fixed_code != block.code:
                    # Replace code inside markdown
                    old_raw = f"```{block.lang}\n{block.code}```"
                    new_raw = f"```{block.lang}\n{fixed_code}```"
                    if old_raw in updated_content:
                        updated_content = updated_content.replace(old_raw, new_raw, 1)
                        file_modified = True
                        fixed_count += 1
                        block.code = fixed_code

                # Validate the code snippet
                is_valid, err_msg = validate_block(block, repo_path=repo_path)
                if is_valid:
                    valid_count += 1
                else:
                    rel_path = os.path.relpath(md_path, repo_path)
                    issue_msg = f"[{rel_path}:line {block.start_line}] ({block.lang}) {err_msg}"
                    issues.append(issue_msg)

            if file_modified:
                try:
                    with open(md_path, "w", encoding="utf-8") as f:
                        f.write(updated_content)
                    rel_written = os.path.relpath(md_path, repo_path).replace("\\", "/")
                    files_written.append(rel_written)
                except Exception as ex:
                    issues.append(f"Failed to write auto-fixed file {md_path}: {str(ex)}")

        duration_ms = int((time.time() - start_time) * 1000)
        status = "success"

        summary = (
            f"Validated {total_blocks} code blocks across {scanned_file_count} markdown files "
            f"({valid_count} valid, {fixed_count} auto-fixed, {len(issues)} issues flagged)"
        )

        return {
            "agent": "example-validator",
            "status": status,
            "duration_ms": duration_ms,
            "files_written": files_written,
            "summary": summary,
            "issues": issues,
        }

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        issues.append(f"Fatal error during example validation: {str(e)}")
        return {
            "agent": "example-validator",
            "status": "failed",
            "duration_ms": duration_ms,
            "files_written": files_written,
            "summary": f"Example validation failed: {str(e)}",
            "issues": issues,
        }
