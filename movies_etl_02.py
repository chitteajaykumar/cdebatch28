"""
Movies ETL Pipeline
-------------------
Loads two raw movie datasets, cleans and standardizes them, merges them into
a single dataset, applies filters/transformations, and writes the results
to CSV files.

Input files:
    movies_01.csv                          (5,000 rows, mostly clean)
    movies_data_engineering_project.csv    (5,250 rows, messy: "Null" strings,
                                             mixed units/currency, string dtypes)

Output files (written to OUTPUT_DIR):
    movies_clean_full.csv       -> full cleaned & merged dataset
    movies_filtered_top.csv     -> filtered subset (see FILTER CRITERIA below)
    movies_summary_by_genre.csv -> aggregated summary table
"""

import pandas as pd
import numpy as np

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
FILE_1 = "/mnt/user-data/uploads/movies_01.csv"
FILE_2 = "/mnt/user-data/uploads/movies_data_engineering_project.csv"
OUTPUT_DIR = "/mnt/user-data/outputs"

# Currency conversion to USD (approximate static rates for this exercise)
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012}

# Unit multipliers -> normalize everything to "Millions"
UNIT_TO_MILLIONS = {
    "million": 1,
    "millions": 1,
    "crore": 10,     # 1 crore = 10 million
    "crores": 10,
    "billion": 1000,
    "billions": 1000,
}

MIN_YEAR, MAX_YEAR = 1990, 2025


def load_and_standardize(path, rename_map=None, has_id=False):
    """Load a CSV, rename columns to a common schema, and replace the
    literal string 'Null' (and blanks) with real NaN values."""
    df = pd.read_csv(path)
    if rename_map:
        df = df.rename(columns=rename_map)
    df = df.replace(["Null", "null", "NULL", ""], np.nan)
    if not has_id:
        df["movie_id"] = np.nan
    return df


def clean_types(df):
    """Coerce columns to their correct dtypes, tolerating bad/missing values."""
    df["release_year"] = pd.to_numeric(df["release_year"], errors="coerce")
    df["imdb_rating"] = pd.to_numeric(df["imdb_rating"], errors="coerce")
    df["budget"] = pd.to_numeric(df["budget"], errors="coerce")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")

    for col in ["movie_name", "industry", "studio", "unit", "currency", "language"]:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()

    df["unit"] = df["unit"].str.lower()
    df["currency"] = df["currency"].str.upper()
    return df


def normalize_financials(df):
    """Convert budget/revenue into a single comparable unit: USD millions."""
    unit_mult = df["unit"].map(UNIT_TO_MILLIONS)
    fx_rate = df["currency"].map(FX_TO_USD)

    df["budget_usd_millions"] = (df["budget"] * unit_mult * fx_rate).round(2)
    df["revenue_usd_millions"] = (df["revenue"] * unit_mult * fx_rate).round(2)
    df["profit_usd_millions"] = (
        df["revenue_usd_millions"] - df["budget_usd_millions"]
    ).round(2)
    return df


def filter_valid_rows(df):
    """Drop rows that are unusable after cleaning: missing key fields,
    out-of-range years, impossible ratings, or non-positive budget/revenue."""
    mask = (
        df["movie_name"].notna()
        & df["release_year"].between(MIN_YEAR, MAX_YEAR)
        & df["imdb_rating"].between(0, 10)
        & df["budget_usd_millions"].gt(0)
        & df["revenue_usd_millions"].gt(0)
        & df["currency"].notna()
        & df["unit"].notna()
    )
    return df.loc[mask].copy()


def dedupe(df):
    """Remove duplicate movies, keeping the most complete / recent record."""
    df["_completeness"] = df.notna().sum(axis=1)
    df = df.sort_values("_completeness", ascending=False)
    df = df.drop_duplicates(subset=["movie_name", "release_year"], keep="first")
    return df.drop(columns="_completeness")


def main():
    # 1. Load both sources into a common schema -----------------------------
    df1 = load_and_standardize(FILE_1, has_id=True)
    df2 = load_and_standardize(FILE_2, rename_map={"movie": "movie_name"})

    common_cols = [
        "movie_id", "movie_name", "industry", "release_year", "imdb_rating",
        "studio", "budget", "revenue", "unit", "currency", "language",
    ]
    df1 = df1[common_cols]
    df2 = df2[common_cols]

    # 2. Combine sources ------------------------------------------------------
    combined = pd.concat([df1, df2], ignore_index=True)
    combined["source"] = ["movies_01"] * len(df1) + ["movies_de_project"] * len(df2)

    # 3. Clean dtypes & normalize financial figures ---------------------------
    combined = clean_types(combined)
    combined = normalize_financials(combined)

    # 4. Filter out unusable rows ---------------------------------------------
    combined = filter_valid_rows(combined)

    # 5. Deduplicate ------------------------------------------------------------
    combined = dedupe(combined)

    # 6. Feature engineering ---------------------------------------------------
    combined["roi_pct"] = (
        (combined["revenue_usd_millions"] - combined["budget_usd_millions"])
        / combined["budget_usd_millions"] * 100
    ).round(1)
    combined["is_hit"] = combined["revenue_usd_millions"] > combined["budget_usd_millions"]
    combined["decade"] = (combined["release_year"] // 10 * 10).astype(int)
    combined["rating_bucket"] = pd.cut(
        combined["imdb_rating"],
        bins=[0, 4, 6, 8, 10],
        labels=["Poor", "Average", "Good", "Excellent"],
    )

    final_cols = [
        "movie_id", "movie_name", "industry", "release_year", "decade",
        "imdb_rating", "rating_bucket", "studio", "language",
        "budget_usd_millions", "revenue_usd_millions", "profit_usd_millions",
        "roi_pct", "is_hit", "source",
    ]
    combined = combined[final_cols].sort_values(
        ["release_year", "movie_name"]
    ).reset_index(drop=True)

    # 7. Build a filtered "top movies" view -------------------------------------
    filtered_top = combined[
        (combined["imdb_rating"] >= 7.0)
        & (combined["is_hit"])
        & (combined["release_year"] >= 2010)
    ].sort_values("profit_usd_millions", ascending=False)

    # 8. Summary aggregation by industry -----------------------------------------
    summary = (
        combined.groupby("industry", observed=True)
        .agg(
            movie_count=("movie_name", "count"),
            avg_rating=("imdb_rating", "mean"),
            avg_budget_usd_m=("budget_usd_millions", "mean"),
            avg_revenue_usd_m=("revenue_usd_millions", "mean"),
            hit_rate_pct=("is_hit", "mean"),
        )
        .round(2)
    )
    summary["hit_rate_pct"] = (summary["hit_rate_pct"] * 100).round(1)
    summary = summary.sort_values("avg_revenue_usd_m", ascending=False).reset_index()

    # 9. Write outputs -------------------------------------------------------
    import os
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    combined.to_csv(f"{OUTPUT_DIR}/movies_clean_full.csv", index=False)
    filtered_top.to_csv(f"{OUTPUT_DIR}/movies_filtered_top.csv", index=False)
    summary.to_csv(f"{OUTPUT_DIR}/movies_summary_by_genre.csv", index=False)

    print(f"Rows loaded (raw):       {len(df1) + len(df2)}")
    print(f"Rows after clean/dedupe: {len(combined)}")
    print(f"Rows in top-movies view: {len(filtered_top)}")
    print(f"Industries summarized:   {len(summary)}")


if __name__ == "__main__":
    main()
