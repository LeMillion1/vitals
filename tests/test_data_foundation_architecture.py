"""Architecture contracts for files, raw data-lake, and external API tokens."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "vitals" / "services"

PACKAGE_MANIFESTS = {
    "files": {
        "__init__.py",
        "contracts.py",
        "lifecycle.py",
        "queries.py",
        "upload_references.py",
    },
    "data_lake": {"__init__.py", "contracts.py", "raw_payloads.py", "sweep.py"},
    "external_api": {"__init__.py", "tokens.py"},
}

PACKAGE_DAGS = {
    "files": {
        "contracts": set(),
        "lifecycle": {"contracts"},
        "queries": {"contracts"},
        "upload_references": set(),
    },
    "data_lake": {
        "contracts": set(),
        "raw_payloads": {"contracts"},
        "sweep": set(),
    },
    "external_api": {"tokens": set()},
}

PUBLIC_MANIFESTS = {
    ("files", "contracts"): {
        "FileAssetConflictError",
        "FileAssetNotFoundError",
        "FileAssetServiceError",
        "FileAssetSubjectNotFoundError",
        "FileAssetUploaderNotFoundError",
        "FileAssetValidationError",
        "coerce_purpose",
        "local_asset_is_live",
        "local_asset_is_retired",
        "validate_media_type",
        "validate_sha256",
        "validate_size",
        "validate_storage_ref",
        "validate_uuid",
    },
    ("files", "lifecycle"): {
        "mark_legacy_local_deleted",
        "mark_local_deleted",
        "register_legacy_local",
        "register_private_local",
    },
    ("files", "queries"): {
        "opaque_keys_for",
        "resolve_for_download",
        "resolve_local_asset",
    },
    ("files", "upload_references"): {
        "OwnedUploadReference",
        "UploadOwnershipError",
        "resolve_owned_upload_reference",
    },
    ("data_lake", "contracts"): {
        "RawPayloadAmbiguityError",
        "RawPayloadConflictError",
        "RawPayloadReferenceError",
        "RawPayloadReferenceLifecycleError",
        "RawPayloadReferenceNotFoundError",
        "RawPayloadReferenceOwnershipError",
        "RawPayloadServiceError",
        "RawPayloadValidationError",
        "validate_owned_inputs",
    },
    ("data_lake", "raw_payloads"): {"upsert_owned_raw_payload"},
    ("data_lake", "sweep"): {"sweep_domain", "sweep_pending_job"},
    ("external_api", "tokens"): {
        "ExternalApiTokenError",
        "IssuedToken",
        "NotTheSubjectOwner",
        "TokenNotFound",
        "TooManyTokens",
        "any_token_exists",
        "authenticate",
        "is_live",
        "issue",
        "list_for_subject",
        "revoke",
        "sole_subject_id",
    },
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _public_definitions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def _package_dependencies(package: str, path: Path) -> set[str]:
    prefix = f"vitals.services.{package}."
    dependencies: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(prefix):
                dependencies.add(node.module.removeprefix(prefix).split(".", 1)[0])
    return dependencies


def test_data_foundation_packages_have_exact_leaf_manifests() -> None:
    for package, expected in PACKAGE_MANIFESTS.items():
        assert {path.name for path in (SERVICES / package).glob("*.py")} == expected
    for (package, leaf), expected in PUBLIC_MANIFESTS.items():
        assert _public_definitions(SERVICES / package / f"{leaf}.py") == expected


def test_data_foundation_package_imports_follow_exact_dags() -> None:
    for package, expected in PACKAGE_DAGS.items():
        actual = {
            leaf: _package_dependencies(package, SERVICES / package / f"{leaf}.py")
            for leaf in expected
        }
        assert actual == expected


def test_data_foundation_has_no_flat_shims_or_old_imports() -> None:
    retired = {
        "external_api_token_service",
        "file_asset_service",
        "raw_payload_service",
        "upload_ownership_service",
    }
    for name in retired:
        assert not (SERVICES / f"{name}.py").exists()

    offenders: list[str] = []
    for root_name in ("vitals", "web", "tests", "scripts"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom):
                    if node.module == "vitals.services" and any(
                        alias.name in retired for alias in node.names
                    ):
                        offenders.append(path.relative_to(ROOT).as_posix())
                    if node.module and node.module.removeprefix("vitals.services.") in retired:
                        offenders.append(path.relative_to(ROOT).as_posix())
                elif isinstance(node, ast.Import) and any(
                    alias.name.removeprefix("vitals.services.") in retired
                    for alias in node.names
                ):
                    offenders.append(path.relative_to(ROOT).as_posix())
    assert sorted(set(offenders)) == []
