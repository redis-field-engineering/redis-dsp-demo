from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import zipfile

import pandas as pd
from huggingface_hub import hf_hub_download

from data.common import write_jsonl


def _parse_impressions(value: str) -> list[tuple[str, int]]:
    parsed: list[tuple[str, int]] = []
    for token in str(value).split():
        if "-" not in token:
            continue
        news_id, label = token.rsplit("-", maxsplit=1)
        parsed.append((news_id, int(label)))
    return parsed


def _parse_history(value: str) -> list[str]:
    if not value or value != value:
        return []
    return str(value).split()


def _fallback_category(news_id: str, bucket_count: int = 24) -> str:
    numeric = int("".join(character for character in news_id if character.isdigit()) or "0")
    return f"topic_{numeric % bucket_count:02d}"


def _mind_zip_name(split: str, variant: str) -> str:
    normalized_variant = variant.lower()
    if normalized_variant not in {"demo", "small", "large"}:
        raise ValueError(f"Unsupported MIND variant: {variant}")
    suffix = "dev" if split in {"dev", "validation"} else split
    return f"MIND{normalized_variant}_{suffix}.zip"


def _load_mind_frames(split: str, variant: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    zip_path = hf_hub_download(
        repo_id="Recommenders/MIND",
        filename=_mind_zip_name(split, variant),
        repo_type="dataset",
    )
    with zipfile.ZipFile(zip_path) as archive:
        with archive.open("behaviors.tsv") as behaviors_file:
            behaviors = pd.read_csv(
                behaviors_file,
                sep="\t",
                header=None,
                names=["impression_id", "user_id", "time", "history", "impressions"],
                dtype=str,
            )
        with archive.open("news.tsv") as news_file:
            news = pd.read_csv(
                news_file,
                sep="\t",
                header=None,
                names=[
                    "news_id",
                    "category",
                    "subcategory",
                    "title",
                    "abstract",
                    "url",
                    "title_entities",
                    "abstract_entities",
                ],
                dtype=str,
            )
    return behaviors, news


def export_mind_translation(
    output_dir: Path,
    split: str = "train",
    sample_size: int = 1500,
    variant: str = "demo",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame, news_frame = _load_mind_frames(split=split, variant=variant)
    frame = frame.head(sample_size).copy()

    user_categories: dict[str, Counter[str]] = defaultdict(Counter)
    translated_rows: list[dict[str, object]] = []
    item_categories = dict(zip(news_frame["news_id"], news_frame["category"], strict=False))

    for row in frame.to_dict(orient="records"):
        history = _parse_history(row.get("history", ""))
        for item in history:
            if item in item_categories:
                user_categories[str(row["user_id"])][item_categories[item]] += 1
        for news_id, label in _parse_impressions(row["impressions"]):
            category = item_categories.get(news_id, _fallback_category(news_id))
            translated_rows.append(
                {
                    "user_id": str(row["user_id"]),
                    "campaign_id": news_id,
                    "label": label,
                    "source_split": split,
                    "source_variant": variant,
                    "translated_geo": "US",
                    "translated_device": "Web",
                    "category": category,
                    "history_length": len(history),
                }
            )

    user_rows = [
        {
            "user_id": user_id,
            "geo": "US",
            "device": "Web",
            "age_bucket": "25-34",
            "interests": {category.lower(): count for category, count in counter.most_common(8)},
            "segments": [f"{category.lower()}_high" for category, _ in counter.most_common(3)],
        }
        for user_id, counter in user_categories.items()
    ]
    interaction_path = output_dir / "mind_translated_interactions.parquet"
    pd.DataFrame(translated_rows).to_parquet(interaction_path, index=False)
    write_jsonl(output_dir / "mind_translated_users.jsonl", user_rows)
    (output_dir / "mind_translation_metadata.json").write_text(
        json.dumps(
            {
                "source_dataset": "Recommenders/MIND",
                "split": split,
                "variant": variant,
                "sample_size": sample_size,
                "rows_exported": len(translated_rows),
                "users_exported": len(user_rows),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return interaction_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate MIND into the DSP domain")
    parser.add_argument("--output-dir", type=Path, default=Path("data/generated/mind"))
    parser.add_argument("--split", default="train")
    parser.add_argument("--sample-size", type=int, default=1500)
    parser.add_argument("--variant", default="demo")
    args = parser.parse_args()
    export_mind_translation(
        args.output_dir,
        split=args.split,
        sample_size=args.sample_size,
        variant=args.variant,
    )


if __name__ == "__main__":
    main()
