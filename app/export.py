import time

import pandas as pd

from . import config
from .models import HEADERS, Result


def export(results: list[Result], name: str):
    ts = time.strftime("%Y%m%d_%H%M%S")
    df = pd.DataFrame([r.to_row() for r in results], columns=[h for _, h in HEADERS])
    xlsx = config.OUTPUT_DIR / f"{name}_{ts}.xlsx"
    csv_path = config.OUTPUT_DIR / f"{name}_{ts}.csv"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Results")
        ws = w.sheets["Results"]
        for i, col in enumerate(df.columns, start=1):
            longest = max([len(str(col))] + [len(str(v)) for v in df[col]])
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(longest + 2, 60)
        ws.freeze_panes = "A2"
    df.to_csv(csv_path, index=False)
    return xlsx, csv_path