from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# -----------------------------
# Настройки по умолчанию
# -----------------------------
APP_VERSION = "mac-batch-2026-06-25"
DEFAULT_MAX_MB = 49.0          # Жесткий лимит на выходной файл. MB = 1 000 000 байт.
TARGET_FILL_RATIO = 0.92       # Целимся ниже лимита, чтобы VBR/контейнер не превысили лимит.
SILENCE_NOISE_DB = -35         # Чем выше число, тем больше мест считается тишиной: -30 мягче, -40 строже.
SILENCE_MIN_SEC = 0.35         # Минимальная длина паузы, чтобы считать ее местом для разреза.
SILENCE_SEARCH_WINDOW_SEC = 20 # Искать паузу плюс/минус столько секунд от расчетной границы.
MIN_SEGMENT_SEC = 2.0          # Защита от слишком коротких фрагментов.
MB = 1_000_000                 # Используем десятичный мегабайт, чтобы безопаснее пройти лимит 50 MB.
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".caf", ".dss", ".ds2",
    ".flac", ".m4a", ".m4b", ".mp3", ".mp4", ".ogg",
    ".opus", ".wav", ".webm", ".wma",
}
# Tk на macOS надежнее принимает список/tuple масок, а не одну строку.
AUDIO_FILE_PATTERNS = tuple(
    [f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS)]
    + [f"*{ext.upper()}" for ext in sorted(AUDIO_EXTENSIONS)]
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class SplitError(RuntimeError):
    pass


def app_dir() -> Path:
    """Папка скрипта или исполняемого файла, если программа собрана через PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def mac_app_external_dir() -> Path | None:
    """
    Для macOS .app возвращает папку, где лежит сам .app.
    Пример: /Users/me/Audio Splitter.app/Contents/MacOS/Audio Splitter
    -> /Users/me
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None

    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
        return exe_dir.parent.parent.parent
    return None


def possible_tool_names(name: str) -> list[str]:
    names = [name]
    if os.name == "nt":
        names.insert(0, f"{name}.exe")
    return names


def find_tool(name: str) -> str | None:
    """
    Ищет ffmpeg/ffprobe:
    1) рядом со скриптом или исполняемым файлом;
    2) внутри PyInstaller-временной папки для onefile;
    3) внутри macOS .app: Contents/MacOS, Contents/Resources, Contents/Resources/bin;
    4) рядом с .app в папке пользователя;
    5) в PATH.
    """
    candidate_dirs: list[Path] = []

    base = app_dir()
    candidate_dirs.append(base)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate_dirs.append(Path(meipass))
        candidate_dirs.append(Path(meipass) / "bin")

    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            contents_dir = exe_dir.parent
            candidate_dirs.extend([
                contents_dir / "MacOS",
                contents_dir / "Resources",
                contents_dir / "Resources" / "bin",
            ])
            external_dir = mac_app_external_dir()
            if external_dir is not None:
                candidate_dirs.extend([external_dir, external_dir / "bin"])

    seen_dirs: set[str] = set()
    for directory in candidate_dirs:
        key = str(directory)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)

        for tool_name in possible_tool_names(name):
            candidate = directory / tool_name
            if candidate.exists() and candidate.is_file():
                return str(candidate)

    found = shutil.which(name)
    if found:
        return found
    return None


def run_command(cmd: list[str], allow_fail: bool = False) -> tuple[int, str, str]:
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if process.returncode != 0 and not allow_fail:
        stderr = process.stderr.strip()
        stdout = process.stdout.strip()
        details = stderr or stdout or "команда завершилась с ошибкой без текста ошибки"
        raise SplitError(details)
    return process.returncode, process.stdout, process.stderr


def bytes_to_mb(value: int | float) -> float:
    return float(value) / MB


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    return f"{m:02d}:{s:06.3f}"


def get_duration_seconds(input_path: Path, ffprobe: str) -> float:
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(input_path),
    ]
    _, stdout, _ = run_command(cmd)
    try:
        data = json.loads(stdout)
        duration = float(data["format"]["duration"])
    except Exception as exc:
        raise SplitError("Не удалось определить длительность аудиофайла через ffprobe.") from exc

    if not math.isfinite(duration) or duration <= 0:
        raise SplitError("ffprobe вернул некорректную длительность аудиофайла.")
    return duration


def detect_silences(
    input_path: Path,
    ffmpeg: str,
    duration: float,
    noise_db: int = SILENCE_NOISE_DB,
    min_silence_sec: float = SILENCE_MIN_SEC,
) -> list[tuple[float, float]]:
    """Возвращает интервалы тишины [(start_sec, end_sec), ...]."""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i", str(input_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null",
        "-",
    ]
    returncode, stdout, stderr = run_command(cmd, allow_fail=True)
    if returncode != 0:
        # Не валим весь процесс: просто режем по расчетному времени.
        return []

    log = stdout + "\n" + stderr
    start_re = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
    end_re = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")

    silences: list[tuple[float, float]] = []
    current_start: float | None = None

    for line in log.splitlines():
        start_match = start_re.search(line)
        if start_match:
            current_start = float(start_match.group(1))
            continue

        end_match = end_re.search(line)
        if end_match and current_start is not None:
            end = float(end_match.group(1))
            if end > current_start:
                silences.append((max(0.0, current_start), min(duration, end)))
            current_start = None

    # Если файл заканчивается тишиной, ffmpeg может не вывести silence_end до конца.
    if current_start is not None and duration > current_start:
        silences.append((current_start, duration))

    return silences


def choose_silence_cut(
    silences: Iterable[tuple[float, float]],
    start: float,
    end: float,
    target: float,
    window_sec: float,
) -> float | None:
    """Ищет ближайшую к target паузу внутри диапазона [start, end]."""
    hard_left = start + MIN_SEGMENT_SEC
    hard_right = end - MIN_SEGMENT_SEC
    if hard_right <= hard_left:
        return None

    left = max(hard_left, target - window_sec)
    right = min(hard_right, target + window_sec)
    if right <= left:
        return None

    best_cut: float | None = None
    best_score: float | None = None

    for silence_start, silence_end in silences:
        overlap_start = max(left, silence_start)
        overlap_end = min(right, silence_end)
        if overlap_end <= overlap_start:
            continue

        cut = (overlap_start + overlap_end) / 2.0
        score = abs(cut - target)
        if best_score is None or score < best_score:
            best_score = score
            best_cut = cut

    return best_cut


def plan_intervals(
    duration: float,
    input_size: int,
    max_bytes: int,
    silences: list[tuple[float, float]],
    prefer_silence: bool,
) -> list[tuple[float, float]]:
    target_bytes = max_bytes * TARGET_FILL_RATIO
    target_duration = duration * target_bytes / max(1, input_size)
    target_duration = max(MIN_SEGMENT_SEC * 2, target_duration)

    intervals: list[tuple[float, float]] = []
    start = 0.0

    while start + target_duration < duration:
        target = start + target_duration
        cut = None
        if prefer_silence:
            cut = choose_silence_cut(
                silences=silences,
                start=start,
                end=duration,
                target=target,
                window_sec=SILENCE_SEARCH_WINDOW_SEC,
            )
        if cut is None:
            cut = target

        # Защита от бесконечного цикла.
        cut = max(start + MIN_SEGMENT_SEC, min(cut, duration))
        intervals.append((start, cut))
        start = cut

    if duration - start > 0.01:
        intervals.append((start, duration))

    return intervals


def split_interval_again(
    interval: tuple[float, float],
    silences: list[tuple[float, float]],
    prefer_silence: bool,
) -> tuple[tuple[float, float], tuple[float, float]]:
    start, end = interval
    if end - start <= MIN_SEGMENT_SEC * 2:
        raise SplitError(
            "Не удалось безопасно уменьшить часть: фрагмент уже слишком короткий, "
            "но размер все еще выше лимита. Возможно, файл имеет необычно высокий битрейт."
        )

    target = (start + end) / 2.0
    cut = None
    if prefer_silence:
        cut = choose_silence_cut(
            silences=silences,
            start=start,
            end=end,
            target=target,
            window_sec=max(SILENCE_SEARCH_WINDOW_SEC, (end - start) / 3.0),
        )
    if cut is None:
        cut = target

    cut = max(start + MIN_SEGMENT_SEC, min(cut, end - MIN_SEGMENT_SEC))
    return (start, cut), (cut, end)


def export_segment(
    input_path: Path,
    output_path: Path,
    ffmpeg: str,
    start: float,
    end: float,
) -> None:
    duration = max(0.01, end - start)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-map", "0:a:0",
        "-vn",
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    run_command(cmd)


def split_audio_file(
    input_file: str | Path,
    output_dir: str | Path | None,
    max_mb: float = DEFAULT_MAX_MB,
    prefer_silence: bool = True,
    overwrite_existing: bool = False,
    log: Callable[[str], None] = print,
) -> list[Path]:
    input_path = Path(input_file).expanduser().resolve()
    if not input_path.exists() or not input_path.is_file():
        raise SplitError("Исходный аудиофайл не найден.")

    if not input_path.suffix:
        raise SplitError("У исходного файла нет расширения. FFmpeg может не понять, в какой формат сохранять части.")

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SplitError(
            "Не найдены ffmpeg и/или ffprobe. Установите FFmpeg и добавьте его в PATH "
            "или положите ffmpeg/ffprobe рядом со скриптом, рядом с приложением, "
            "либо внутрь .app в Contents/MacOS."
        )

    max_bytes = int(max_mb * MB)
    if max_bytes < 2 * MB:
        raise SplitError("Лимит слишком маленький. Поставьте хотя бы 2 MB.")

    input_size = input_path.stat().st_size
    log(f"Исходный файл: {input_path}")
    log(f"Размер исходного файла: {bytes_to_mb(input_size):.2f} MB")
    log(f"Лимит на часть: меньше {max_mb:.2f} MB")

    if input_size < max_bytes:
        log("Файл уже меньше лимита. Разбивка не требуется.")
        return []

    duration = get_duration_seconds(input_path, ffprobe)
    log(f"Длительность: {fmt_time(duration)}")

    silences: list[tuple[float, float]] = []
    if prefer_silence:
        log("Ищу паузы, чтобы по возможности не резать фразы посередине...")
        silences = detect_silences(input_path, ffmpeg, duration)
        if silences:
            log(f"Найдено пауз: {len(silences)}")
        else:
            log("Подходящие паузы не найдены или анализ тишины не удался. Будет разрез по расчетному времени.")

    intervals = plan_intervals(duration, input_size, max_bytes, silences, prefer_silence)
    log(f"Первичный план: {len(intervals)} частей")

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{input_path.stem}_split_", dir=str(out_dir)))
    temp_paths: list[Path] = []

    try:
        i = 0
        while i < len(intervals):
            start, end = intervals[i]
            temp_path = temp_dir / f"part_{i + 1:05d}{input_path.suffix}"
            if temp_path.exists():
                temp_path.unlink()

            log(f"Создаю часть {i + 1}/{len(intervals)}: {fmt_time(start)} - {fmt_time(end)}")
            export_segment(input_path, temp_path, ffmpeg, start, end)

            part_size = temp_path.stat().st_size
            if part_size >= max_bytes:
                temp_path.unlink(missing_ok=True)
                log(
                    f"Часть получилась {bytes_to_mb(part_size):.2f} MB, "
                    "это выше лимита. Делю этот фрагмент еще раз."
                )
                left, right = split_interval_again(intervals[i], silences, prefer_silence)
                intervals[i:i + 1] = [left, right]
                continue

            temp_paths.append(temp_path)
            log(f"OK: {bytes_to_mb(part_size):.2f} MB")
            i += 1

        final_paths = [out_dir / f"{input_path.stem}_{n}{input_path.suffix}" for n in range(1, len(temp_paths) + 1)]
        conflicts = [path for path in final_paths if path.exists()]
        if conflicts and not overwrite_existing:
            examples = "\n".join(str(path) for path in conflicts[:5])
            more = "" if len(conflicts) <= 5 else f"\n...и еще {len(conflicts) - 5}"
            raise SplitError(
                "В папке назначения уже есть файлы с такими именами. "
                "Включите перезапись или выберите другую папку.\n" + examples + more
            )

        for final_path in final_paths:
            if final_path.exists() and overwrite_existing:
                final_path.unlink()

        for temp_path, final_path in zip(temp_paths, final_paths):
            temp_path.replace(final_path)

        for final_path in final_paths:
            if final_path.stat().st_size >= max_bytes:
                raise SplitError(f"Проверка не пройдена: {final_path.name} все еще выше лимита.")

        log("Готово.")
        for path in final_paths:
            log(f"Сохранено: {path.name} ({bytes_to_mb(path.stat().st_size):.2f} MB)")
        return final_paths

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class AudioSplitterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Audio Splitter < 50 MB")
        self.geometry("820x620")
        self.minsize(760, 540)

        self.input_files: list[Path] = []
        self.output_var = tk.StringVar()
        self.max_mb_var = tk.StringVar(value=str(DEFAULT_MAX_MB))
        self.prefer_silence_var = tk.BooleanVar(value=True)
        self.overwrite_var = tk.BooleanVar(value=False)
        self.files_count_var = tk.StringVar(value="Файлы не выбраны")

        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(9, weight=1)

        ttk.Label(root, text="Аудиофайлы / список очереди:").grid(row=0, column=0, sticky="nw", pady=4)

        files_frame = ttk.Frame(root)
        files_frame.grid(row=0, column=1, sticky="nsew", padx=8, pady=4)
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)

        self.files_listbox = tk.Listbox(files_frame, height=6, activestyle="none", selectmode="extended")
        self.files_listbox.grid(row=0, column=0, sticky="nsew")

        files_scrollbar = ttk.Scrollbar(files_frame, orient="vertical", command=self.files_listbox.yview)
        files_scrollbar.grid(row=0, column=1, sticky="ns")
        self.files_listbox.configure(yscrollcommand=files_scrollbar.set)

        buttons_frame = ttk.Frame(root)
        buttons_frame.grid(row=0, column=2, sticky="new", pady=4)
        ttk.Button(buttons_frame, text="Добавить файлы...", command=self.choose_inputs).pack(fill="x", pady=(0, 6))
        ttk.Button(buttons_frame, text="Добавить папку...", command=self.choose_input_folder).pack(fill="x", pady=(0, 6))
        ttk.Button(buttons_frame, text="Удалить выбранные", command=self.remove_selected_inputs).pack(fill="x", pady=(0, 6))
        ttk.Button(buttons_frame, text="Очистить список", command=self.clear_inputs).pack(fill="x")

        ttk.Label(root, textvariable=self.files_count_var).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(root, text="Папка сохранения:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=self.output_var).grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(root, text="Выбрать папку", command=self.choose_output).grid(row=2, column=2, sticky="ew", pady=4)

        ttk.Label(root, text="Пусто = рядом с каждым исходным файлом").grid(row=3, column=1, sticky="w", padx=8)

        ttk.Label(root, text="Максимум на часть, MB:").grid(row=4, column=0, sticky="w", pady=8)
        ttk.Entry(root, textvariable=self.max_mb_var, width=12).grid(row=4, column=1, sticky="w", padx=8, pady=8)

        ttk.Checkbutton(
            root,
            text="Стараться резать по паузам/тишине",
            variable=self.prefer_silence_var,
        ).grid(row=5, column=1, sticky="w", padx=8, pady=2)

        ttk.Checkbutton(
            root,
            text="Перезаписывать существующие части с такими именами",
            variable=self.overwrite_var,
        ).grid(row=6, column=1, sticky="w", padx=8, pady=2)

        ttk.Label(
            root,
            text="Выбор нескольких файлов на macOS: Cmd/Shift в окне выбора. Альтернатива: «Добавить файлы» несколько раз или «Добавить папку».",
        ).grid(row=7, column=1, sticky="w", padx=8, pady=(2, 0))

        ttk.Label(
            root,
            text="Важно: если выбрана общая папка сохранения, файлы с одинаковым именем будут конфликтовать.",
        ).grid(row=8, column=1, sticky="w", padx=8, pady=(2, 0))

        self.log_text = tk.Text(root, height=14, wrap="word")
        self.log_text.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(12, 6))

        scrollbar = ttk.Scrollbar(root, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=9, column=3, sticky="ns", pady=(12, 6))
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.grid(row=10, column=0, columnspan=2, sticky="ew", pady=6)

        self.start_button = ttk.Button(root, text="Разделить выбранные файлы", command=self.start_split)
        self.start_button.grid(row=10, column=2, sticky="ew", pady=6)

        self.log(
            "Нажмите «Добавить файлы...». В окне выбора можно выделить несколько файлов через Cmd/Shift; "
            "если диалог вашей системы этого не дает, нажимайте «Добавить файлы...» несколько раз или используйте «Добавить папку...». "
            "Папку сохранения можно оставить пустой: части сохранятся рядом с каждым исходным файлом."
        )

    def _path_key(self, path: Path) -> str:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        key = str(resolved)
        return key.casefold() if os.name == "nt" else key

    def _normalize_file_dialog_result(self, filenames: object) -> list[Path]:
        if not filenames:
            return []

        if isinstance(filenames, (tuple, list)):
            raw_names = filenames
        else:
            try:
                raw_names = self.tk.splitlist(str(filenames))
            except tk.TclError:
                raw_names = [str(filenames)]

        return [Path(str(name)) for name in raw_names if str(name).strip()]

    def add_input_paths(self, paths: Iterable[Path]) -> None:
        candidate_paths = list(paths)
        existing = {self._path_key(path) for path in self.input_files}

        added = 0
        for path in candidate_paths:
            input_path = Path(path).expanduser()
            if not input_path.exists() or not input_path.is_file():
                continue

            key = self._path_key(input_path)
            if key in existing:
                continue

            self.input_files.append(input_path)
            existing.add(key)
            added += 1

        self.refresh_file_list()

        if candidate_paths and added == 0:
            messagebox.showinfo("Info", "No new files were added. They may already be in the list.")

    def choose_inputs(self) -> None:
        # askopenfilenames is the Tkinter dialog intended for selecting multiple files.
        # On macOS: select with Command-click or Shift-click, then press Open.
        filenames = filedialog.askopenfilenames(
            parent=self,
            title="Выберите аудиофайлы: можно несколько через Cmd/Shift",
            filetypes=[
                ("Audio files", AUDIO_FILE_PATTERNS),
                ("All files", "*.*"),
            ],
        )
        selected_paths = self._normalize_file_dialog_result(filenames)
        if selected_paths:
            self.add_input_paths(selected_paths)

    def choose_input_folder(self) -> None:
        directory = filedialog.askdirectory(title="Select a folder with audio files")
        if not directory:
            return

        folder = Path(directory).expanduser()
        audio_files = sorted(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        )

        if not audio_files:
            messagebox.showinfo("Info", "No supported audio files were found in this folder.")
            return

        self.add_input_paths(audio_files)

    def remove_selected_inputs(self) -> None:
        selected_indices = set(self.files_listbox.curselection())
        if not selected_indices:
            return

        self.input_files = [
            path for index, path in enumerate(self.input_files)
            if index not in selected_indices
        ]
        self.refresh_file_list()

    def clear_inputs(self) -> None:
        self.input_files = []
        self.refresh_file_list()

    def refresh_file_list(self) -> None:
        self.files_listbox.delete(0, "end")
        for input_file in self.input_files:
            self.files_listbox.insert("end", str(input_file))

        count = len(self.input_files)
        if count == 0:
            self.files_count_var.set("Файлы не выбраны")
        elif count == 1:
            self.files_count_var.set("Выбран 1 файл")
        else:
            self.files_count_var.set(f"Выбрано файлов: {count}")

    def choose_output(self) -> None:
        directory = filedialog.askdirectory(title="Выберите папку для сохранения")
        if directory:
            self.output_var.set(directory)

    def log(self, message: str) -> None:
        def append() -> None:
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
        self.after(0, append)

    def _has_duplicate_names_for_common_output(self, input_files: list[Path]) -> bool:
        seen: set[tuple[str, str]] = set()
        for path in input_files:
            key = (path.stem.lower(), path.suffix.lower())
            if key in seen:
                return True
            seen.add(key)
        return False

    def start_split(self) -> None:
        input_files = list(self.input_files)
        output_dir = self.output_var.get().strip() or None

        if not input_files:
            messagebox.showerror("Ошибка", "Выберите один или несколько аудиофайлов.")
            return

        if output_dir and self._has_duplicate_names_for_common_output(input_files):
            messagebox.showerror(
                "Ошибка",
                "В списке есть файлы с одинаковым именем и расширением. При выбранной общей папке "
                "сохранения они создадут одинаковые части вида oldname_1, oldname_2 и т.д. "
                "Оставьте папку сохранения пустой или выберите файлы с разными именами."
            )
            return

        try:
            max_mb = float(self.max_mb_var.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("Ошибка", "Введите число в поле максимального размера.")
            return

        self.start_button.configure(state="disabled")
        self.progress.start(10)
        self.log("---")
        self.log(f"Запущена пакетная обработка. Файлов в очереди: {len(input_files)}")

        worker = threading.Thread(
            target=self._worker,
            args=(input_files, output_dir, max_mb, self.prefer_silence_var.get(), self.overwrite_var.get()),
            daemon=True,
        )
        worker.start()

    def _worker(
        self,
        input_files: list[Path],
        output_dir: str | None,
        max_mb: float,
        prefer_silence: bool,
        overwrite_existing: bool,
    ) -> None:
        total_created = 0
        skipped_count = 0
        errors: list[tuple[Path, str]] = []

        for index, input_file in enumerate(input_files, start=1):
            self.log("")
            self.log(f"=== Файл {index}/{len(input_files)}: {input_file.name} ===")

            try:
                final_paths = split_audio_file(
                    input_file=input_file,
                    output_dir=output_dir,
                    max_mb=max_mb,
                    prefer_silence=prefer_silence,
                    overwrite_existing=overwrite_existing,
                    log=self.log,
                )
                if final_paths:
                    total_created += len(final_paths)
                else:
                    skipped_count += 1
            except Exception as exc:
                error_text = str(exc)
                errors.append((input_file, error_text))
                self.log("ОШИБКА: " + error_text)
                self.log("Этот файл пропущен. Перехожу к следующему файлу.")

        def show_result() -> None:
            if errors:
                message = (
                    "Пакетная обработка завершена с ошибками.\n"
                    f"Создано частей: {total_created}\n"
                    f"Файлов уже меньше лимита: {skipped_count}\n"
                    f"Файлов с ошибками: {len(errors)}\n\n"
                    "Подробности смотрите в логе окна."
                )
                messagebox.showwarning("Готово с ошибками", message)
            else:
                if total_created:
                    message = (
                        "Пакетная обработка завершена.\n"
                        f"Создано частей: {total_created}\n"
                        f"Файлов уже меньше лимита: {skipped_count}"
                    )
                else:
                    message = "Разбивка не требовалась: все выбранные файлы уже меньше лимита."
                messagebox.showinfo("Готово", message)

        self.log("")
        self.log("=== Пакетная обработка завершена ===")
        self.log(f"Создано частей: {total_created}")
        self.log(f"Файлов уже меньше лимита: {skipped_count}")
        self.log(f"Файлов с ошибками: {len(errors)}")

        self.after(0, show_result)
        self.after(0, self._finish_ui)

    def _finish_ui(self) -> None:
        self.progress.stop()
        self.start_button.configure(state="normal")

def main() -> None:
    app = AudioSplitterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
