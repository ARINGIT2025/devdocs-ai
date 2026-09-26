"""
Unit and integration tests for API Documentation Generator Agent (api_agent.py).
"""

import os
import tempfile
import pytest
import yaml

from app.agents.api_agent import (
    scan_endpoints,
    generate_openapi_spec,
    generate_swagger_html,
    run,
)


@pytest.fixture
def fixture_repo_path():
    """Returns absolute path to sample_fastapi_app fixture directory."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, "fixtures", "sample_fastapi_app")


def test_scan_endpoints_finds_all_routes(fixture_repo_path):
    """Test AST scanner detects all defined routes in sample FastAPI app."""
    routes, schemas = scan_endpoints(fixture_repo_path)
    
    assert len(routes) >= 7, f"Expected at least 7 routes, found {len(routes)}"

    # Check method coverage
    methods = set(r["method"].lower() for r in routes)
    assert "get" in methods
    assert "post" in methods
    assert "put" in methods
    assert "delete" in methods

    # Check paths detected
    paths = set(r["full_path"] for r in routes)
    assert "/health" in paths
    assert "/api/v1/items" in paths
    assert "/api/v1/items/{item_id}" in paths
    assert "/api/v1/users" in paths or "/users" in paths or "/api/v1/users/{user_id}" in paths


def test_scan_endpoints_extracts_parameters_and_body(fixture_repo_path):
    """Test scanner accurately extracts path params, query params, and body models."""
    routes, schemas = scan_endpoints(fixture_repo_path)
    
    # Check items get endpoint query params
    list_items_ep = next((r for r in routes if r["path"] == "/api/v1/items" and r["method"] == "get"), None)
    assert list_items_ep is not None
    q_names = [q["name"] for q in list_items_ep["query_params"]]
    assert "skip" in q_names
    assert "limit" in q_names
    assert "q" in q_names

    # Check get item path param
    get_item_ep = next((r for r in routes if "items/{item_id}" in r["path"] and r["method"] == "get"), None)
    assert get_item_ep is not None
    p_names = [p["name"] for p in get_item_ep["path_params"]]
    assert "item_id" in p_names

    # Check post item body
    post_item_ep = next((r for r in routes if r["path"] == "/api/v1/items" and r["method"] == "post"), None)
    assert post_item_ep is not None
    assert post_item_ep["body_param"] is not None
    assert post_item_ep["body_param"]["type"] == "ItemCreate"
    assert post_item_ep["status_code"] == 201


def test_scan_endpoints_extracts_schemas(fixture_repo_path):
    """Test extraction of Pydantic model schemas."""
    routes, schemas = scan_endpoints(fixture_repo_path)
    
    assert "ItemCreate" in schemas
    assert "ItemResponse" in schemas
    assert "UserCreate" in schemas
    assert "UserResponse" in schemas

    item_create = schemas["ItemCreate"]
    assert "name" in item_create["properties"]
    assert "price" in item_create["properties"]
    assert "is_active" in item_create["properties"]
    assert "required" in item_create
    assert "name" in item_create["required"]


def test_generate_openapi_spec_structure(fixture_repo_path):
    """Test OpenAPI 3.0.3 specification structure."""
    routes, schemas = scan_endpoints(fixture_repo_path)
    spec = generate_openapi_spec(
        endpoints=routes,
        schemas=schemas,
        title="Test API",
        version="2.0.0",
    )

    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["title"] == "Test API"
    assert spec["info"]["version"] == "2.0.0"
    assert "paths" in spec
    assert len(spec["paths"]) > 0
    assert "components" in spec
    assert "schemas" in spec["components"]


def test_generate_swagger_html():
    """Test standalone Swagger UI HTML generation."""
    html = generate_swagger_html(title="Custom Title")
    assert "<!DOCTYPE html>" in html
    assert "swagger-ui" in html
    assert "../openapi.yaml" in html
    assert "SwaggerUIBundle" in html


def test_api_agent_run_contract():
    """Test standard agent interface run(context) output contract."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Copy fixture into temp directory to simulate full repo
        import shutil
        app_dir = os.path.join(temp_dir, "app")
        os.makedirs(app_dir, exist_ok=True)
        fixture_src = os.path.join(os.path.dirname(__file__), "fixtures", "sample_fastapi_app")
        shutil.copytree(fixture_src, os.path.join(app_dir, "sample"), dirs_exist_ok=True)

        context = {
            "repo_path": temp_dir,
            "changed_files": ["app/sample/main.py"],
            "diff": "",
            "package_version": "1.2.3",
        }

        result = run(context)

        # Assert contract shape
        assert result["agent"] == "api-doc-generator"
        assert result["status"] == "success"
        assert isinstance(result["duration_ms"], int)
        assert result["duration_ms"] >= 0
        assert isinstance(result["files_written"], list)
        assert "docs/api/openapi.yaml" in result["files_written"]
        assert "docs/api/swagger-ui/index.html" in result["files_written"]
        assert "summary" in result
        assert isinstance(result["issues"], list)

        # Check files created on disk
        openapi_path = os.path.join(temp_dir, "docs", "api", "openapi.yaml")
        swagger_path = os.path.join(temp_dir, "docs", "api", "swagger-ui", "index.html")

        assert os.path.exists(openapi_path)
        assert os.path.exists(swagger_path)

        with open(openapi_path, "r", encoding="utf-8") as f:
            loaded_spec = yaml.safe_load(f)
            assert loaded_spec["openapi"] == "3.0.3"
            assert loaded_spec["info"]["version"] == "1.2.3"
