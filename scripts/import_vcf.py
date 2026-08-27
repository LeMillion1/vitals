#!/usr/bin/env python3
"""Genotek (and generic) VCF importer for the genetics reference table — CLI.

The parsing core now lives in :mod:`vitals.services.genetics.vcf` (so the web
router and this CLI share one implementation and ``web/`` never imports
``scripts/``). This module is the thin command-line + DB wrapper around it and
re-exports the core names for backward compatibility.

Usage:
    python -m scripts.import_vcf path/to/genome.vcf --actor-username owner
    python -m scripts.import_vcf path/to/genome.vcf --actor-username owner --only-interpreted
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

# Re-export the pure parsing core (kept for backward-compat imports).
from vitals.services.genetics.vcf import (  # noqa: F401
    INTERPRETATIONS,
    ParsedVariant,
    interpret,
    iter_parsed,
    parse_vcf_line,
)


async def _import(path: str, only_interpreted: bool, actor_username: str) -> int:
    from vitals.config import load_config
    from vitals.database import create_session_factory
    from vitals.services.conflicts import engine
    from vitals.services.genetics import variants as variant_records

    raw_variants: list[ParsedVariant] = []
    curated_variants: list[ParsedVariant] = []
    truncated = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            variant = parse_vcf_line(line)
            if variant is None:
                continue
            if len(raw_variants) < variant_records.MAX_RAW_VARIANTS:
                raw_variants.append(variant)
            else:
                truncated = True
            if variant.rsid in INTERPRETATIONS:
                curated_variants.append(variant)

    config = load_config()
    factory = create_session_factory(config)

    async with factory() as session:
        try:
            context = await engine.resolve_legacy_conflict_write_context(
                session,
                actor_username=actor_username,
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=context,
            )
            summary = await variant_records.ingest_vcf_batch(
                session,
                filename=Path(path).name,
                curated_variants=curated_variants,
                raw_variants=raw_variants,
                only_interpreted=only_interpreted,
                truncated=truncated,
                identity=context.identity,
                prepared_conflict_write=prepared,
                include_legacy_unowned=True,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return summary.imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a VCF into genetic_variants.")
    parser.add_argument("vcf_path", help="Path to the .vcf file")
    parser.add_argument(
        "--only-interpreted",
        action="store_true",
        help="Normalize only variants with a curated marker (raw capture remains).",
    )
    parser.add_argument(
        "--actor-username",
        required=True,
        help="Authenticated owner username responsible for this import.",
    )
    args = parser.parse_args()
    count = asyncio.run(
        _import(args.vcf_path, args.only_interpreted, args.actor_username)
    )
    print(f"Imported/updated {count} variants.")


if __name__ == "__main__":
    main()
