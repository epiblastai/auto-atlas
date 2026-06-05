"""Shared internal utilities."""

import os


def extract_h5ad_obs_var(h5ad_path: str) -> tuple[str, str]:
    """Write the obs and var dataframes of an h5ad file to separate CSV files.

    The CSVs are written alongside the input, reusing its name: ``foo.h5ad``
    yields ``foo_obs.csv`` and ``foo_var.csv``. The dataframes keep their index
    (cell barcodes for obs, feature ids for var). The file is read in backed
    mode so X is never loaded into memory. Returns ``(obs_csv_path, var_csv_path)``.
    """
    # Imported lazily so the rest of this module does not depend on anndata.
    import anndata as ad

    base = os.path.splitext(h5ad_path)[0]
    obs_csv_path = f"{base}_obs.csv"
    var_csv_path = f"{base}_var.csv"

    adata = ad.read_h5ad(h5ad_path, backed="r")
    adata.obs.to_csv(obs_csv_path)
    adata.var.to_csv(var_csv_path)
    return obs_csv_path, var_csv_path
