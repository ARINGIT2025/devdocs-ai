"""
API Documentation Generator Agent.

Scans FastAPI route definitions using Python AST parsing, extracts endpoint metadata,
query/path/body parameters, and Pydantic schemas, generates an OpenAPI 3.0 specification
with synthetic request/response examples, and generates a standalone Swagger UI viewer.
"""

import ast
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import yaml


# ----------------------------------------------------------------------
# AST Helper & Model Extractor
# ----------------------------------------------------------------------

def _ast_to_literal(node: Optional[ast.AST]) -> Any:
    """Safely convert AST node to python literal value if possible."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if hasattr(ast, "Str") and isinstance(node, getattr(ast, "Str")):
        return getattr(node, "s", None)
    if hasattr(ast, "Num") and isinstance(node, getattr(ast, "Num")):
        return getattr(node, "n", None)
    if hasattr(ast, "NameConstant") and isinstance(node, getattr(ast, "NameConstant")):
        return getattr(node, "value", None)
    if isinstance(node, ast.List):
        return [_ast_to_literal(elt) for elt in node.elts]
    if isinstance(node, ast.Tuple):
        return [_ast_to_literal(elt) for elt in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _ast_to_literal(k): _ast_to_literal(v)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        val = _ast_to_literal(node.value)
        return f"{val}.{node.attr}" if val else node.attr
    return None


def _extract_type_name(node: Optional[ast.AST]) -> str:
    """Extract string representation of a type annotation AST node."""
    if node is None:
        return "Any"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        value_name = _extract_type_name(node.value)
        slice_node = node.slice
        if hasattr(ast, "Index") and isinstance(slice_node, getattr(ast, "Index")):
            slice_node = getattr(slice_node, "value")
        slice_name = _extract_type_name(slice_node)
        return f"{value_name}[{slice_name}]"
    if isinstance(node, ast.Tuple):
        elts = ", ".join(_extract_type_name(e) for e in node.elts)
        return f"({elts})"
    return "Any"


def _map_python_type_to_openapi(type_str: str) -> Dict[str, Any]:
    """Map python type name string to OpenAPI schema definition."""
    type_str = type_str.strip()
    
    # Handle Optional[...]
    if type_str.startswith("Optional[") and type_str.endswith("]"):
        inner = type_str[9:-1].strip()
        schema = _map_python_type_to_openapi(inner)
        schema["nullable"] = True
        return schema

    # Handle List[...]
    if type_str.startswith("List[") and type_str.endswith("]"):
        inner = type_str[5:-1].strip()
        return {
            "type": "array",
            "items": _map_python_type_to_openapi(inner),
        }

    # Handle Union[..., None]
    if type_str.startswith("Union[") and type_str.endswith("]"):
        inners = [p.strip() for p in type_str[6:-1].split(",")]
        non_none = [p for p in inners if p not in ("None", "NoneType")]
        if non_none:
            schema = _map_python_type_to_openapi(non_none[0])
            if len(non_none) < len(inners):
                schema["nullable"] = True
            return schema

    # Primitives
    mapping = {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "dict": {"type": "object"},
        "Dict": {"type": "object"},
        "list": {"type": "array", "items": {"type": "string"}},
        "List": {"type": "array", "items": {"type": "string"}},
        "Any": {"type": "string"},
    }

    if type_str in mapping:
        return dict(mapping[type_str])

    # Reference to custom schema model
    return {"$ref": f"#/components/schemas/{type_str}"}


def _generate_example_value(field_name: str, type_str: str) -> Any:
    """Generate a realistic example value based on field name and type."""
    fn = field_name.lower()
    ts = type_str.lower()

    if "optional[" in ts:
        ts = ts.replace("optional[", "").rstrip("]")
    if "list[" in ts:
        inner = ts.replace("list[", "").rstrip("]")
        return [_generate_example_value(field_name, inner)]

    # Name-based heuristics
    if fn in ("id", "item_id", "user_id", "order_id", "account_id") or fn.endswith("_id"):
        return 101 if "user" in fn else 1
    if "email" in fn:
        return "user@example.com"
    if "username" in fn:
        return "alex_dev"
    if "full_name" in fn or fn == "name":
        return "Alex Mercer" if "user" in fn or "name" == fn else "Sample Item"
    if "title" in fn:
        return "Sample Title"
    if "description" in fn or "summary" in fn:
        return "A detailed description of the entity."
    if "price" in fn or "cost" in fn or "amount" in fn:
        return 29.99
    if "is_" in fn or "has_" in fn or fn in ("active", "enabled", "completed"):
        return True
    if "count" in fn or "limit" in fn or "size" in fn:
        return 10
    if "skip" in fn or "offset" in fn:
        return 0
    if "url" in fn or "uri" in fn or "link" in fn:
        return "https://example.com/api/resource"
    if "created_at" in fn or "updated_at" in fn or "date" in fn or "timestamp" in fn:
        return "2026-09-26T12:00:00Z"
    if "tags" in fn or "categories" in fn:
        return ["tech", "development"]
    if "role" in fn:
        return "admin"

    # Type-based heuristics
    if "int" in ts:
        return 1
    if "float" in ts or "number" in ts:
        return 19.99
    if "bool" in ts:
        return True
    if "str" in ts or ts == "string":
        return f"sample_{field_name}" if field_name else "sample_value"
    if "dict" in ts or "object" in ts:
        return {"key": "value"}
    if "list" in ts or "array" in ts:
        return ["item_1", "item_2"]

    return f"sample_{field_name}" if field_name else "example"


# ----------------------------------------------------------------------
# AST Model & Route Extractor
# ----------------------------------------------------------------------

class SchemaExtractor(ast.NodeVisitor):
    """Extract Pydantic model schemas from Python AST."""

    def __init__(self):
        self.schemas: Dict[str, Dict[str, Any]] = {}

    def visit_ClassDef(self, node: ast.ClassDef):
        fields: Dict[str, Any] = {}
        required: List[str] = []
        examples: Dict[str, Any] = {}

        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                field_name = item.target.id
                type_name = _extract_type_name(item.annotation)
                field_schema = _map_python_type_to_openapi(type_name)

                # Check default value / Field(...)
                has_default = item.value is not None
                if isinstance(item.value, ast.Call):
                    # Check Field(...)
                    for kw in item.value.keywords:
                        if kw.arg == "description":
                            desc = _ast_to_literal(kw.value)
                            if desc:
                                field_schema["description"] = desc
                        elif kw.arg == "default":
                            val = _ast_to_literal(kw.value)
                            if val != Ellipsis:
                                has_default = True
                    # If first arg is ... (Ellipsis)
                    if item.value.args and isinstance(item.value.args[0], ast.Constant) and item.value.args[0].value == Ellipsis:
                        has_default = False

                if not has_default and not type_name.startswith("Optional["):
                    required.append(field_name)

                example_val = _generate_example_value(field_name, type_name)
                field_schema["example"] = example_val
                fields[field_name] = field_schema
                examples[field_name] = example_val

        if fields:
            schema_obj = {
                "type": "object",
                "properties": fields,
            }
            if required:
                schema_obj["required"] = required
            if examples:
                schema_obj["example"] = examples
            self.schemas[node.name] = schema_obj

        self.generic_visit(node)


class RouteExtractor(ast.NodeVisitor):
    """Extract FastAPI route endpoints from Python AST."""

    HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.routes: List[Dict[str, Any]] = []
        self.router_prefix = ""
        self.router_tags: List[str] = []
        self.included_routers: List[Dict[str, Any]] = []

    def visit_Assign(self, node: ast.Assign):
        # Look for router = APIRouter(prefix="/...", tags=[...])
        if isinstance(node.value, ast.Call):
            func = node.value.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr

            if "APIRouter" in name:
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        self.router_prefix = _ast_to_literal(kw.value) or ""
                    elif kw.arg == "tags":
                        tags = _ast_to_literal(kw.value)
                        if isinstance(tags, list):
                            self.router_tags = tags
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        # Look for app.include_router(router, prefix="/api/v1")
        if isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "include_router":
                prefix = ""
                router_name = ""
                if node.value.args and isinstance(node.value.args[0], ast.Name):
                    router_name = node.value.args[0].id
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = _ast_to_literal(kw.value) or ""
                self.included_routers.append({"router": router_name, "prefix": prefix})
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_route_decorators(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_route_decorators(node)
        self.generic_visit(node)

    def _check_route_decorators(self, node: Any):
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue

            func = decorator.func
            method = ""
            router_var = ""

            if isinstance(func, ast.Attribute):
                if func.attr.lower() in self.HTTP_METHODS:
                    method = func.attr.lower()
                    if isinstance(func.value, ast.Name):
                        router_var = func.value.id

            if not method:
                continue

            # Extract path from first argument or 'path' keyword
            path = ""
            if decorator.args:
                path = _ast_to_literal(decorator.args[0]) or ""
            for kw in decorator.keywords:
                if kw.arg == "path":
                    path = _ast_to_literal(kw.value) or path

            if not path.startswith("/"):
                path = "/" + path

            # Extract decorator options
            summary = ""
            description = ""
            tags = list(self.router_tags)
            response_model_str = ""
            status_code = 200 if method != "post" else 201

            for kw in decorator.keywords:
                if kw.arg == "summary":
                    summary = _ast_to_literal(kw.value) or ""
                elif kw.arg == "description":
                    description = _ast_to_literal(kw.value) or ""
                elif kw.arg == "tags":
                    d_tags = _ast_to_literal(kw.value)
                    if isinstance(d_tags, list):
                        tags = list(set(tags + d_tags))
                elif kw.arg == "response_model":
                    response_model_str = _extract_type_name(kw.value)
                elif kw.arg == "status_code":
                    val = _ast_to_literal(kw.value)
                    if isinstance(val, int):
                        status_code = val
                    elif isinstance(val, str) and "204" in val:
                        status_code = 204
                    elif isinstance(val, str) and "201" in val:
                        status_code = 201

            # Fallbacks from function
            docstring = ast.get_docstring(node) or ""
            if not summary:
                summary = node.name.replace("_", " ").title()
            if not description and docstring:
                description = docstring.strip()

            # Inspect function parameters (path, query, body)
            path_param_names = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", path))
            path_params: List[Dict[str, Any]] = []
            query_params: List[Dict[str, Any]] = []
            body_param: Optional[Dict[str, Any]] = None

            # Map arguments
            args = node.args.args
            defaults = node.args.defaults
            num_defaults = len(defaults)
            default_start_idx = len(args) - num_defaults

            for idx, arg in enumerate(args):
                arg_name = arg.arg
                if arg_name in ("self", "cls"):
                    continue

                type_str = _extract_type_name(arg.annotation) if arg.annotation else "str"
                has_def = idx >= default_start_idx
                param_desc = ""

                # Check if default is Query(...) or Path(...)
                if has_def:
                    def_node = defaults[idx - default_start_idx]
                    if isinstance(def_node, ast.Call):
                        call_name = _extract_type_name(def_node.func)
                        for kw in def_node.keywords:
                            if kw.arg == "description":
                                param_desc = _ast_to_literal(kw.value) or ""

                if arg_name in path_param_names:
                    schema = _map_python_type_to_openapi(type_str)
                    schema["example"] = _generate_example_value(arg_name, type_str)
                    param_dict = {
                        "name": arg_name,
                        "in": "path",
                        "required": True,
                        "schema": schema,
                    }
                    if param_desc:
                        param_dict["description"] = param_desc
                    path_params.append(param_dict)
                else:
                    # Check if body param (Pydantic model or dict/body)
                    is_primitive = type_str.lower() in (
                        "str", "int", "float", "bool", "optional[str]",
                        "optional[int]", "optional[float]", "optional[bool]"
                    )
                    if not is_primitive and not type_str.startswith("Query"):
                        # Body parameter
                        body_schema = _map_python_type_to_openapi(type_str)
                        body_param = {
                            "name": arg_name,
                            "type": type_str,
                            "schema": body_schema,
                            "required": not type_str.startswith("Optional["),
                        }
                    else:
                        schema = _map_python_type_to_openapi(type_str)
                        schema["example"] = _generate_example_value(arg_name, type_str)
                        param_dict = {
                            "name": arg_name,
                            "in": "query",
                            "required": not has_def and not type_str.startswith("Optional["),
                            "schema": schema,
                        }
                        if param_desc:
                            param_dict["description"] = param_desc
                        query_params.append(param_dict)

            self.routes.append({
                "method": method,
                "path": path,
                "summary": summary,
                "description": description,
                "tags": tags,
                "status_code": status_code,
                "response_model": response_model_str,
                "path_params": path_params,
                "query_params": query_params,
                "body_param": body_param,
                "file": self.file_path,
                "line": node.lineno,
                "handler_name": node.name,
                "router_var": router_var,
            })


# ----------------------------------------------------------------------
# Endpoint Scanner & OpenAPI Generator
# ----------------------------------------------------------------------

def scan_endpoints(repo_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Recursively scan Python files in repo_path for FastAPI routes and Pydantic schemas.
    """
    all_routes: List[Dict[str, Any]] = []
    all_schemas: Dict[str, Any] = {}
    included_routers_map: Dict[str, str] = {}  # router_var -> prefix

    # First pass: collect files to scan
    py_files: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        # Skip cache, git, venv
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv", "env", "node_modules")]
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))

    # Second pass: parse AST for schemas and routes
    file_extractors: List[RouteExtractor] = []

    for file_path in py_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content, filename=file_path)

            # Extract schemas
            schema_ext = SchemaExtractor()
            schema_ext.visit(tree)
            all_schemas.update(schema_ext.schemas)

            # Extract routes
            route_ext = RouteExtractor(file_path=file_path)
            route_ext.visit(tree)
            file_extractors.append(route_ext)

            for inc in route_ext.included_routers:
                if inc["router"]:
                    included_routers_map[inc["router"]] = inc["prefix"]

        except Exception as e:
            # Skip unparseable files gracefully
            continue

    # Third pass: resolve router prefixes and paths
    for ext in file_extractors:
        for route in ext.routes:
            final_path = route["path"]
            # Apply internal router prefix if set
            if ext.router_prefix and not final_path.startswith(ext.router_prefix):
                final_path = ext.router_prefix.rstrip("/") + "/" + final_path.lstrip("/")

            # Apply include_router prefix if router variable matches
            for r_var, inc_prefix in included_routers_map.items():
                if route["router_var"] and (r_var in route["router_var"] or route["router_var"] in r_var):
                    if not final_path.startswith(inc_prefix):
                        final_path = inc_prefix.rstrip("/") + "/" + final_path.lstrip("/")

            # Clean duplicate slashes
            final_path = "/" + "/".join(part for part in final_path.split("/") if part)
            route["full_path"] = final_path
            all_routes.append(route)

    return all_routes, all_schemas


def generate_openapi_spec(
    endpoints: List[Dict[str, Any]],
    schemas: Dict[str, Any],
    title: str = "DevDocs AI Generated API",
    version: str = "1.0.0",
    description: str = "Auto-generated OpenAPI 3.0 specification from FastAPI source code.",
) -> Dict[str, Any]:
    """Generate OpenAPI 3.0 specification dictionary from parsed endpoints and schemas."""
    paths: Dict[str, Any] = {}

    for ep in endpoints:
        path = ep.get("full_path") or ep["path"]
        method = ep["method"].lower()

        if path not in paths:
            paths[path] = {}

        operation_id = f"{method}_{ep['handler_name']}"
        operation: Dict[str, Any] = {
            "summary": ep.get("summary") or ep["handler_name"],
            "operationId": operation_id,
            "responses": {},
        }

        if ep.get("description"):
            operation["description"] = ep["description"]
        if ep.get("tags"):
            operation["tags"] = ep["tags"]

        # Parameters
        parameters: List[Dict[str, Any]] = []
        for p in ep.get("path_params", []):
            parameters.append(p)
        for q in ep.get("query_params", []):
            parameters.append(q)

        if parameters:
            operation["parameters"] = parameters

        # Request Body
        body = ep.get("body_param")
        if body:
            schema_ref = body["schema"]
            example_data = None
            if "$ref" in schema_ref:
                model_name = schema_ref["$ref"].split("/")[-1]
                if model_name in schemas and "example" in schemas[model_name]:
                    example_data = schemas[model_name]["example"]
            if example_data is None:
                example_data = _generate_example_value(body["name"], body["type"])

            content_obj: Dict[str, Any] = {"schema": schema_ref}
            if example_data:
                content_obj["example"] = example_data

            operation["requestBody"] = {
                "required": body.get("required", True),
                "content": {"application/json": content_obj},
            }

        # Responses
        status_code = str(ep.get("status_code", 200))
        resp_model = ep.get("response_model", "")

        if status_code == "204":
            operation["responses"][status_code] = {"description": "No Content"}
        else:
            resp_content: Dict[str, Any] = {}
            if resp_model:
                resp_schema = _map_python_type_to_openapi(resp_model)
                example_resp = None
                if "$ref" in resp_schema:
                    m_name = resp_schema["$ref"].split("/")[-1]
                    if m_name in schemas and "example" in schemas[m_name]:
                        example_resp = schemas[m_name]["example"]
                elif resp_schema.get("type") == "array":
                    item_schema = resp_schema.get("items", {})
                    if "$ref" in item_schema:
                        m_name = item_schema["$ref"].split("/")[-1]
                        if m_name in schemas and "example" in schemas[m_name]:
                            example_resp = [schemas[m_name]["example"]]
                    else:
                        example_resp = ["item_1", "item_2"]

                resp_obj: Dict[str, Any] = {"schema": resp_schema}
                if example_resp is not None:
                    resp_obj["example"] = example_resp
                resp_content["application/json"] = resp_obj
            else:
                resp_content["application/json"] = {
                    "schema": {"type": "object"},
                    "example": {"status": "success"},
                }

            operation["responses"][status_code] = {
                "description": "Successful Response",
                "content": resp_content,
            }

        paths[path][method] = operation

    spec: Dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": version,
            "description": description,
        },
        "paths": paths,
    }

    if schemas:
        spec["components"] = {"schemas": schemas}

    return spec


def generate_swagger_html(title: str = "DevDocs API Explorer") -> str:
    """Generate standalone HTML document embedding Swagger UI via CDN."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui.css">
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #fafafa;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      display: none;
    }}
    .devdocs-banner {{
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      color: #ffffff;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 2px solid #3b82f6;
    }}
    .devdocs-banner h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.5px;
    }}
    .devdocs-banner .badge {{
      background: #3b82f6;
      color: #ffffff;
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-weight: 500;
    }}
  </style>
</head>
<body>
  <div class="devdocs-banner">
    <h1>DevDocs AI — Interactive API Documentation</h1>
    <span class="badge">OpenAPI 3.0</span>
  </div>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui-bundle.js" charset="UTF-8"></script>
  <script src="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui-standalone-preset.js" charset="UTF-8"></script>
  <script>
    window.onload = function() {{
      window.ui = SwaggerUIBundle({{
        url: "../openapi.yaml",
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        plugins: [
          SwaggerUIBundle.plugins.DownloadUrl
        ],
        layout: "BaseLayout",
        defaultModelsExpandDepth: 2,
        defaultModelExpandDepth: 2,
        docExpansion: "list",
        filter: true,
        showExtensions: true,
        showCommonExtensions: true
      }});
    }};
  </script>
</body>
</html>
"""


# ----------------------------------------------------------------------
# Standard Agent Interface Contract
# ----------------------------------------------------------------------

def run(context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Standard agent interface entrypoint.
    
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
    package_version = context.get("package_version") or context.get("packageVersion") or "1.0.0"

    try:
        # Scan endpoints
        endpoints, schemas = scan_endpoints(repo_path)
        
        # Build OpenAPI Spec
        openapi_spec = generate_openapi_spec(
            endpoints=endpoints,
            schemas=schemas,
            title=context.get("title", "DevDocs AI Generated API"),
            version=package_version,
            description="Auto-generated OpenAPI 3.0 specification from FastAPI source code.",
        )

        # Ensure output directories
        docs_api_dir = os.path.join(repo_path, "docs", "api")
        swagger_dir = os.path.join(docs_api_dir, "swagger-ui")
        os.makedirs(swagger_dir, exist_ok=True)

        # Write openapi.yaml
        openapi_file_path = os.path.join(docs_api_dir, "openapi.yaml")
        with open(openapi_file_path, "w", encoding="utf-8") as f:
            yaml.dump(openapi_spec, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        files_written.append("docs/api/openapi.yaml")

        # Write swagger-ui/index.html
        swagger_html_path = os.path.join(swagger_dir, "index.html")
        with open(swagger_html_path, "w", encoding="utf-8") as f:
            f.write(generate_swagger_html())
        files_written.append("docs/api/swagger-ui/index.html")

        file_count = len(set(ep["file"] for ep in endpoints if "file" in ep))
        endpoint_count = len(endpoints)
        summary = f"Documented {endpoint_count} endpoints across {file_count} route files"

        duration_ms = int((time.time() - start_time) * 1000)

        return {
            "agent": "api-doc-generator",
            "status": "success",
            "duration_ms": duration_ms,
            "files_written": files_written,
            "summary": summary,
            "issues": issues,
        }

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        issues.append(f"Fatal error during API documentation generation: {str(e)}")
        return {
            "agent": "api-doc-generator",
            "status": "failed",
            "duration_ms": duration_ms,
            "files_written": files_written,
            "summary": f"Failed to generate API documentation: {str(e)}",
            "issues": issues,
        }
