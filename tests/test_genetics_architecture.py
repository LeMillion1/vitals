"""Static package boundaries for the Genetics bounded context."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "vitals" / "services" / "genetics"

LEAF_MANIFEST = {
    "__init__.py",
    "contracts.py",
    "queries.py",
    "reparse.py",
    "validation.py",
    "vcf.py",
    "vcf_ingestion.py",
    "writes.py",
}

TOP_LEVEL_DAG = {
    "contracts": set(),
    "vcf": set(),
    "validation": {"contracts", "vcf"},
    "queries": {"contracts", "validation"},
    "writes": {"contracts", "validation"},
    "vcf_ingestion": {"contracts", "validation", "vcf", "writes"},
    "reparse": {"contracts", "validation", "vcf_ingestion"},
}

PUBLIC_OWNERS = {
    "contracts": {
        "GeneticsOwnershipError",
        "GeneticsRawProvenanceError",
        "GeneticsValidationError",
        "MAX_LIST_LIMIT",
        "MAX_RAW_VARIANTS",
        "PATCH_UNSET",
        "VcfIngestSummary",
    },
    "queries": {
        "bounded_variants",
        "get_variant",
        "legacy_unowned_present",
        "list_variants",
        "resolve_variants_scoped",
    },
    "writes": {"add_variant", "delete_variant", "upsert_by_rsid"},
    "vcf_ingestion": {"ingest_vcf_batch", "store_raw_vcf"},
    "reparse": {"reparse_owned_pending"},
    "vcf": {"INTERPRETATIONS", "ParsedVariant", "interpret", "parse_vcf_line"},
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _defined_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in _tree(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _top_level_dependencies(path: Path) -> set[str]:
    prefix = "vitals.services.genetics."
    return {
        node.module.removeprefix(prefix).split(".", 1)[0]
        for node in _tree(path).body
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix)
    }


def test_genetics_has_exact_leaf_manifest_without_flat_variants() -> None:
    assert {path.name for path in PACKAGE_ROOT.glob("*.py")} == LEAF_MANIFEST
    assert not (PACKAGE_ROOT / "variants.py").exists()


def test_genetics_leaf_imports_follow_exact_dag() -> None:
    assert {
        leaf: _top_level_dependencies(PACKAGE_ROOT / f"{leaf}.py") for leaf in TOP_LEVEL_DAG
    } == TOP_LEVEL_DAG


def test_genetics_leaf_size_is_bounded() -> None:
    sizes = {
        path.name: len(path.read_text().splitlines())
        for path in PACKAGE_ROOT.glob("*.py")
        if path.name != "__init__.py"
    }
    assert max(sizes.values()) <= 500, sizes


def test_genetics_public_operations_have_one_explicit_owner() -> None:
    definitions = {leaf: _defined_names(PACKAGE_ROOT / f"{leaf}.py") for leaf in PUBLIC_OWNERS}
    for owner, names in PUBLIC_OWNERS.items():
        for name in names:
            assert name in definitions[owner], (owner, name)
            assert all(
                name not in other_names
                for leaf, other_names in definitions.items()
                if leaf != owner
            ), name


def test_python_callers_do_not_import_removed_variants_module() -> None:
    forbidden = "vitals.services.genetics.variants"
    for root_name in ("vitals", "web", "scripts", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom):
                    assert node.module != forbidden, path
                    if node.module == "vitals.services.genetics":
                        assert all(alias.name != "variants" for alias in node.names), path
                elif isinstance(node, ast.Import):
                    assert all(alias.name != forbidden for alias in node.names), path
