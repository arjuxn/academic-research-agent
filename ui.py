"""PySide6 desktop UI for the three Academic Research Agent modes."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget, QHeaderView
)

from app import config, llm
from app.discover import discover_universities
from app.export import export
from app.models import HEADERS, Result
from app.pipeline import run_university
from app.store import Store


def load_batch_items(csv_path: Path, default_discipline: str = "") -> list[dict]:
    raw = csv_path.read_bytes()
    text = raw.decode("utf-8-sig")
    items = []
    for row in csv.DictReader(text.splitlines()):
        # Header names are intentionally forgiving about capitalization and spaces.
        normalized = {str(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = normalized.get("university name") or normalized.get("university") or normalized.get("college") or ""
        discipline = normalized.get("discipline") or default_discipline
        if not name or not discipline:
            continue
        items.append({
            "university": name,
            "discipline": discipline,
            "geo": normalized.get("geo") or normalized.get("region") or "",
            "country": normalized.get("country/region") or normalized.get("country") or "",
            "website": normalized.get("website") or "",
        })
    return items


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "run"


class RunWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)
    completed = Signal(object, str, str)
    failed = Signal(str)

    def __init__(self, mode: str, args: dict):
        super().__init__()
        self.mode = mode
        self.args = args

    def _process_items(self, run_id: str, items: list[dict], retry_failed: bool, output_name: str):
        store = Store()
        try:
            store.add_items(run_id, items)
            todo = store.pending(run_id, retry_failed)
            self.log.emit(f"Found {len(items)} input row(s); {len(todo)} need processing.")
            for index, item in enumerate(todo, start=1):
                if self.isInterruptionRequested():
                    self.log.emit("Cancellation requested. Finished rows have been saved and can be resumed.")
                    break
                self.progress.emit(index - 1, len(todo), item["university"])
                self.log.emit(f"[{index}/{len(todo)}] Processing {item['university']} — {item['discipline']}")
                result = run_university(
                    item["university"], item["discipline"], item.get("geo", ""),
                    item.get("country", ""), item.get("website", "")
                )
                store.save(run_id, item["key"], result)
                self.progress.emit(index, len(todo), item["university"])
                self.log.emit(f"    {result.status}: {result.contact_name}" +
                              (f" | {result.email}" if result.email else ""))
            results = store.results(run_id)
        finally:
            store.conn.close()
        xlsx, csv_path = export(results, output_name)
        self.completed.emit(results, str(xlsx), str(csv_path))

    def run(self):
        try:
            if self.mode == "single":
                a = self.args
                self.log.emit(f"Running single-college search for {a['university']}…")
                self.progress.emit(0, 1, a["university"])
                result = run_university(
                    a["university"], a["discipline"], geo=a.get("geo", ""),
                    country=a.get("country", ""), website=a.get("website", ""),
                    campus=a.get("campus", ""), show_candidates=a.get("show_candidates", False)
                )
                results = [result]
                self.progress.emit(1, 1, a["university"])
                self.log.emit(f"Finished: {result.status}")
                xlsx, csv_path = export(results, "single")
                self.completed.emit(results, str(xlsx), str(csv_path))
                return

            if self.mode == "geo":
                a = self.args
                self.log.emit(f"Discovering institutions for {a['discipline']} in {a['region']}…")
                universities = discover_universities(a["discipline"], a["region"], a["limit"])
                if not universities:
                    raise RuntimeError("No universities were discovered. Try a broader region or a different discipline.")
                items = [{
                    "university": u["name"], "discipline": a["discipline"], "geo": a["region"],
                    "country": a.get("country", ""), "website": u.get("website", "")
                } for u in universities]
                run_id = "geo-" + hashlib.sha1(
                    f"{a['discipline']}|{a['region']}|{a.get('country','')}".encode()
                ).hexdigest()[:12]
                self._process_items(run_id, items, a.get("retry_failed", False),
                                    "geo_" + safe_slug(a["discipline"] + "-" + a["region"]))
                return

            if self.mode == "batch":
                a = self.args
                csv_path = Path(a["csv_path"])
                if not csv_path.exists():
                    raise RuntimeError(f"CSV file does not exist: {csv_path}")
                raw = csv_path.read_bytes()
                items = load_batch_items(csv_path, a.get("discipline", ""))
                if not items:
                    raise RuntimeError("No usable CSV rows. Include University Name and Discipline, or set a default discipline.")
                run_id = "batch-" + hashlib.sha1(raw + a.get("discipline", "").encode()).hexdigest()[:12]
                self._process_items(run_id, items, a.get("retry_failed", False), "batch")
                return

            raise RuntimeError(f"Unknown mode: {self.mode}")
        except Exception as exc:
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class OllamaCheckWorker(QThread):
    checked = Signal(bool)

    def run(self):
        try:
            self.checked.emit(llm.available(refresh=True))
        except Exception:
            self.checked.emit(False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Academic Research Agent")
        self.resize(1120, 760)
        self.setMinimumSize(900, 650)
        self.worker = None
        self.health_worker = None
        self.results: list[Result] = []
        self.xlsx_path = ""
        self.csv_path = ""

        central = QWidget()
        self.setCentralWidget(central)
        central.setObjectName("centralWidget")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel("Academic Research Agent")
        title.setObjectName("title")
        subtitle = QLabel("Local-first faculty contact discovery · Ollama / Qwen3 4B")
        subtitle.setObjectName("subtitle")
        title_col = QVBoxLayout()
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()
        self.llm_status = QLabel("Checking local model…")
        self.llm_status.setObjectName("status")
        header.addWidget(self.llm_status)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 0)
        self.tabs.addTab(self._build_single_tab(), "1  Single College")
        self.tabs.addTab(self._build_geo_tab(), "2  Discipline + GEO")
        self.tabs.addTab(self._build_batch_tab(), "3  Batch CSV")

        result_group = QGroupBox("Results")
        result_layout = QVBoxLayout(result_group)
        self.result_table = QTableWidget(0, 7)
        self.result_table.setHorizontalHeaderLabels(
            ["University", "Department", "Contact", "Role", "Email", "Method", "Status"]
        )
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        result_layout.addWidget(self.result_table)
        actions = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        actions.addWidget(self.progress, 1)
        self.open_output_btn = QPushButton("Open output folder")
        self.open_output_btn.clicked.connect(self.open_output)
        actions.addWidget(self.open_output_btn)
        result_layout.addLayout(actions)
        outer.addWidget(result_group, 3)

        log_group = QGroupBox("Run log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log_view)
        outer.addWidget(log_group, 1)

        toolbar = self.addToolBar("Actions")
        refresh_action = QAction("Check Ollama", self)
        refresh_action.triggered.connect(self.check_ollama)
        toolbar.addAction(refresh_action)
        self.cancel_action = QAction("Stop after current item", self)
        self.cancel_action.setEnabled(False)
        self.cancel_action.triggered.connect(self.cancel_run)
        toolbar.addAction(self.cancel_action)

        # Explicit foreground/background colours are important on Windows, where
        # a dark system palette can otherwise make text white on white inputs.
        self.setStyleSheet("""
            QWidget { color: #182235; font-size: 13px; }
            QMainWindow, QWidget#centralWidget { background: #f3f6fb; }
            QLabel { color: #273449; background: transparent; }
            QLabel#title { font-size: 25px; font-weight: 750; color: #13233b; }
            QLabel#subtitle { color: #58677d; font-size: 12px; }
            QLabel#status { color: #244675; padding: 9px 12px; background: #e5edf9; border: 1px solid #d4e1f4; border-radius: 7px; }
            QGroupBox { color: #182235; font-weight: 650; border: 1px solid #d6deea; border-radius: 9px; margin-top: 12px; padding: 14px 12px 12px 12px; background: #ffffff; }
            QGroupBox::title { color: #182235; subcontrol-origin: margin; left: 12px; padding: 0 6px; background: #ffffff; }
            QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {
                color: #182235; selection-color: #ffffff; selection-background-color: #285ca8;
                border: 1px solid #c5cfde; border-radius: 6px; padding: 8px 10px;
                background-color: #ffffff; min-height: 20px;
            }
            QLineEdit::placeholder { color: #7b8798; }
            QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus { border: 2px solid #3976c8; padding: 7px 9px; }
            QComboBox QAbstractItemView { color: #182235; background: #ffffff; selection-background-color: #dce9fb; selection-color: #182235; }
            QPushButton { color: #ffffff; padding: 10px 15px; border: 0; border-radius: 6px; background: #24549a; font-weight: 650; min-height: 20px; }
            QPushButton:hover { background: #1b427d; }
            QPushButton:pressed { background: #153563; }
            QPushButton:disabled { background: #aab6c7; color: #f6f8fb; }
            QTabWidget::pane { border: 1px solid #d6deea; border-radius: 8px; background: #ffffff; top: -1px; }
            QTabBar::tab { color: #3a4a61; background: #e9eef6; padding: 11px 16px; margin-right: 4px; border-top-left-radius: 6px; border-top-right-radius: 6px; }
            QTabBar::tab:selected { color: #ffffff; background: #24549a; font-weight: 650; }
            QTabBar::tab:hover:!selected { background: #dce5f2; }
            QTableWidget { color: #182235; alternate-background-color: #f5f8fc; gridline-color: #e1e7f0; background: #ffffff; border: 1px solid #dce3ed; border-radius: 6px; }
            QHeaderView::section { color: #ffffff; background: #33465f; border: 0; padding: 8px; font-weight: 650; }
            QProgressBar { color: #182235; background: #e7edf5; border: 0; border-radius: 5px; text-align: center; min-height: 18px; }
            QProgressBar::chunk { background: #3976c8; border-radius: 5px; }
            QToolBar { color: #273449; background: #ffffff; border: 0; spacing: 8px; padding: 4px; }
            QToolButton { color: #273449; background: transparent; padding: 6px 8px; }
            QToolButton:hover { background: #e8eef7; border-radius: 4px; }
        """)
        self.check_ollama()

    def _make_form_page(self, title: str, description: str):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        intro = QLabel(description)
        intro.setWordWrap(True)
        intro.setMinimumHeight(32)
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setContentsMargins(14, 18, 14, 14)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(intro)
        layout.addWidget(group)
        layout.addStretch(1)
        return page, form, layout

    def _build_single_tab(self):
        page, form, layout = self._make_form_page(
            "Search one institution",
            "Enter the institution and discipline. Campus and location hints help avoid choosing the wrong constituent college."
        )
        self.single_uni = QLineEdit()
        self.single_uni.setPlaceholderText("e.g. Bharati Vidyapeeth")
        self.single_disc = QLineEdit()
        self.single_disc.setPlaceholderText("e.g. Computer Science and Engineering")
        self.single_campus = QLineEdit()
        self.single_campus.setPlaceholderText("e.g. Pune")
        self.single_geo = QLineEdit()
        self.single_geo.setPlaceholderText("Optional region / GEO")
        self.single_country = QLineEdit()
        self.single_country.setText("India")
        self.single_website = QLineEdit()
        self.single_website.setPlaceholderText("Optional official website URL")
        self.single_candidates = QComboBox()
        self.single_candidates.addItem("No — normal run", False)
        self.single_candidates.addItem("Yes — show candidates in log", True)
        form.addRow("University / college *", self.single_uni)
        form.addRow("Discipline *", self.single_disc)
        form.addRow("Campus", self.single_campus)
        form.addRow("GEO / region", self.single_geo)
        form.addRow("Country / region", self.single_country)
        form.addRow("Official website (optional)", self.single_website)
        form.addRow("Candidate diagnostics", self.single_candidates)
        button = QPushButton("Run Single College")
        button.clicked.connect(self.run_single)
        layout.insertWidget(2, button)
        return page

    def _build_geo_tab(self):
        page, form, layout = self._make_form_page(
            "Discover universities by discipline and region",
            "The agent first discovers institutions, then runs the same department and contact pipeline for each."
        )
        self.geo_disc = QLineEdit()
        self.geo_disc.setPlaceholderText("e.g. Computer Science")
        self.geo_region = QLineEdit()
        self.geo_region.setPlaceholderText("e.g. Maharashtra")
        self.geo_country = QLineEdit()
        self.geo_country.setText("India")
        self.geo_limit = QSpinBox()
        self.geo_limit.setRange(1, 100)
        self.geo_limit.setValue(20)
        self.geo_retry = QComboBox()
        self.geo_retry.addItem("Resume pending / errors only", False)
        self.geo_retry.addItem("Retry previous not-found rows too", True)
        form.addRow("Discipline *", self.geo_disc)
        form.addRow("Region / GEO *", self.geo_region)
        form.addRow("Country / region", self.geo_country)
        form.addRow("Maximum institutions", self.geo_limit)
        form.addRow("Resume behavior", self.geo_retry)
        button = QPushButton("Discover and Run")
        button.clicked.connect(self.run_geo)
        layout.insertWidget(2, button)
        return page

    def _build_batch_tab(self):
        page, form, layout = self._make_form_page(
            "Process a CSV list",
            "CSV headers supported: University Name, GEO, Country/Region, Discipline, and optional Website."
        )
        self.batch_path = QLineEdit(str(Path("input") / "universities.csv"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_csv)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.batch_path, 1)
        path_layout.addWidget(browse)
        self.batch_disc = QLineEdit()
        self.batch_disc.setPlaceholderText("Optional default for rows with no Discipline")
        self.batch_retry = QComboBox()
        self.batch_retry.addItem("Resume pending / errors only", False)
        self.batch_retry.addItem("Retry previous not-found rows too", True)
        form.addRow("Input CSV *", path_row)
        form.addRow("Default discipline", self.batch_disc)
        form.addRow("Resume behavior", self.batch_retry)
        button = QPushButton("Run Batch CSV")
        button.clicked.connect(self.run_batch)
        layout.insertWidget(2, button)
        sample = QPushButton("Create sample CSV")
        sample.clicked.connect(self.create_sample_csv)
        layout.insertWidget(3, sample)
        return page

    def add_log(self, message: str):
        self.log_view.appendPlainText(message)

    def check_ollama(self):
        if self.health_worker and self.health_worker.isRunning():
            return
        self.llm_status.setText("Checking local model…")
        self.health_worker = OllamaCheckWorker()
        self.health_worker.checked.connect(self.ollama_checked)
        self.health_worker.start()

    def ollama_checked(self, ready: bool):
        if ready:
            self.llm_status.setText(f"Local model ready · {config.OLLAMA_MODEL}")
            self.add_log("Ollama check: model ready.")
        else:
            self.llm_status.setText(f"Ollama unavailable · expected {config.OLLAMA_MODEL}")
            self.add_log(
                f"Ollama check: not ready; run `ollama pull {config.OLLAMA_MODEL}` "
                "and ensure Ollama is running. The deterministic crawler can still run without it."
            )

    def _start(self, mode: str, args: dict):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Run in progress", "Wait for the current run to finish.")
            return
        self.results = []
        self.result_table.setRowCount(0)
        self.progress.setRange(0, 0)
        self.cancel_action.setEnabled(True)
        self.add_log("=" * 72)
        self.worker = RunWorker(mode, args)
        self.worker.log.connect(self.add_log)
        self.worker.progress.connect(self.update_progress)
        self.worker.completed.connect(self.run_completed)
        self.worker.failed.connect(self.run_failed)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def run_single(self):
        university, discipline = self.single_uni.text().strip(), self.single_disc.text().strip()
        if not university or not discipline:
            QMessageBox.warning(self, "Missing information", "University/college and discipline are required.")
            return
        self._start("single", {
            "university": university, "discipline": discipline,
            "campus": self.single_campus.text().strip(), "geo": self.single_geo.text().strip(),
            "country": self.single_country.text().strip(), "website": self.single_website.text().strip(),
            "show_candidates": bool(self.single_candidates.currentData())
        })

    def run_geo(self):
        discipline, region = self.geo_disc.text().strip(), self.geo_region.text().strip()
        if not discipline or not region:
            QMessageBox.warning(self, "Missing information", "Discipline and region/GEO are required.")
            return
        self._start("geo", {
            "discipline": discipline, "region": region, "country": self.geo_country.text().strip(),
            "limit": self.geo_limit.value(), "retry_failed": bool(self.geo_retry.currentData())
        })

    def run_batch(self):
        path = self.batch_path.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing CSV", "Choose an input CSV file.")
            return
        self._start("batch", {
            "csv_path": path, "discipline": self.batch_disc.text().strip(),
            "retry_failed": bool(self.batch_retry.currentData())
        })

    def browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose universities CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            self.batch_path.setText(path)

    def create_sample_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Create sample CSV", str(Path("input") / "universities.csv"), "CSV files (*.csv)"
        )
        if not path:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "University Name,GEO,Country/Region,Discipline,Website\n"
            "Bharati Vidyapeeth,Pune,India,Computer Science and Engineering,\n",
            encoding="utf-8-sig"
        )
        self.batch_path.setText(str(target))
        self.add_log(f"Created sample CSV: {target}")

    def update_progress(self, current: int, total: int, label: str):
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, total)
        self.progress.setValue(min(current, total))
        self.progress.setFormat(f"{current}/{total} · {label}")

    def run_completed(self, results, xlsx_path: str, csv_path: str):
        self.results = list(results)
        self.xlsx_path, self.csv_path = xlsx_path, csv_path
        self.result_table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            values = [
                result.university_name, result.target_department, result.contact_name,
                result.contact_type, result.email, result.extraction_method, result.status
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                self.result_table.setItem(row, col, item)
        self.add_log(f"Excel saved: {xlsx_path}")
        self.add_log(f"CSV saved:   {csv_path}")
        successes = sum(1 for r in self.results if r.status == "Success")
        self.add_log(f"Completed {len(self.results)} result row(s); {successes} success(es).")

    def run_failed(self, message: str):
        self.add_log("RUN FAILED:\n" + message)
        QMessageBox.critical(self, "Run failed", message.split("\n", 1)[0])

    def worker_finished(self):
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if self.results else 0)
        self.cancel_action.setEnabled(False)

    def cancel_run(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.add_log("Stop requested. The current institution will finish before stopping.")

    def open_output(self):
        path = str(config.OUTPUT_DIR.resolve())
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            QMessageBox.warning(self, "Could not open folder", f"{path}\n\n{exc}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Academic Research Agent")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
